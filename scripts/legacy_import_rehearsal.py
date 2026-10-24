"""Verify an explicit old SQLite backup against an isolated new platform database."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import secrets
import sys
import tempfile
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.platform.api import create_app
    from app.platform.config import PlatformSettings
    from app.platform.database import PlatformDatabase
    from app.platform.legacy import inspect_legacy
    from app.platform.legacy_import import import_legacy
    from app.platform.models import AssetRevision, LegacyRecord, ProjectionJob
    from app.platform.projections import LocalFileStore, process_batch
    from app.platform.security import create_owner

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    original_hash = file_hash(source)
    inventory = inspect_legacy(source)
    expected = {row["legacy_id"]: row["sha256"] for row in inventory["chapters"]}
    output = ROOT / "data" / "validation"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="legacy-rehearsal-", dir=output
    ) as directory:
        folder = Path(directory)
        settings = PlatformSettings(
            environment="test",
            master_key=Fernet.generate_key().decode(),
            database_url=f"sqlite:///{(folder / 'platform.db').as_posix()}",
            public_origin="http://testserver",
            storage_root=folder / "files",
        )
        db = PlatformDatabase(settings)
        try:
            db.upgrade()
            password = secrets.token_urlsafe(24)
            with db.sessions.begin() as session:
                create_owner(session, "owner", password)
            report = import_legacy(db, source, "rehearsal-source")
            assert import_legacy(db, source, "rehearsal-source")["reused"]
            with db.sessions() as session:
                mappings = list(
                    session.scalars(
                        select(LegacyRecord).where(
                            LegacyRecord.table_name == "chapters"
                        )
                    )
                )
                assert len(mappings) == len(expected)
                for mapping in mappings:
                    revision = session.get(AssetRevision, mapping.revision_id)
                    assert revision.sha256 == expected[mapping.legacy_id]
                    assert revision.status == "DRAFT"
            store = LocalFileStore(settings.storage_root)
            for _ in range(1000):
                if process_batch(db, store) == 0:
                    break
            with db.sessions() as session:
                assert all(
                    job.state == "done"
                    for job in session.scalars(select(ProjectionJob))
                )
            files_checked = 0
            with TestClient(create_app(settings, db)) as client:
                assert (
                    client.post(
                        "/api/v1/auth/login",
                        json={"username": "owner", "password": password},
                    ).status_code
                    == 200
                )
                for work in report["works"]:
                    response = client.get(f"/api/v1/works/{work['work_id']}/export")
                    assert response.status_code == 200
                    with ZipFile(io.BytesIO(response.content)) as archive:
                        manifest = json.loads(archive.read("manifest.json"))
                        assert manifest["finalization"] is False
                        for entry in manifest["files"]:
                            assert (
                                hashlib.sha256(archive.read(entry["path"])).hexdigest()
                                == entry["sha256"]
                            )
                            files_checked += 1
            assert file_hash(source) == original_hash
            evidence = {
                "result": "passed",
                "source_unchanged": True,
                "chapter_hashes_checked": len(expected),
                "works": len(report["works"]),
                "counts": report["counts"],
                "exported_files_checked": files_checked,
                "skipped_tables": report["skipped_tables"],
                "formal_memory": False,
                "destination": "isolated_temporary_database_removed_after_verification",
            }
            (output / "legacy-import-rehearsal.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(evidence, ensure_ascii=False))
        finally:
            db.engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"Legacy rehearsal failed ({type(exc).__name__}); source was opened read-only.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
