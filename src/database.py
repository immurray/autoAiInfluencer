"""SQLite 数据库封装，新增 caption_log 与 post_history 支持。"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence
import json
import sqlite3
import threading
import time


class Database:
    """轻量级 SQLite 封装，线程安全。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS post_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_name TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    caption TEXT,
                    style TEXT,
                    post_time TEXT NOT NULL,
                    result TEXT,
                    dry_run INTEGER NOT NULL DEFAULT 1,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS caption_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_name TEXT NOT NULL,
                    style TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    extra JSON,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def get_posted_images(self) -> Sequence[str]:
        with self._connect() as conn:
            cur = conn.execute("SELECT image_name FROM post_history")
            return [row[0] for row in cur.fetchall()]

    def record_post(
        self,
        *,
        image_path: Path,
        caption: str,
        style: Optional[str],
        post_time: str,
        result: Optional[dict] = None,
        dry_run: bool = True,
        error: Optional[str] = None,
    ) -> None:
        payload = json.dumps(result or {}, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO post_history(image_name, image_path, caption, style, post_time, result, dry_run, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    Path(image_path).name,
                    str(image_path),
                    caption,
                    style,
                    post_time,
                    payload,
                    1 if dry_run else 0,
                    error,
                ),
            )
            conn.commit()

    def log_caption(
        self,
        *,
        image_path: Path,
        caption: str,
        style: str,
        provider: str,
        extra: Optional[dict] = None,
    ) -> None:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO caption_log(image_name, style, caption, provider, extra, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    Path(image_path).name,
                    style,
                    caption,
                    provider,
                    json.dumps(extra or {}, ensure_ascii=False),
                    now_iso,
                ),
            )
            conn.commit()

    def fetch_post_history(self, limit: int = 20) -> List[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT image_name, image_path, caption, style, post_time, result, dry_run, error FROM post_history ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    def fetch_caption_logs(self, limit: int = 20) -> List[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT image_name, style, caption, provider, extra, created_at FROM caption_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    def mark_post_result(
        self,
        image_name: str,
        result: Optional[dict],
        error: Optional[str],
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE post_history
                   SET result = ?, error = ?
                 WHERE id = (
                        SELECT id FROM post_history
                         WHERE image_name = ?
                         ORDER BY id DESC
                         LIMIT 1
                 )
                """,
                (json.dumps(result or {}, ensure_ascii=False), error, image_name),
            )
            conn.commit()


__all__ = ["Database"]
