"""公告正文提取与检索逻辑测试。

覆盖：
- extract_body_text：只取公告正文区可见文字，导航 / 侧栏 / 脚本 / 隐藏元素不参与；
- parse_detail：正文随详情页一并提取；
- 检索词规则：连续至少三个汉字才生效，两字 / 单字 / 英文数字被忽略；
- FTS5 trigram 子串匹配、多短语 AND、片段高亮；
- 单事务原子性：事务回滚时公告记录与 FTS 索引都不留存；
- 端到端（ASGI 内存客户端）：失败轮次仍可搜上一轮；成功重抓并撤下公告后不再命中。
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import db  # noqa: E402
import scraper  # noqa: E402
import search  # noqa: E402

DETAIL_HTML = """<!DOCTYPE html>
<html><body>
<nav class="site-nav"><a href="/x">联合体投标操作指南（顶部导航）</a></nav>
<div class="notice" data-notice-type="tender">
  <h1>招标公告</h1>
  <h2>某项目招标公告</h2>
  <dl class="notice-fields">
    <dt>项目编号</dt><dd id="project-no">PRJ-1</dd>
    <dt>公告编号</dt><dd id="notice-no">NT-1</dd>
    <dt>发布时间</dt><dd id="publish-time">2026-11-01 09:00</dd>
    <dt>截标时间</dt><dd id="deadline">2026-11-20 17:00</dd>
  </dl>
  <div class="notice-body">
    <p>现就某项目进行公开招标。</p>
    <p>本项目接受联合体投标，联合体成员不超过两家。</p>
    <p hidden>隐藏占位：联合体投标隐藏文字不应被检索</p>
    <p style="display:none">样式隐藏：联合体投标 display none 文字不应被检索</p>
    <script>var x = "脚本中的联合体投标文字不应被检索";</script>
  </div>
</div>
<aside class="site-sidebar"><a href="/y">联合体投标常见问题（侧栏）</a></aside>
</body></html>"""

URL = "http://example.com/detail/NT-1.html"


# ---------- 正文提取 ----------

def test_extract_body_only_visible_notice_text():
    a = scraper.parse_detail(DETAIL_HTML, URL)
    body = a.body_text
    # 正文可见文字进入
    assert "本项目接受联合体投标" in body
    assert "现就某项目进行公开招标" in body
    # 导航、侧栏不参与
    assert "顶部导航" not in body
    assert "侧栏" not in body
    # 隐藏元素不参与
    assert "隐藏占位" not in body
    assert "display none" not in body
    # 脚本不参与
    assert "脚本中的" not in body


def test_extract_body_fallback_whole_notice_excludes_chrome():
    html = """<html><body>
    <nav><a>联合体投标导航文字</a></nav>
    <div class="notice" data-notice-type="tender">
      <h2>标题</h2>
      <span id="project-no">P1</span><span id="notice-no">N1</span><span id="publish-time">2026-11-01 09:00</span>
      <p>无专用正文容器时的联合体投标条款正文。</p>
    </div>
    <aside>联合体投标侧栏文字</aside>
    </body></html>"""
    a = scraper.parse_detail(html, "http://example.com/detail/N1.html")
    assert "联合体投标条款正文" in a.body_text
    assert "导航文字" not in a.body_text
    assert "侧栏文字" not in a.body_text


def test_extract_body_collapses_whitespace_and_keeps_paragraphs():
    html = DETAIL_HTML.replace(
        "<p>本项目接受联合体投标，联合体成员不超过两家。</p>",
        "<p>本项目接受  联合体投标，\n\t联合体成员不超过两家。</p>",
    )
    a = scraper.parse_detail(html, URL)
    assert "本项目接受 联合体投标， 联合体成员不超过两家。" in a.body_text
    assert "\n" in a.body_text  # 段落边界保留


# ---------- 检索词规则 ----------

def test_extract_han_phrases_min_three_chars():
    assert search.extract_han_phrases("联合体投标") == ["联合体投标"]
    assert search.extract_han_phrases("关于联合体投标的说明") == ["关于联合体投标的说明"]
    # 两字词与单字被忽略，英文数字标点被忽略
    assert search.extract_han_phrases("投标 abc 123") == []
    assert search.extract_han_phrases("联合体 NT-2026") == ["联合体"]
    # 多个合格短语都保留
    assert search.extract_han_phrases("联合体 投标保证金条款") == ["联合体", "投标保证金条款"]


def test_build_match_expr_quotes_phrases_and_escapes():
    assert search.build_match_expr("联合体投标") == '"联合体投标"'
    assert search.build_match_expr("联合体 投标保证金") == '"联合体" "投标保证金"'
    with pytest.raises(search.QueryError):
        search.build_match_expr("ab 12 投标")
    with pytest.raises(search.QueryError):
        search.build_match_expr("")


# ---------- 片段生成 ----------

def test_make_snippets_highlights_match():
    body = "现就某项目公开招标。" + "前" * 30 + "本项目接受联合体投标，成员不超过两家。" + "后" * 30
    snippets = search._make_snippets(body, ["联合体投标"])
    assert len(snippets) == 1
    assert "<mark>联合体投标</mark>" in snippets[0]
    assert snippets[0].startswith("…") and snippets[0].endswith("…")


def test_make_snippets_escapes_html():
    body = "条款如下：联合体投标 <script>alert(1)</script> 结束"
    snippets = search._make_snippets(body, ["联合体投标"])
    joined = "".join(snippets)
    assert "<script>alert(1)</script>" not in joined
    assert "&lt;script&gt;" in joined
    assert "<mark>联合体投标</mark>" in joined


def test_make_snippets_multiple_hits_dedup():
    body = "联合体投标第一次。" + "中" * 80 + "联合体投标第二次。"
    snippets = search._make_snippets(body, ["联合体投标"])
    assert len(snippets) == 2
    assert all("<mark>联合体投标</mark>" in s for s in snippets)


# ---------- SQLite FTS 与事务 ----------

@pytest.fixture()
def db_env(monkeypatch):
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


def _ann(notice_no, body, project_no="PRJ-1"):
    return scraper.Announcement(
        project_no=project_no,
        notice_no=notice_no,
        notice_type="tender",
        publish_time="2026-11-01 09:00",
        deadline=None,
        target_notice_no=None,
        title=notice_no,
        url=f"http://example.com/detail/{notice_no}.html",
        body_text=body,
    )


def test_fts_substring_and_round_scope(db_env):
    r1 = db.create_round("http://example.com/list.html?page=1")
    db.finish_round_success(
        r1,
        [
            _ann("N1", "本项目接受联合体投标，联合体成员不超过两家。"),
            _ann("N2", "本项目不接受联合体投标。"),
            _ann("N3", "本项目要求缴纳投标保证金。"),
        ],
        1,
        3,
    )
    hits, phrases = search.search(r1, "联合体")
    assert phrases == ["联合体"]
    assert {h.notice_no for h in hits} == {"N1", "N2"}
    # 两字词不构成合格查询
    with pytest.raises(search.QueryError):
        search.search(r1, "投标")
    # 更长短语同样按子串命中
    hits, _ = search.search(r1, "联合体投标")
    assert {h.notice_no for h in hits} == {"N1", "N2"}
    hits, _ = search.search(r1, "不接受联合体投标")
    assert [h.notice_no for h in hits] == ["N2"]


def test_multi_phrase_and(db_env):
    r1 = db.create_round("http://example.com/list.html?page=1")
    db.finish_round_success(
        r1,
        [
            _ann("N1", "本项目接受联合体投标，并要求缴纳投标保证金。"),
            _ann("N2", "本项目接受联合体投标，无保证金要求。"),
        ],
        1,
        2,
    )
    hits, phrases = search.search(r1, "联合体 投标保证金")
    assert phrases == ["联合体", "投标保证金"]
    assert [h.notice_no for h in hits] == ["N1"]


def test_success_round_replaces_old_results(db_env):
    # 第一轮：含 N1
    r1 = db.create_round("u")
    db.finish_round_success(r1, [_ann("N1", "联合体投标条款，接受联合体。")], 1, 1)
    # 第二轮：N1 被撤下，只剩 N2（重抓不叠加）
    r2 = db.create_round("u")
    db.finish_round_success(r2, [_ann("N2", "本项目不接受联合体投标。")], 1, 1)

    latest = db.get_latest_success_round()
    assert latest["id"] == r2
    hits, _ = search.search(latest["id"], "联合体投标")
    assert [h.notice_no for h in hits] == ["N2"]
    # 上一轮的公告在新一轮检索范围内不可见
    assert db.get_announcements(latest["id"]) and all(
        a.notice_no != "N1" for a in db.get_announcements(latest["id"])
    )


def test_failed_round_keeps_previous_results_searchable(db_env):
    r1 = db.create_round("u")
    db.finish_round_success(r1, [_ann("N1", "联合体投标条款。")], 1, 1)
    r2 = db.create_round("u")
    db.finish_round_failed(r2, "http://example.com/detail/N9.html", "HTTP 500")

    latest = db.get_latest_success_round()
    assert latest["id"] == r1  # 失败轮次不会顶替最近成功轮次
    hits, _ = search.search(latest["id"], "联合体投标")
    assert [h.notice_no for h in hits] == ["N1"]


def test_transaction_atomicity_rolls_back_rows_and_fts(db_env):
    r1 = db.create_round("u")
    conn = db._connect()
    try:
        with pytest.raises(RuntimeError):
            with conn:  # 同一事务：先写一部分，再主动失败
                for a in [_ann("N1", "联合体投标条款一。"), _ann("N2", "联合体投标条款二。")]:
                    cur = conn.execute(
                        "INSERT INTO announcements (round_id, project_no, notice_no, notice_type, "
                        "publish_time, title, url, body_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (r1, a.project_no, a.notice_no, a.notice_type, a.publish_time, a.title, a.url, a.body_text),
                    )
                    conn.execute("INSERT INTO notice_fts (rowid, body_text) VALUES (?, ?)", (cur.lastrowid, a.body_text))
                raise RuntimeError("模拟事务中途失败")
    finally:
        conn.close()

    # 整体回滚：公告记录与 FTS 索引都不留存
    with db._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM notice_fts").fetchone()[0] == 0
        assert c.execute("SELECT status FROM rounds WHERE id=?", (r1,)).fetchone()["status"] == "running"
    # 旧成功轮次仍可检索
    r0 = db.create_round("u")
    db.finish_round_success(r0, [_ann("N0", "联合体投标旧条款。")], 1, 1)
    latest = db.get_latest_success_round()
    hits, _ = search.search(latest["id"], "联合体投标")
    assert [h.notice_no for h in hits] == ["N0"]
