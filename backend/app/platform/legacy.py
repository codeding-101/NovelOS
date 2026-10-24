"""Read-only inventory for staged legacy migration; never imports old COMPLETED as final."""

from __future__ import annotations

import hashlib
from contextlib import closing
import sqlite3
from pathlib import Path


def inspect_legacy(path: Path) -> dict:
    path = path.resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute(
            "BEGIN"
        )  # One read snapshot across counts and chapter hashes.
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Legacy database integrity check failed")
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"novels", "chapters"} <= names:
            raise ValueError("This is not a supported NovelOS legacy database")
        allowed = (
            "novels",
            "chapters",
            "characters",
            "canon_facts",
            "events",
            "timeline_entries",
            "foreshadowing",
            "foreshadowings",
        )
        counts = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in allowed
            if table in names
        }
        chapters = [
            {
                "legacy_id": row[0],
                "sha256": hashlib.sha256((row[1] or "").encode()).hexdigest(),
                "legacy_status": row[2],
                "target_status": "DRAFT",
            }
            for row in connection.execute(
                "SELECT id, content, status FROM chapters ORDER BY id"
            )
        ]
        return {
            "format_version": 1,
            "mode": "read_only_inventory",
            "counts": counts,
            "chapters": chapters,
            "imports_data": False,
            "next_step": "Use import-legacy with a stable --source-id to import an explicit snapshot",
        }
