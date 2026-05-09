from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SyncedFileRecord:
    suralink_file_id: str
    engagement_id: str
    request_id: str
    file_name: str
    box_file_id: str
    box_folder_id: str


class SyncTracker:
    """
    Simple SQLite-based tracker for files already synced from Suralink to Box.
    """

    def __init__(self, db_path: str = "sync_state.db") -> None:
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS synced_files (
                suralink_file_id TEXT NOT NULL,
                engagement_id TEXT NOT NULL,
                request_id TEXT,
                file_name TEXT,
                box_file_id TEXT,
                box_folder_id TEXT,
                synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (suralink_file_id, engagement_id)
            )
            """
        )
        self.conn.commit()

    def is_synced(self, *, suralink_file_id: str, engagement_id: str) -> bool:
        cursor = self.conn.execute(
            """
            SELECT 1
            FROM synced_files
            WHERE suralink_file_id = ? AND engagement_id = ?
            LIMIT 1
            """,
            (suralink_file_id, engagement_id),
        )
        row = cursor.fetchone()
        return row is not None

    def mark_synced(
        self,
        *,
        suralink_file_id: str,
        engagement_id: str,
        request_id: str,
        file_name: str,
        box_file_id: str,
        box_folder_id: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO synced_files (
                suralink_file_id,
                engagement_id,
                request_id,
                file_name,
                box_file_id,
                box_folder_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                suralink_file_id,
                engagement_id,
                request_id,
                file_name,
                box_file_id,
                box_folder_id,
            ),
        )
        self.conn.commit()

    def list_synced_files_for_engagement(self, *, engagement_id: str) -> list[SyncedFileRecord]:
        cursor = self.conn.execute(
            """
            SELECT
                suralink_file_id,
                engagement_id,
                COALESCE(request_id, ''),
                COALESCE(file_name, ''),
                COALESCE(box_file_id, ''),
                COALESCE(box_folder_id, '')
            FROM synced_files
            WHERE engagement_id = ?
            """,
            (engagement_id,),
        )
        return [
            SyncedFileRecord(
                suralink_file_id=str(row[0]),
                engagement_id=str(row[1]),
                request_id=str(row[2]),
                file_name=str(row[3]),
                box_file_id=str(row[4]),
                box_folder_id=str(row[5]),
            )
            for row in cursor.fetchall()
        ]

    def unmark_synced(self, *, suralink_file_id: str, engagement_id: str) -> None:
        self.conn.execute(
            """
            DELETE FROM synced_files
            WHERE suralink_file_id = ? AND engagement_id = ?
            """,
            (suralink_file_id, engagement_id),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
