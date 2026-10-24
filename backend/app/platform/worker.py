"""Process asset projections, recover expired tasks, and run local diagnostic tasks."""

from __future__ import annotations

import argparse
import os
import logging
import signal
import socket
import threading

from app.platform.config import PlatformSettings
from app.platform.database import PlatformDatabase
from app.platform.models import WorkerHeartbeat, now
from app.platform.projections import LocalFileStore, process_batch
from app.platform.task_service import run_local


def heartbeat(db: PlatformDatabase, worker_id: str, state: str = "idle") -> None:
    with db.sessions.begin() as session:
        item = session.get(WorkerHeartbeat, worker_id)
        if item is None:
            session.add(WorkerHeartbeat(id=worker_id, state=state, last_seen_at=now()))
        else:
            item.state, item.last_seen_at = state, now()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = PlatformSettings.from_env()
    settings.validate()
    db = PlatformDatabase(settings)
    db.require_current()
    worker_id = os.getenv("NOVELOS_WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            heartbeat(db, worker_id)
            processed = 0
            for kind, operation in (
                (
                    "projections",
                    lambda: process_batch(db, LocalFileStore(settings.storage_root)),
                ),
                ("tasks", lambda: int(run_local(db))),
            ):
                try:
                    processed += operation()
                except Exception as exc:
                    logging.getLogger(__name__).error(
                        "worker_step_failed kind=%s type=%s", kind, type(exc).__name__
                    )
            if args.once:
                break
            stop.wait(0.1 if processed else 5)
    finally:
        heartbeat(db, worker_id, "stopped")
        db.engine.dispose()


if __name__ == "__main__":
    main()
