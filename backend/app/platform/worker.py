"""Independent M1 worker lifecycle. Task claiming is introduced in M3."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import threading

from app.platform.config import PlatformSettings
from app.platform.database import PlatformDatabase
from app.platform.models import WorkerHeartbeat, now


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
            if args.once:
                break
            stop.wait(15)
    finally:
        heartbeat(db, worker_id, "stopped")
        db.engine.dispose()


if __name__ == "__main__":
    main()
