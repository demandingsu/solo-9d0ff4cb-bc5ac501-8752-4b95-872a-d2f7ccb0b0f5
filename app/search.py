"""公告正文检索：查询校验、FTS5 表达式构造与命中片段生成。

检索规则（与需求对应）：
- 只按“连续至少三个汉字”的短语检索：输入中所有连续汉字串，长度 >= 3 的才作为
  检索短语；不足三个汉字（单字、两字词）以及英文、数字、标点一律忽略；
- 多个汉字短语之间是“且”（AND）关系，必须全部命中才返回；
- 没有任何合格短语时抛出 QueryError，由页面提示，不发起查询；
- 片段只从公告正文可见文字中截取并高亮，导航、侧栏等不参与（采集阶段已剔除）。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

import db

# 连续汉字串（CJK 统一表意文字及扩展 A，覆盖常见中文）
_HAN_RUN_RE = re.compile(r"[㐀-䶿一-鿿]+")
MIN_HAN_LEN = 3
_SNIPPET_RADIUS = 24  # 命中点前后保留的字符数
_MAX_SNIPPETS = 3


class QueryError(ValueError):
    """检索词不符合“连续至少三个汉字”的要求。"""


@dataclass
class SearchHit:
    project_no: str
    notice_no: str
    notice_type: str
    title: str
    url: str
    snippets: list[str]
    body_text: str


def extract_han_phrases(query: str) -> list[str]:
    """取出查询中所有长度 >= 3 的连续汉字短语。"""
    return [run for run in _HAN_RUN_RE.findall(query or "") if len(run) >= MIN_HAN_LEN]


def build_match_expr(query: str) -> str:
    """把查询转换为 FTS5 trigram 的 MATCH 短语表达式。

    每个合格汉字短语包成双引号短语（内部双引号转义），多短语以空格连接，
    在 FTS5 中隐式表示 AND。无合格短语时抛 QueryError。
    """
    phrases = extract_han_phrases(query)
    if not phrases:
        raise QueryError("检索词须包含连续至少三个汉字的短语，例如：联合体投标")
    return " ".join('"' + p.replace('"', '""') + '"' for p in phrases)


def _make_snippets(body_text: str, phrases: list[str]) -> list[str]:
    """在正文中定位命中位置，生成带 <mark> 高亮的去重片段（纯 Python 实现，便于测试）。"""
    lowered = body_text.lower()
    lowered_phrases = [p.lower() for p in phrases]
    positions: list[int] = []
    for p in lowered_phrases:
        start = 0
        while True:
            idx = lowered.find(p, start)
            if idx < 0:
                break
            positions.append((idx, idx + len(p)))
            start = idx + 1
    if not positions:
        return []

    positions.sort()
    # 合并重叠或紧邻的命中区间，避免重复展示
    merged: list[list[int]] = []
    for begin, end in positions:
        if merged and begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])

    snippets: list[str] = []
    seen: set[str] = set()
    for begin, end in merged[:_MAX_SNIPPETS]:
        left = max(0, begin - _SNIPPET_RADIUS)
        right = min(len(body_text), end + _SNIPPET_RADIUS)
        prefix = "…" if left > 0 else ""
        suffix = "…" if right < len(body_text) else ""
        # 先转义再高亮，避免正文里的 < > 破坏页面结构
        before = html.escape(body_text[left:begin])
        marked = html.escape(body_text[begin:end])
        after = html.escape(body_text[end:right])
        text = f"{prefix}{before}<mark>{marked}</mark>{after}{suffix}"
        if text not in seen:
            seen.add(text)
            snippets.append(text)
    return snippets


def search(round_id: int, query: str) -> tuple[list[SearchHit], list[str]]:
    """在指定轮次内检索，返回命中列表与实际生效的汉字短语。"""
    phrases = extract_han_phrases(query)
    if not phrases:
        raise QueryError("检索词须包含连续至少三个汉字的短语，例如：联合体投标")
    rows = db.search_announcements(round_id, build_match_expr(query))
    hits: list[SearchHit] = []
    for r in rows:
        body = r["body_text"] or ""
        snippets = _make_snippets(body, phrases)
        if not snippets:
            continue  # 理论上不会发生，防御性跳过
        hits.append(
            SearchHit(
                project_no=r["project_no"],
                notice_no=r["notice_no"],
                notice_type=r["notice_type"],
                title=r["title"] or r["notice_no"],
                url=r["url"],
                snippets=snippets,
                body_text=body,
            )
        )
    return hits, phrases
