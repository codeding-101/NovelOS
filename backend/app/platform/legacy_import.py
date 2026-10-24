"""Explicit, atomic legacy snapshots. No legacy status is granted formal authority."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from sqlalchemy.exc import IntegrityError

from app.platform.models import LegacyImport, LegacyRecord, Work, identifier
from app.platform.works import AssetInput, WorkInput, audit, create_asset

TABLES = (
    "novels",
    "chapters",
    "characters",
    "character_states",
    "relationships",
    "canon_facts",
    "events",
    "timeline_entries",
    "foreshadowings",
    "foreshadowing",
    "world_rules",
    "chapter_plans",
    "commitments",
    "style_profiles",
    "fragments",
)


def canonical(payload) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def read_snapshot(path: Path):
    with closing(
        sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if [tuple(row) for row in connection.execute("PRAGMA integrity_check")] != [
            ("ok",)
        ]:
            raise ValueError("LEGACY_INTEGRITY_FAILED")
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"novels", "chapters"} <= names:
            raise ValueError("UNSUPPORTED_LEGACY_SCHEMA")
        tables, size, count = {}, 0, 0
        for table in TABLES:
            if table not in names:
                continue
            # Identifiers come only from this module's fixed whitelist.
            rows = []
            for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY id'):
                payload = dict(row)
                record_id = payload.get("id")
                if not isinstance(record_id, str) or not 1 <= len(record_id) <= 128:
                    raise ValueError("INVALID_LEGACY_ID")
                encoded = canonical(payload)
                if b"\\u0000" in encoded:
                    raise ValueError("UNSUPPORTED_NUL_IN_LEGACY_TEXT")
                size += len(encoded)
                count += 1
                if size > 100 * 1024 * 1024 or count > 100_000:
                    raise ValueError("LEGACY_SNAPSHOT_LIMIT_EXCEEDED")
                rows.append(payload)
            tables[table] = rows
        skipped = sorted(names - set(TABLES) - {"sqlite_sequence"})
    return tables, skipped


def import_legacy(db, path: Path, source_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", source_id):
        raise ValueError("SOURCE_ID_MUST_BE_STABLE_ASCII_IDENTIFIER")
    tables, skipped = read_snapshot(path)
    fingerprint = hashlib.sha256(canonical(tables)).hexdigest()
    work_ids = {row["id"]: identifier() for row in tables["novels"]}
    report = {
        "source_id": source_id,
        "fingerprint": fingerprint,
        "counts": {name: len(rows) for name, rows in tables.items()},
        "works": [{"legacy_id": old, "work_id": new} for old, new in work_ids.items()],
        "skipped_tables": skipped,
        "target_status": "DRAFT",
        "formal_memory": False,
        "external_files_imported": False,
    }

    def prior(session):
        existing = session.get(LegacyImport, source_id)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise ValueError("SOURCE_CHANGED_REVIEW_REQUIRED")
            return {**existing.report, "reused": True}
        return None

    try:
        with db.sessions.begin() as session:
            result = prior(session)
            if result:
                return result
            # Reserving the source key also serializes simultaneous imports of this source.
            session.add(
                LegacyImport(
                    source_id=source_id, fingerprint=fingerprint, report=report
                )
            )
            session.flush()
            for row in tables["novels"]:
                data = WorkInput(
                    title=row.get("title") or "未命名旧作品",
                    description="从旧库导入；设定待审。",
                )
                session.add(Work(id=work_ids[row["id"]], **data.model_dump()))
            session.flush()
            character_works = {
                row["id"]: row.get("novel_id") for row in tables.get("characters", [])
            }
            for table, rows in tables.items():
                if table == "chapters":
                    rows = sorted(
                        rows,
                        key=lambda row: (
                            str(row.get("novel_id")),
                            row.get("chapter_number") or 0,
                            row["id"],
                        ),
                    )
                for row in rows:
                    novel_id = row["id"] if table == "novels" else row.get("novel_id")
                    if table == "character_states":
                        novel_id = character_works.get(row.get("character_id"))
                    if novel_id not in work_ids:
                        raise ValueError("ORPHAN_LEGACY_RECORD")
                    work_id = work_ids[novel_id]
                    asset_id = revision_id = None
                    if table == "chapters":
                        content = row.get("content")
                        if content is not None and not isinstance(content, str):
                            raise ValueError("INVALID_LEGACY_CONTENT")
                        asset, revision = create_asset(
                            session,
                            work_id,
                            AssetInput(
                                title=row.get("title")
                                or f"第 {row.get('chapter_number', '')} 章",
                                content=content or "",
                                kind="chapter",
                            ),
                        )
                        asset_id, revision_id = asset.id, revision.id
                        if (
                            revision.sha256
                            != hashlib.sha256((content or "").encode()).hexdigest()
                        ):
                            raise ValueError("LEGACY_CONTENT_HASH_MISMATCH")
                    session.add(
                        LegacyRecord(
                            source_id=source_id,
                            table_name=table,
                            legacy_id=row["id"],
                            work_id=work_id,
                            asset_id=asset_id,
                            revision_id=revision_id,
                            sha256=hashlib.sha256(canonical(row)).hexdigest(),
                            payload=row,
                        )
                    )
            for work_id in work_ids.values():
                audit(
                    session,
                    "legacy.imported",
                    work_id,
                    work_id,
                    source_id=source_id,
                    fingerprint=fingerprint,
                )
            session.flush()
        return {**report, "reused": False}
    except IntegrityError:
        # A concurrent winner may have committed while this import waited on its source key.
        with db.sessions() as session:
            result = prior(session)
            if result:
                return result
        raise
