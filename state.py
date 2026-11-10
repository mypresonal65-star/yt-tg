"""
state.py - Persistent SQLite Job Queue for ytdl-telegram.

Stores video download/upload state in jobs.db:
- Tracks: playlist_url, video_id, title, quality, status, file_path,
          telegram_message_id, retry_count, error, timestamps.
- Statuses: queued -> downloading -> downloaded -> uploading -> uploaded (or failed)
- Single source of truth: ensures idempotency without relying on download-archive.txt.
- Multi-stage retry awareness: if video is already downloaded on disk, retry only uploads.
- Retries failed jobs up to 3 times before permanent failure.
- Helper methods for job recovery, retry_failed command, and Telegram summary reports.
"""

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional


@dataclass
class Job:
    """Represents a video processing job in the queue."""
    id: int
    playlist_url: str
    video_id: str
    title: str
    quality: str
    status: str
    file_path: Optional[str]
    telegram_message_id: Optional[str]
    retry_count: int
    error: Optional[str]
    playlist_title: Optional[str]
    created_at: str
    updated_at: str

    @property
    def is_finished(self) -> bool:
        return self.status == "uploaded"

    @property
    def can_retry(self) -> bool:
        return self.status == "failed" and self.retry_count < 3


class StateManager:
    """Manages persistent job records in SQLite database."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.db_path = db_path
        else:
            base_dir = Path(__file__).resolve().parent
            self.db_path = str(base_dir / "jobs.db")

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Creates a connection with row factory enabled."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        """Creates the jobs table and performance indexes if they don't exist."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_url TEXT NOT NULL,
                    playlist_title TEXT,
                    video_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    quality TEXT NOT NULL DEFAULT '1080',
                    status TEXT NOT NULL DEFAULT 'queued',
                    file_path TEXT,
                    telegram_message_id TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_playlist ON jobs(playlist_url);"
            )
            conn.commit()

    def add_job(
        self,
        playlist_url: str,
        video_id: str,
        title: str,
        quality: str = "1080",
        playlist_title: Optional[str] = None,
    ) -> bool:
        """
        Inserts a new video job if not already present.
        Returns True if inserted, False if it was already in the database.
        """
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO jobs 
                (playlist_url, playlist_title, video_id, title, quality, status)
                VALUES (?, ?, ?, ?, ?, 'queued')
                """,
                (playlist_url, playlist_title, video_id, title, quality),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_job(self, video_id: str) -> Optional[Job]:
        """Fetches a specific job by video_id."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM jobs WHERE video_id = ?", (video_id,))
            row = cur.fetchone()
            if row:
                return Job(**dict(row))
        return None

    def get_next_pending_job(self, playlist_url: Optional[str] = None) -> Optional[Job]:
        """
        Fetches the next job ready to be processed:
        - Must be 'queued' or 'downloaded', OR
        - Must be 'failed' with retry_count < 3
        Ordered sequentially by ID.
        """
        query = """
            SELECT * FROM jobs
            WHERE (status IN ('queued', 'downloaded') OR (status = 'failed' AND retry_count < 3))
        """
        params: List[Any] = []
        if playlist_url:
            query += " AND playlist_url = ?"
            params.append(playlist_url)

        query += " ORDER BY id ASC LIMIT 1;"

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            row = cur.fetchone()
            if row:
                return Job(**dict(row))
        return None

    def mark_downloading(self, video_id: str) -> None:
        """Marks video as currently downloading."""
        self._update_status(video_id, status="downloading", error=None)

    def mark_downloaded(self, video_id: str, file_path: str) -> None:
        """Marks video as downloaded with local file path."""
        self._update_status(video_id, status="downloaded", file_path=file_path, error=None)

    def mark_uploading(self, video_id: str) -> None:
        """Marks video as currently uploading to Telegram."""
        self._update_status(video_id, status="uploading")

    def mark_uploaded(self, video_id: str, telegram_message_id: str) -> None:
        """Marks video as fully uploaded with Telegram message ID."""
        self._update_status(
            video_id,
            status="uploaded",
            telegram_message_id=telegram_message_id,
            file_path=None,
            error=None,
        )

    def record_failure(
        self,
        video_id: str,
        error_message: str,
        keep_file_path: bool = False,
    ) -> int:
        """
        Increments retry_count and records error.
        If keep_file_path is True and file exists on disk, preserves file_path
        and keeps status as 'downloaded' (so next retry uploads directly without re-downloading).
        If retry_count >= 3: marks status as 'failed'.
        Returns the new retry_count.
        """
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT retry_count, file_path FROM jobs WHERE video_id = ?", (video_id,))
            row = cur.fetchone()
            current_retries = row["retry_count"] if row else 0
            existing_file_path = row["file_path"] if row else None
            new_retries = current_retries + 1

            file_exists = (
                keep_file_path
                and existing_file_path
                and os.path.isfile(existing_file_path)
                and os.path.getsize(existing_file_path) > 0
            )

            file_to_save = existing_file_path if file_exists else None

            if new_retries >= 3:
                new_status = "failed"
            else:
                new_status = "downloaded" if file_exists else "queued"

            cur.execute(
                """
                UPDATE jobs
                SET retry_count = ?,
                    status = ?,
                    error = ?,
                    file_path = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE video_id = ?
                """,
                (new_retries, new_status, error_message, file_to_save, video_id),
            )
            conn.commit()
            return new_retries

    def reset_interrupted_jobs(self) -> int:
        """
        Resets jobs left stuck in 'downloading' or 'uploading'.
        If file exists on disk, sets status to 'downloaded' so upload resumes directly.
        Otherwise resets to 'queued'.
        """
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT video_id, file_path, status FROM jobs WHERE status IN ('downloading', 'uploading')")
            rows = cur.fetchall()
            count = 0
            for r in rows:
                vid = r["video_id"]
                fp = r["file_path"]
                if fp and os.path.isfile(fp) and os.path.getsize(fp) > 0:
                    cur.execute(
                        "UPDATE jobs SET status = 'downloaded', updated_at = CURRENT_TIMESTAMP WHERE video_id = ?",
                        (vid,),
                    )
                else:
                    cur.execute(
                        "UPDATE jobs SET status = 'queued', file_path = NULL, updated_at = CURRENT_TIMESTAMP WHERE video_id = ?",
                        (vid,),
                    )
                count += 1
            conn.commit()
            return count

    def cancel_queued_jobs(self) -> int:
        """Deletes or cancels all pending 'queued' or 'downloaded' jobs from the database."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM jobs WHERE status IN ('queued', 'downloaded')")
            conn.commit()
            return cur.rowcount

    def reset_failed_job(self, video_id: str) -> bool:
        """
        Resets a specific failed job so it can run again.
        If downloaded file is on disk, sets status='downloaded', else 'queued'.
        """
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT file_path FROM jobs WHERE video_id = ?", (video_id,))
            row = cur.fetchone()
            if not row:
                return False
            fp = row["file_path"]
            if fp and os.path.isfile(fp) and os.path.getsize(fp) > 0:
                new_status = "downloaded"
            else:
                new_status = "queued"
                fp = None
            cur.execute(
                """
                UPDATE jobs
                SET status = ?, retry_count = 0, error = NULL, file_path = ?, updated_at = CURRENT_TIMESTAMP
                WHERE video_id = ?
                """,
                (new_status, fp, video_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def retry_all_failed(self, playlist_url: Optional[str] = None) -> int:
        """
        Resets all failed jobs back to queued or downloaded (if local file exists),
        clearing their error and retry count.
        """
        query = "SELECT video_id, file_path FROM jobs WHERE status = 'failed'"
        params: List[Any] = []
        if playlist_url:
            query += " AND playlist_url = ?"
            params.append(playlist_url)

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall()
            count = 0
            for r in rows:
                vid = r["video_id"]
                fp = r["file_path"]
                if fp and os.path.isfile(fp) and os.path.getsize(fp) > 0:
                    status = "downloaded"
                else:
                    status = "queued"
                    fp = None
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = ?, retry_count = 0, error = NULL, file_path = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE video_id = ?
                    """,
                    (status, fp, vid),
                )
                count += 1
            conn.commit()
            return count

    def get_stats(self, playlist_url: Optional[str] = None) -> Dict[str, int]:
        """Returns counts grouped by job status."""
        query = "SELECT status, COUNT(*) as count FROM jobs"
        params: List[Any] = []
        if playlist_url:
            query += " WHERE playlist_url = ?"
            params.append(playlist_url)
        query += " GROUP BY status;"

        stats = {
            "total": 0,
            "queued": 0,
            "downloading": 0,
            "downloaded": 0,
            "uploading": 0,
            "uploaded": 0,
            "failed": 0,
        }

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            for row in cur.fetchall():
                st = row["status"]
                cnt = row["count"]
                if st in stats:
                    stats[st] = cnt
                stats["total"] += cnt

        return stats

    def get_failed_jobs(self) -> List[Job]:
        """Returns all jobs marked as permanently failed (retry_count >= 3)."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM jobs WHERE status = 'failed' ORDER BY id ASC")
            return [Job(**dict(row)) for row in cur.fetchall()]

    def get_all_jobs(self, playlist_url: Optional[str] = None) -> List[Job]:
        """Returns all jobs ordered by ID."""
        query = "SELECT * FROM jobs"
        params: List[Any] = []
        if playlist_url:
            query += " WHERE playlist_url = ?"
            params.append(playlist_url)
        query += " ORDER BY id ASC;"

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            return [Job(**dict(row)) for row in cur.fetchall()]

    def _update_status(
        self,
        video_id: str,
        status: str,
        file_path: Optional[str] = None,
        telegram_message_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Internal helper to update job state and timestamp."""
        updates = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
        params: List[Any] = [status]

        if file_path is not None:
            updates.append("file_path = ?")
            params.append(file_path)

        if telegram_message_id is not None:
            updates.append("telegram_message_id = ?")
            params.append(telegram_message_id)

        if error is not None:
            updates.append("error = ?")
            params.append(error)

        params.append(video_id)
        sql = f"UPDATE jobs SET {', '.join(updates)} WHERE video_id = ?"

        with self._get_connection() as conn:
            conn.execute(sql, params)
            conn.commit()
