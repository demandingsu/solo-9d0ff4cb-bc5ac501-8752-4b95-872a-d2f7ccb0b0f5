"""列表页 / 详情页抓取与解析。

解析约定（与示例公告站页面结构一致）：
- 列表页：公告链接 ul.notice-list a[href]；分页导航 nav.pagination a[href]
- 详情页：div.notice[data-notice-type]，字段 #project-no / #notice-no /
  #publish-time / #deadline / #target-notice

只跟随与起始列表网址同源的链接；任何一页抓取或解析失败都抛出
ScrapeError（携带出错网址），由上层决定保留上一轮结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

MAX_LIST_PAGES = 50
MAX_DETAIL_PAGES = 500
USER_AGENT = "bid-deadline-checker/1.0"

TYPE_LABELS = {
    "tender": "招标公告",
    "correction": "更正公告",
    "cancellation": "撤销公告",
}

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d")


class ScrapeError(Exception):
    """单页抓取或解析失败。url 为出错的页面网址。"""

    def __init__(self, url: str, message: str):
        self.url = url
        self.message = message
        super().__init__(f"{url}：{message}")


@dataclass
class Announcement:
    project_no: str
    notice_no: str
    notice_type: str  # tender / correction / cancellation
    publish_time: str  # 归一化为 YYYY-MM-DD HH:MM
    deadline: str | None
    target_notice_no: str | None
    title: str
    url: str
    # 公告正文区的可见文字（不含导航、侧栏、脚本与隐藏元素），供全文检索
    body_text: str = ""

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.notice_type, self.notice_type)


@dataclass
class ScrapeResult:
    announcements: list[Announcement] = field(default_factory=list)
    list_pages: list[str] = field(default_factory=list)
    detail_pages: list[str] = field(default_factory=list)


def _default_port(scheme: str) -> int | None:
    return {"http": 80, "https": 443}.get(scheme)


def same_origin(url: str, origin_url: str) -> bool:
    p, o = urlparse(url), urlparse(origin_url)
    return (p.scheme, p.hostname, _default_port(p.scheme) if p.port is None else p.port) == (
        o.scheme,
        o.hostname,
        _default_port(o.scheme) if o.port is None else o.port,
    )


def _normalize(href: str, base: str) -> str:
    url, _frag = urldefrag(urljoin(base, href))
    return url


def _fetch_soup(client: httpx.Client, url: str) -> BeautifulSoup:
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        raise ScrapeError(url, f"请求失败：{exc}") from exc
    if resp.status_code >= 400:
        raise ScrapeError(url, f"HTTP {resp.status_code}")
    return BeautifulSoup(resp.text, "html.parser")


def _norm_time(raw: str, url: str, field_name: str) -> str:
    raw = raw.strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    raise ScrapeError(url, f"{field_name}格式无法识别：{raw!r}")


def _text(node, selector: str) -> str | None:
    el = node.select_one(selector)
    return el.get_text(strip=True) if el else None


# 这些元素只承载脚本 / 样式 / 页面框架（导航、侧栏等），不属于公告正文
_NON_BODY_TAGS = ("script", "style", "noscript", "template", "nav", "header", "footer", "aside", "form")
# 显式声明不向用户展示的样式
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden")


def _is_hidden(node) -> bool:
    """判断元素是否对用户不可见（hidden 属性或 display:none / visibility:hidden）。"""
    if getattr(node, "name", None) is None:
        return False
    if node.has_attr("hidden"):
        return True
    style = node.get("style")
    return bool(style and _HIDDEN_STYLE.search(style.replace(" ", "").lower()))


def extract_body_text(root) -> str:
    """提取公告正文区的可见文字。

    范围限定在公告容器内，且：
    - 优先取专用正文容器 .notice-body（页面框架——导航、侧栏等——本就不在其中）；
    - 没有专用正文容器时退回到整个公告容器，但剔除导航、侧栏等框架元素；
    - 剔除 script/style 等非正文标签与带 hidden / display:none / visibility:hidden
      的隐藏元素；
    - 输出按块级元素换行并折叠空白，保留段落边界，便于展示命中片段。
    """
    scope = root.select_one(".notice-body")
    if scope is None:
        scope = root

    chunks: list[str] = []
    for el in scope.find_all(True):
        if el.name in _NON_BODY_TAGS or _is_hidden(el):
            el.extract()
    for block in scope.find_all(
        ["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "br"], recursive=True
    ):
        piece = block.get_text(" ", strip=True)
        if piece:
            chunks.append(piece)
    # 容器自身可能直接挂文本（没有块级子元素），用整体文本兜底
    whole = scope.get_text(" ", strip=True)
    if not chunks and whole:
        chunks = [whole]

    seen: set[str] = set()
    lines: list[str] = []
    for piece in chunks:
        piece = re.sub(r"\s+", " ", piece).strip()
        if piece and piece not in seen:
            seen.add(piece)
            lines.append(piece)
    return "\n".join(lines)


def parse_detail(html: str, url: str) -> Announcement:
    """解析单个详情页 HTML（独立出来便于测试）。"""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("div.notice")
    if root is None:
        raise ScrapeError(url, "页面结构不符：未找到 div.notice 容器")
    notice_type = (root.get("data-notice-type") or "").strip()
    if notice_type not in TYPE_LABELS:
        raise ScrapeError(url, f"未知公告类型：{notice_type!r}")

    project_no = _text(root, "#project-no")
    notice_no = _text(root, "#notice-no")
    publish_raw = _text(root, "#publish-time")
    missing = [
        name
        for name, val in (("项目编号", project_no), ("公告编号", notice_no), ("发布时间", publish_raw))
        if not val
    ]
    if missing:
        raise ScrapeError(url, "缺少必要字段：" + "、".join(missing))

    deadline_raw = _text(root, "#deadline")
    target = _text(root, "#target-notice")
    if notice_type in ("correction", "cancellation") and not target:
        raise ScrapeError(url, "更正/撤销公告缺少所指公告编号")

    title_el = root.select_one("h2")
    body_text = extract_body_text(root)
    return Announcement(
        project_no=project_no,
        notice_no=notice_no,
        notice_type=notice_type,
        publish_time=_norm_time(publish_raw, url, "发布时间"),
        deadline=_norm_time(deadline_raw, url, "截标时间") if deadline_raw else None,
        target_notice_no=target,
        title=title_el.get_text(strip=True) if title_el else notice_no,
        url=url,
        body_text=body_text,
    )


def scrape(start_url: str, client: httpx.Client | None = None) -> ScrapeResult:
    """从列表网址出发，抓完同源分页与全部详情页。任一页面失败即抛 ScrapeError。

    client 可由调用方注入（例如测试中指向 ASGI 应用的内存客户端）；
    不传则自建带超时与重试设置的 httpx.Client。
    """
    parsed = urlparse(start_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ScrapeError(start_url, "起始网址必须是 http(s) 网址")

    result = ScrapeResult()
    visited_lists: set[str] = set()
    queue = [start_url]
    detail_urls: list[str] = []
    seen_details: set[str] = set()

    def _run(c: httpx.Client) -> None:
        # 1) 遍历同源分页，收集详情页链接
        while queue:
            url = queue.pop(0)
            if url in visited_lists:
                continue
            if len(visited_lists) >= MAX_LIST_PAGES:
                raise ScrapeError(url, f"分页数量超过上限 {MAX_LIST_PAGES}")
            soup = _fetch_soup(c, url)

            items = soup.select("ul.notice-list a[href]")
            nav_links = soup.select("nav.pagination a[href]")
            if not items and not nav_links:
                raise ScrapeError(url, "页面结构不符：未找到公告列表（ul.notice-list）或分页导航（nav.pagination）")

            for a in items:
                href = _normalize(a["href"], url)
                if not same_origin(href, start_url):
                    continue  # 跨源链接不跟随
                if href not in seen_details:
                    seen_details.add(href)
                    detail_urls.append(href)

            visited_lists.add(url)
            result.list_pages.append(url)
            for a in nav_links:
                href = _normalize(a["href"], url)
                if same_origin(href, start_url) and href not in visited_lists and href not in queue:
                    queue.append(href)

        # 2) 逐个抓取详情页（正文可见文字在 parse_detail 内一并提取）
        for durl in detail_urls:
            if len(result.announcements) >= MAX_DETAIL_PAGES:
                raise ScrapeError(durl, f"详情页数量超过上限 {MAX_DETAIL_PAGES}")
            soup = _fetch_soup(c, durl)
            result.announcements.append(parse_detail(str(soup), durl))
            result.detail_pages.append(durl)

    if client is not None:
        _run(client)
    else:
        with httpx.Client(
            timeout=httpx.Timeout(10.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as owned:
            _run(owned)

    return result
