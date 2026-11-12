"""SQLite 持久化：采集轮次、公告快照与正文 FTS5 全文索引。

设计要点：
- 每一轮采集对应 rounds 表一行，公告挂在 round_id 下，轮次之间互不影响；
- 公告记录、公告正文（announcements.body_text）与 FTS5 索引（notice_fts）
  只在整轮抓取全部成功后，于同一事务内写入并标记轮次成功，
  因此失败轮次不会留下半截数据，也不会污染上一轮结果（重抓不叠加）；
- 页面始终展示最近一次 status='success' 的轮次结果，检索也只在该轮范围内进行，
  成功重抓后被撤下的公告不再出现在检索结果中。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Iterable

from scraper import Announcement

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.db"))

# trigram 分词器：按连续三个字符切分，使“联合体投标”等纯汉字短语可按子串命中，
# 且查询词短于三个字符（如单个汉字）不会产生匹配。
SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    start_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',   -- running / success / failed
    list_pages INTEGER NOT NULL DEFAULT 0,
    detail_pages INTEGER NOT NULL DEFAULT 0,
    error_url TEXT,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    project_no TEXT NOT NULL,
    notice_no TEXT NOT NULL,
    notice_type TEXT NOT NULL,
    publish_time TEXT NOT NULL,
    deadline TEXT,
    target_notice_no TEXT,
    title TEXT,
    url TEXT NOT NULL,
    body_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_announcements_round ON announcements(round_id);
CREATE VIRTUAL TABLE IF NOT EXISTS notice_fts USING fts5(
    body_text,
    tokenize = 'trigram'
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)
        # 旧库迁移：补齐正文字段（老轮次没有正文，检索自然不会命中）
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(announcements)").fetchall()}
        if "body_text" not in existing:
            conn.execute("ALTER TABLE announcements ADD COLUMN body_text TEXT NOT NULL DEFAULT ''")
        # 上次进程退出可能留下 running 状态的轮次，统一标记为失败
        conn.execute(
            "UPDATE rounds SET status='failed', finished_at=?, error_message='进程中断，轮次未完成' "
            "WHERE status='running'",
            (_now(),),
        )


def create_round(start_url: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO rounds (started_at, start_url, status) VALUES (?, ?, 'running')",
            (_now(), start_url),
        )
        return cur.lastrowid


def finish_round_success(round_id: int, announcements: Iterable[Announcement], list_pages: int, detail_pages: int) -> None:
    """整轮成功后一次性写入公告记录、正文与 FTS 索引并更新轮次状态。

    单事务：公告行、正文、FTS 索引、轮次状态要么全部提交，要么整体回滚，
    失败轮次不会留下任何半截数据（包括半截索引）。
    """
    with _connect() as conn:
        with conn:  # 事务：要么全部写入，要么全部回滚
            for a in announcements:
                cur = conn.execute(
                    "INSERT INTO announcements (round_id, project_no, notice_no, notice_type, publish_time, "
                    "deadline, target_notice_no, title, url, body_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        round_id,
                        a.project_no,
                        a.notice_no,
                        a.notice_type,
                        a.publish_time,
                        a.deadline,
                        a.target_notice_no,
                        a.title,
                        a.url,
                        a.body_text,
                    ),
                )
                # FTS 行的 rowid 与公告行 id 一一对应，便于检索后回取记录
                conn.execute(
                    "INSERT INTO notice_fts (rowid, body_text) VALUES (?, ?)",
                    (cur.lastrowid, a.body_text),
                )
            conn.execute(
                "UPDATE rounds SET status='success', finished_at=?, list_pages=?, detail_pages=? WHERE id=?",
                (_now(), list_pages, detail_pages, round_id),
            )


def finish_round_failed(round_id: int, error_url: str, error_message: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE rounds SET status='failed', finished_at=?, error_url=?, error_message=? WHERE id=?",
            (_now(), error_url, error_message, round_id),
        )


def get_latest_success_round() -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM rounds WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()


def get_rounds(limit: int = 20) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_announcements(round_id: int) -> list[Announcement]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM announcements WHERE round_id=? ORDER BY publish_time, notice_no",
            (round_id,),
        ).fetchall()
    return [
        Announcement(
            project_no=r["project_no"],
            notice_no=r["notice_no"],
            notice_type=r["notice_type"],
            publish_time=r["publish_time"],
            deadline=r["deadline"],
            target_notice_no=r["target_notice_no"],
            title=r["title"] or r["notice_no"],
            url=r["url"],
            body_text=r["body_text"] or "",
        )
        for r in rows
    ]


def search_announcements(round_id: int, match_expr: str, limit: int = 100) -> list[sqlite3.Row]:
    """在指定轮次内做 FTS5 检索。

    match_expr 为已转义的 FTS5 MATCH 表达式（见 search.build_match_expr）。
    仅返回该轮次的公告：上一轮被撤下的公告不在本轮，自然不会命中。
    """
    with _connect() as conn:
        return conn.execute(
            "SELECT a.* FROM notice_fts f "
            "JOIN announcements a ON a.id = f.rowid "
            "WHERE a.round_id = ? AND notice_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (round_id, match_expr, limit),
        ).fetchall()
