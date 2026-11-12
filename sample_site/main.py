"""示例公告站：模拟公开招标公告网站。

页面结构（与采集端解析约定一致）：
- 列表页 /list.html?page=N
    - 公告链接：ul.notice-list a[href]
    - 分页导航：nav.pagination a[href]
- 详情页 /detail/<公告编号>.html
    - 容器 div.notice，data-notice-type = tender|correction|cancellation
    - #project-no 项目编号、#notice-no 公告编号、#publish-time 发布时间
    - #deadline 截标时间（如有）、#target-notice 更正/撤销所指公告编号（如有）

故障注入：GET /admin/fail?on=1 开启后，详情页 NT-2026-0004 返回 500，
用于演示采集端“任一页面失败则保留上一轮结果”的行为；on=0 关闭。

公告撤下：GET /admin/remove?notice_no=... 撤下指定公告（列表不再展示、
详情返回 404），用于演示“成功重抓并撤下某公告后，检索不再返回它”；
GET /admin/remove?notice_no=...&on=0 恢复。
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import os

from data import ANNOUNCEMENTS, PAGE_SIZE, TYPE_LABELS, get_by_notice_no, sorted_announcements

app = FastAPI(title="示例公告站")
# 模板目录取本文件所在路径，避免进程工作目录不同时找不到模板
_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

# 故障注入状态：开启后 FAIL_NOTICE 对应的详情页返回 500
FAIL_STATE = {"on": False}
FAIL_NOTICE = "NT-2026-0004"

# 已撤下的公告编号：不进入列表页，详情页返回 404（模拟公告从站点撤下）
REMOVED_NOTICES: set[str] = set()


def visible_announcements():
    """当前仍在站点上的公告（撤下的不参与列表与详情）。"""
    return [a for a in sorted_announcements() if a["notice_no"] not in REMOVED_NOTICES]


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/list.html?page=1")


@app.get("/list.html", response_class=HTMLResponse)
def list_page(request: Request, page: int = 1):
    items_all = visible_announcements()
    total_pages = max(1, (len(items_all) + PAGE_SIZE - 1) // PAGE_SIZE)
    if page < 1 or page > total_pages:
        return HTMLResponse("<h1>404 页码不存在</h1>", status_code=404)
    start = (page - 1) * PAGE_SIZE
    items = []
    for a in items_all[start : start + PAGE_SIZE]:
        items.append({**a, "type_label": TYPE_LABELS[a["type"]]})
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "items": items,
            "page": page,
            "total_pages": total_pages,
            "pages": list(range(1, total_pages + 1)),
            "fail_on": FAIL_STATE["on"],
            "fail_notice": FAIL_NOTICE,
            "removed": sorted(REMOVED_NOTICES),
        },
    )


@app.get("/detail/{notice_no}.html", response_class=HTMLResponse)
def detail_page(request: Request, notice_no: str):
    if FAIL_STATE["on"] and notice_no == FAIL_NOTICE:
        return HTMLResponse(
            f"<h1>500 Internal Server Error</h1><p>故障注入已开启：{notice_no} 暂时不可用。</p>",
            status_code=500,
        )
    if notice_no in REMOVED_NOTICES:
        return HTMLResponse(f"<h1>404 公告已撤下：{notice_no}</h1>", status_code=404)
    a = get_by_notice_no(notice_no)
    if a is None:
        return HTMLResponse(f"<h1>404 公告不存在：{notice_no}</h1>", status_code=404)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"a": {**a, "type_label": TYPE_LABELS[a["type"]]}, "fail_on": FAIL_STATE["on"]},
    )


@app.get("/admin/fail", response_class=HTMLResponse)
def admin_fail(on: int = 0):
    FAIL_STATE["on"] = bool(on)
    state = "已开启" if FAIL_STATE["on"] else "已关闭"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>故障注入</title></head>
<body>
<h1>故障注入{state}</h1>
<p>开启后，详情页 <code>/detail/{FAIL_NOTICE}.html</code> 将返回 500，用于验证采集端失败处理。</p>
<p><a href="/admin/fail?on=1">开启</a> · <a href="/admin/fail?on=0">关闭</a> · <a href="/list.html?page=1">返回列表</a></p>
</body></html>"""


@app.get("/admin/remove", response_class=HTMLResponse)
def admin_remove(notice_no: str, on: int = 1):
    """撤下（on=1，默认）或恢复（on=0）指定公告，用于演示撤下后重抓的检索行为。"""
    if not get_by_notice_no(notice_no):
        return HTMLResponse(f"<h1>404 公告不存在：{notice_no}</h1>", status_code=404)
    if on:
        REMOVED_NOTICES.add(notice_no)
        action = "已撤下"
    else:
        REMOVED_NOTICES.discard(notice_no)
        action = "已恢复"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>公告撤下管理</title></head>
<body>
<h1>公告 {notice_no} {action}</h1>
<p>撤下后该公告不进入列表页、详情页返回 404；在核对工具重新抓取后，
上一轮包含该公告的检索结果将被新一轮整体替换。</p>
<p>当前已撤下：{('、'.join(sorted(REMOVED_NOTICES)) or '无')}</p>
<p><a href="/admin/remove?notice_no={notice_no}&on=0">恢复本公告</a> · <a href="/list.html?page=1">返回列表</a></p>
</body></html>"""


@app.get("/admin/status")
def admin_status():
    return {
        "fail_on": FAIL_STATE["on"],
        "fail_notice": FAIL_NOTICE,
        "announcements": len(ANNOUNCEMENTS),
        "removed": sorted(REMOVED_NOTICES),
    }
