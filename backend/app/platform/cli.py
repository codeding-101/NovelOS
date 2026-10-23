from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

from app.platform.config import PlatformSettings
from app.platform.database import PlatformDatabase
from app.platform.legacy import inspect_legacy
from app.platform.security import create_owner


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
