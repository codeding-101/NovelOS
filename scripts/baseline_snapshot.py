"""Create and verify a local pre-migration snapshot without changing source data.

Snapshots contain private assets and configuration. Keep them under ignored data/.
SQLite databases use the online backup API, including committed WAL contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def backup_database(source: Path, target: Path) -> None:
    deadline = time.monotonic() + 60

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("Database backup exceeded 60 seconds; retry when idle")

    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst, pages=256, progress=progress)
            if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("Snapshot database integrity check failed")


def create_snapshot(root: Path) -> Path:
    root = root.resolve()
    snapshot_root = root / "data" / "restructure-backups"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = snapshot_root / stamp
    destination.mkdir(parents=True, exist_ok=False)
    tracked = git(root, "ls-files", "-z").decode("utf-8").split("\0")
    untracked = (
        git(root, "ls-files", "--others", "--exclude-standard", "-z")
        .decode("utf-8")
        .split("\0")
    )
    files = {root / name for name in tracked + untracked if name}
    files.update(
        root / name for name in (".env", "backend/.env") if (root / name).is_file()
    )
    files.update(
        path
        for path in (root / "data").rglob("*")
        if path.is_file() and not path.is_relative_to(snapshot_root)
    )
    records = []
    missing = []
    for source in sorted(files):
        if source.name.endswith(("-wal", "-shm", "-journal")):
            continue  # Never treat SQLite sidecars as independently restorable files.
        relative = source.relative_to(root)
        if not source.exists():
            missing.append(relative.as_posix())
            continue
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError(
                f"Snapshot refuses links outside the workspace: {relative}"
            )
        target = destination / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            backup_database(source, target)
            mode = "sqlite-online-backup"
        else:
            before = digest(source)
            shutil.copy2(source, target)
            if before != digest(target) or before != digest(source):
                raise RuntimeError(f"Source changed during snapshot; retry: {relative}")
            mode = "copy"
        records.append(
            {
                "path": relative.as_posix(),
                "sha256": digest(target),
                "size": target.stat().st_size,
                "mode": mode,
            }
        )
    (destination / "working-tree.patch").write_bytes(
        git(root, "diff", "--binary", "HEAD")
    )
    (destination / "index.patch").write_bytes(git(root, "diff", "--cached", "--binary"))
    (destination / "git-status.txt").write_bytes(
        git(root, "status", "--porcelain=v1", "-b")
    )
    git(root, "bundle", "create", str(destination / "repository.bundle"), "--all")
    manifest = {
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "head": git(root, "rev-parse", "HEAD").decode().strip(),
        "branch": git(root, "branch", "--show-current").decode().strip(),
        "files": records,
        "deleted_tracked_paths": missing,
        "metadata": {
            name: digest(destination / name)
            for name in (
                "working-tree.patch",
                "index.patch",
                "git-status.txt",
                "repository.bundle",
            )
        },
        "consistency": "SQLite is an online snapshot; other files are individually verified. "
        "This does not claim a transaction across active legacy file mirrors.",
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    verify_snapshot(destination)
    return destination


def verify_snapshot(destination: Path) -> None:
    destination = destination.resolve()
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    for record in manifest["files"]:
        path = (destination / "files" / record["path"]).resolve()
        if (
            not path.is_relative_to(destination / "files")
            or digest(path) != record["sha256"]
        ):
            raise RuntimeError("Snapshot file checksum mismatch or invalid path")
        if record["mode"] == "sqlite-online-backup":
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
                if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise RuntimeError("Snapshot database integrity check failed")
    for name, expected in manifest["metadata"].items():
        path = (destination / name).resolve()
        if not path.is_relative_to(destination) or digest(path) != expected:
            raise RuntimeError("Snapshot metadata checksum mismatch")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        verify_snapshot(args.verify)
        print("Snapshot integrity verified")
    else:
        snapshot = create_snapshot(Path(__file__).resolve().parents[1])
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        print(
            json.dumps(
                {
                    "snapshot": str(snapshot),
                    "verified_files": len(manifest["files"]),
                    "head": manifest["head"],
                },
                ensure_ascii=False,
            )
        )
