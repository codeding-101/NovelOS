from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

from app.platform.config import PlatformSettings
from app.platform.database import PlatformDatabase
from app.platform.legacy import inspect_legacy
from app.platform.security import create_owner
from app.platform.legacy_import import import_legacy
from app.platform.projections import LocalFileStore, process_batch, rebuild
from app.platform.works import audit, require_work


def main() -> None:
    parser = argparse.ArgumentParser(description="NovelOS platform administration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate-key")
    commands.add_parser("upgrade")
    commands.add_parser("check")
    owner = commands.add_parser("init-owner")
    owner.add_argument("--username", default="owner")
    legacy = commands.add_parser("inspect-legacy")
    legacy.add_argument("database", type=Path)
    legacy.add_argument("--output", type=Path)
    migrate = commands.add_parser("import-legacy")
    migrate.add_argument("database", type=Path)
    migrate.add_argument("--source-id", required=True)
    repair = commands.add_parser("rebuild-projections")
    repair.add_argument("work_id")
    delivery = commands.add_parser("process-projections")
    delivery.add_argument(
        "--limit", type=int, choices=range(1, 1001), default=20, metavar="1..1000"
    )
    args = parser.parse_args()
    if args.command == "generate-key":
        print(Fernet.generate_key().decode())
        return
    if args.command == "inspect-legacy":
        report = inspect_legacy(args.database)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(
            json.dumps(
                {"counts": report["counts"], "mode": report["mode"]}, ensure_ascii=False
            )
        )
        return
    settings = PlatformSettings.from_env()
    settings.validate()
    db = PlatformDatabase(settings)
    try:
        if args.command == "upgrade":
            db.upgrade()
            print(f"Schema ready: {db.require_current()}")
        elif args.command == "check":
            print(f"Schema ready: {db.require_current()}")
        elif args.command == "import-legacy":
            db.require_current()
            try:
                report = import_legacy(db, args.database, args.source_id)
            except Exception as exc:
                # Validation/database exceptions may include novel text: don't dump them to logs.
                known_codes = {
                    "SOURCE_CHANGED_REVIEW_REQUIRED",
                    "ORPHAN_LEGACY_RECORD",
                    "UNSUPPORTED_LEGACY_SCHEMA",
                    "LEGACY_INTEGRITY_FAILED",
                    "LEGACY_SNAPSHOT_LIMIT_EXCEEDED",
                    "INVALID_LEGACY_ID",
                    "UNSUPPORTED_NUL_IN_LEGACY_TEXT",
                    "INVALID_LEGACY_CONTENT",
                    "SOURCE_ID_MUST_BE_STABLE_ASCII_IDENTIFIER",
                }
                reason = (
                    str(exc)
                    if type(exc) is ValueError and str(exc) in known_codes
                    else "IMPORT_ABORTED"
                )
                print(
                    json.dumps(
                        {
                            "error": reason,
                            "type": type(exc).__name__,
                            "message": "Import failed; target transaction rolled back. Check schema, source ID and input limits.",
                        }
                    ),
                    file=sys.stderr,
                )
                raise SystemExit(1) from None
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "rebuild-projections":
            db.require_current()
            with db.sessions.begin() as session:
                require_work(session, args.work_id)
                count = rebuild(session, args.work_id)
                audit(
                    session,
                    "projections.rebuild_requested",
                    args.work_id,
                    args.work_id,
                    queued=count,
                )
            print(json.dumps({"queued": count}))
        elif args.command == "process-projections":
            db.require_current()
            print(
                json.dumps(
                    {
                        "processed": process_batch(
                            db, LocalFileStore(settings.storage_root), args.limit
                        )
                    }
                )
            )
        elif args.command == "init-owner":
            db.require_current()
            password = os.environ.pop(
                "NOVELOS_BOOTSTRAP_PASSWORD", ""
            ) or getpass.getpass("Owner password: ")
            with db.sessions.begin() as session:
                create_owner(session, args.username, password)
            print("Owner initialized")
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    main()
