"""端到端测试：真实 uvicorn 端口跑示例站 + 采集 + 持久化 + 检索。

示例站在后台线程起 uvicorn（随机端口），采集端用普通 httpx.Client 访问，
与 docker compose 内的真实链路一致，避免依赖 httpx 版本特定的 ASGI 测试传输。
"""

import importlib.util
import os
import socket
import sys
import tempfile
import time

import httpx
import pytest
import uvicorn

ROOT = os.path.dirname(__file__)
APP_DIR = os.path.join(ROOT, "..", "app")
SITE_DIR = os.path.join(ROOT, "..", "sample_site")
sys.path.insert(0, APP_DIR)
sys.path.insert(0, SITE_DIR)  # 供示例站的 `from data import ...` 使用


def _load_module(name: str, path: str):
    """按文件路径显式加载模块，避免两个目录下同名 main.py 互相遮蔽。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


import db  # noqa: E402
import scraper  # noqa: E402
import search  # noqa: E402

site_main = _load_module("sample_site_main", os.path.join(SITE_DIR, "main.py"))
app_main = _load_module("app_main", os.path.join(APP_DIR, "main.py"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def site_server():
    port = _free_port()
    config = uvicorn.Config(site_main.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    import threading

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(base_url + "/admin/status", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        raise RuntimeError("示例站未能在限时内启动")
    yield base_url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def isolated_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    yield path
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def reset_site_state():
    site_main.FAIL_STATE["on"] = False
    site_main.REMOVED_NOTICES.clear()
    yield
    site_main.FAIL_STATE["on"] = False
    site_main.REMOVED_NOTICES.clear()


def _scrape_round(base_url):
    start_url = base_url + "/list.html?page=1"
    round_id = db.create_round(start_url)
    try:
        result = scraper.scrape(start_url)
    except scraper.ScrapeError as exc:
        db.finish_round_failed(round_id, exc.url, exc.message)
        return round_id, None
    db.finish_round_success(round_id, result.announcements, len(result.list_pages), len(result.detail_pages))
    return round_id, result


def test_full_flow_scrape_search_fail_then_remove(isolated_db, site_server):
    base_url = site_server

    # 1) 首次成功采集：3 个列表页、11 条详情，正文含“联合体投标”条款
    r1, result = _scrape_round(base_url)
    assert result is not None
    assert len(result.list_pages) == 3
    assert len(result.announcements) == 11
    assert all(a.body_text for a in result.announcements)
    # 导航/侧栏/隐藏/脚本内容不进入正文
    for a in result.announcements:
        assert "顶部导航" not in a.body_text
        assert "侧栏" not in a.body_text
        assert "隐藏占位" not in a.body_text
        assert "脚本中的" not in a.body_text

    latest = db.get_latest_success_round()
    assert latest["id"] == r1
    hits, phrases = search.search(latest["id"], "联合体投标")
    assert phrases == ["联合体投标"]
    hit_notices = {h.notice_no for h in hits}
    # 招标 0001（接受）、更正 0002（条款变更为不接受）、招标 0003（不接受）、招标 0005（接受）
    assert {"NT-2026-0001", "NT-2026-0002", "NT-2026-0003", "NT-2026-0005"} <= hit_notices
    # 命中片段带高亮，且带项目编号与来源链接字段
    one = next(h for h in hits if h.notice_no == "NT-2026-0002")
    assert any("<mark>联合体投标</mark>" in s for s in one.snippets)
    assert one.url.endswith("/detail/NT-2026-0002.html")
    assert one.project_no == "PRJ-2026-001"
    # 不含该条款的公告不命中
    assert "NT-2026-0008" not in hit_notices

    # 2) 故障注入：新一轮抓取失败，上一轮结果仍可检索
    site_main.FAIL_STATE["on"] = True
    r2, failed = _scrape_round(base_url)
    assert failed is None
    assert db.get_latest_success_round()["id"] == r1
    hits, _ = search.search(db.get_latest_success_round()["id"], "联合体投标")
    assert "NT-2026-0001" in {h.notice_no for h in hits}

    # 3) 关闭故障并撤下一条公告后重抓：成功轮次整体替换，被撤公告不再命中
    site_main.FAIL_STATE["on"] = False
    site_main.REMOVED_NOTICES.add("NT-2026-0005")
    r3, result3 = _scrape_round(base_url)
    assert result3 is not None
    assert len(result3.announcements) == 10
    latest = db.get_latest_success_round()
    assert latest["id"] == r3
    hit_notices = {h.notice_no for h in search.search(latest["id"], "联合体投标")[0]}
    assert "NT-2026-0005" not in hit_notices
    # 其他公告仍可检索
    assert "NT-2026-0001" in hit_notices


def test_web_search_route_and_invalid_query(isolated_db, site_server):
    """通过 FastAPI 路由验证页面：无效查询给错误提示，有效查询渲染结果表。"""
    from fastapi.testclient import TestClient

    base_url = site_server
    app_main.DEFAULT_START_URL = base_url + "/list.html?page=1"
    client = TestClient(app_main.app)

    # 尚无采集：检索提示先抓取
    resp = client.get("/search", params={"q": "联合体投标"})
    assert resp.status_code == 200
    assert "尚无成功采集结果" in resp.text

    # 完成一次真实采集
    _scrape_round(base_url)

    # 无效查询（不足三个汉字）
    resp = client.get("/search", params={"q": "投标"})
    assert resp.status_code == 200
    assert "连续至少三个汉字" in resp.text

    # 有效查询：结果表含项目编号、公告编号、片段、来源链接
    resp = client.get("/search", params={"q": "联合体投标"})
    text = resp.text
    assert "PRJ-2026-001" in text
    assert "NT-2026-0001" in text
    assert "<mark>联合体投标</mark>" in text
    assert "/detail/NT-2026-0001.html" in text
    assert "查看原文" in text

    # 无命中
    resp = client.get("/search", params={"q": "资格预审文件售卖"})
    assert "没有命中" in resp.text
    client.close()
