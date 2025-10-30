"""SQLite 数据存储与查询。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Set
import logging
import sqlite3
import traceback


@dataclass
class PostRecord:
    """单次发布的记录结构。"""

    image_path: Path
    caption: str
    posted_at: datetime
    platform: str
    external_id: Optional[str]
    dry_run: bool


class Database:
    """对 SQLite 数据库的轻量封装。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._logger = logging.getLogger(__name__)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        """建立数据库连接。"""

        return sqlite3.connect(self._db_path)

    def _ensure_schema(self) -> None:
        """确保表结构存在，并兼容旧版本数据。"""

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.executescript(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_path TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    posted_at TEXT NOT NULL,
                    tweet_id TEXT,
                    platform TEXT NOT NULL DEFAULT 'twitter',
                    external_id TEXT,
                    dry_run INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    context TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS engagements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tweet_id TEXT NOT NULL,
                    likes INTEGER DEFAULT 0,
                    retweets INTEGER DEFAULT 0,
                    replies INTEGER DEFAULT 0,
                    recorded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_posts_image_path ON posts(image_path);
                CREATE INDEX IF NOT EXISTS idx_engagements_tweet_id ON engagements(tweet_id);
                """
            )
            conn.commit()

            self._ensure_new_columns(conn)

    def record_post(self, record: PostRecord) -> None:
        """写入一条发布记录。"""

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO posts(image_path, caption, posted_at, platform, external_id, tweet_id, dry_run)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.image_path),
                    record.caption,
                    record.posted_at.isoformat(),
                    record.platform,
                    record.external_id,
                    record.external_id if record.platform == "twitter" else None,
                    1 if record.dry_run else 0,
                ),
            )
            conn.commit()
        self._logger.info("记录发布：%s", record.image_path)

    def record_error(self, context: str, message: str, exc: Exception) -> None:
        """记录一次错误日志。"""

        details = "".join(traceback.format_exception(exc))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO errors(context, message, details, created_at) VALUES (?, ?, ?, ?)",
                (context, message, details, datetime.utcnow().isoformat()),
            )
            conn.commit()
        self._logger.error("写入错误记录：%s - %s", context, message)

    def record_engagement(self, tweet_id: str, likes: int, retweets: int, replies: int) -> None:
        """记录一次互动数据快照。"""

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO engagements(tweet_id, likes, retweets, replies, recorded_at) VALUES (?, ?, ?, ?, ?)",
                (tweet_id, likes, retweets, replies, datetime.utcnow().isoformat()),
            )
            conn.commit()
        self._logger.info("记录互动数据：%s", tweet_id)

    def get_posted_images(self) -> Set[Path]:
        """返回已经发布过的图片集合。"""

        with self._connect() as conn:
            cursor = conn.execute("SELECT DISTINCT image_path FROM posts")
            rows = cursor.fetchall()
        return {Path(row[0]) for row in rows}

    def list_recent_posts(self, limit: int = 20) -> Iterable[PostRecord]:
        """列出近期的发布记录。"""

        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT image_path, caption, posted_at, platform, external_id, tweet_id, dry_run
                  FROM posts
              ORDER BY posted_at DESC
                 LIMIT ?
                """,
                (limit,),
            )
            for image_path, caption, posted_at, platform, external_id, tweet_id, dry_run in cursor.fetchall():
                final_id = external_id or tweet_id
                yield PostRecord(
                    image_path=Path(image_path),
                    caption=caption,
                    posted_at=datetime.fromisoformat(posted_at),
                    platform=platform or "twitter",
                    external_id=final_id,
                    dry_run=bool(dry_run),
                )

    def _ensure_new_columns(self, conn: sqlite3.Connection) -> None:
        """检查并补齐新增的列，兼容旧版本数据库。"""

        cursor = conn.execute("PRAGMA table_info(posts)")
        columns = {row[1] for row in cursor.fetchall()}

        if "platform" not in columns:
            conn.execute("ALTER TABLE posts ADD COLUMN platform TEXT NOT NULL DEFAULT 'twitter'")
        if "external_id" not in columns:
            conn.execute("ALTER TABLE posts ADD COLUMN external_id TEXT")
        if "tweet_id" not in columns:
            conn.execute("ALTER TABLE posts ADD COLUMN tweet_id TEXT")
        conn.commit()

        if "tweet_id" in columns:
            conn.execute(
                """
                UPDATE posts
                   SET external_id = COALESCE(external_id, tweet_id)
                 WHERE tweet_id IS NOT NULL
                   AND (external_id IS NULL OR external_id = '')
                """
            )
            conn.commit()


__all__ = ["Database", "PostRecord"]
