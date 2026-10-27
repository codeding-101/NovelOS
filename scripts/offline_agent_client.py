"""Reference task-scoped Agent client. Diagnostic execution only; never calls a model."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AgentClient:
    def __init__(self, origin, task_id, token, allow_insecure_loopback=False):
        url = urlsplit(origin)
        if (
            url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or not url.hostname
        ):
            raise ValueError("Use a single server origin without credentials or path")
        if url.scheme != "https" and not (
            allow_insecure_loopback
            and url.scheme == "http"
            and url.hostname in {"127.0.0.1", "::1"}
        ):
            raise ValueError(
                "HTTPS required; loopback HTTP requires explicit test flag"
            )
        if not task_id.isalnum() or len(task_id) > 32 or not 32 <= len(token) <= 128:
            raise ValueError("Invalid task or token format")
        self.origin, self.task_id, self.token = origin.rstrip("/"), task_id, token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path="", body=None):
        request = Request(
            self.origin + f"/api/agent/v1/tasks/{self.task_id}" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise RuntimeError("Agent response exceeds size limit")
                return json.loads(data)
        except HTTPError as exc:
            raise RuntimeError(
                f"Agent request rejected ({exc.code}); inspect task status in website"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise RuntimeError(
                "Connection interrupted; keep the state file and retry"
            ) from None

    def status(self):
        return self.request()

    def claim(self):
        return self.request("/claim", {})

    def heartbeat(self, lease):
        return self.request("/heartbeat", lease)

    def checkpoint(self, body):
        return self.request("/checkpoints", body)

    def submit(self, body):
        return self.request("/results", body)


def save_state(path: Path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".agent-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def diagnostic(client: AgentClient, path: Path):
    identity = {
        "origin": client.origin,
        "task_id": client.task_id,
        "grant_hash": hashlib.sha256(client.token.encode()).hexdigest(),
    }
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else identity
    if any(saved.get(key) != value for key, value in identity.items()):
        raise ValueError(
            "State file belongs to another task, origin, or grant; use a new private file"
        )
    # A lost result acknowledgement is retried before claiming, so its lease token stays valid.
    if "result" in saved:
        return client.submit(saved["result"])
    state = client.status()
    if state["state"] in {"PAUSED", "FAILED", "CANCELLED", "SUCCEEDED"}:
        raise RuntimeError("Task requires owner action or is already complete")
    envelope = client.claim()
    context = envelope.get("context")
    if context:
        if (
            envelope["protocol_version"] != "1.1"
            or hashlib.sha256(context["document"].encode("utf-8")).hexdigest()
            != context["sha256"]
        ):
            raise ValueError("Invalid context delivery")
    elif envelope["protocol_version"] != "1.0":
        raise ValueError("Unsupported task protocol")
    lease = {
        "attempt_id": envelope["attempt_id"],
        "lease_token": envelope["lease_token"],
    }
    save_state(path, {**identity, "lease": lease})
    if envelope["task_type"] != "analyze":
        raise RuntimeError(
            "Reference diagnostic client supports analyze only; pause this task in website"
        )
    client.heartbeat(lease)
    stats = {
        "sha256": envelope["input"]["sha256"],
        "characters": len(envelope["input"]["content"]),
        **(
            {"context_sha256": envelope["context"]["sha256"]}
            if envelope.get("context")
            else {}
        ),
    }
    previous = envelope["checkpoint"]
    if (
        not previous
        or previous["step"] != "offline_diagnostic"
        or previous["payload"] != stats
    ):
        sequence = (
            previous["sequence"] + 1
            if previous and previous["attempt_id"] == envelope["attempt_id"]
            else 1
        )
        client.checkpoint(
            {
                **lease,
                "sequence": sequence,
                "step": "offline_diagnostic",
                "payload": stats,
            }
        )
    result = {
        **lease,
        "protocol_version": envelope["protocol_version"],
        **(
            {
                "context_ack": {
                    key: envelope["context"][key] for key in ("snapshot_id", "sha256")
                }
            }
            if envelope.get("context")
            else {}
        ),
        "task_id": client.task_id,
        "executor_id": envelope["executor_id"],
        "submission_id": "offline-diagnostic-v1",
        "result_status": "success",
        "finish_reason": "complete",
        "output_artifacts": [
            {
                "kind": "analyze",
                "title": "离线 Agent 流程校验",
                "content": f"字符数：{stats['characters']}\n输入 SHA-256：{stats['sha256']}\n未调用模型；此结果仅用于协议验收。",
            }
        ],
        "evidence_revision_ids": [envelope["input"]["revision_id"]],
    }
    save_state(path, {**identity, "lease": lease, "result": result})
    return client.submit(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--allow-insecure-loopback", action="store_true")
    args = parser.parse_args()
    token = os.getenv("NOVELOS_AGENT_TOKEN") or getpass.getpass("Task Agent token: ")
    try:
        client = AgentClient(
            args.origin, args.task_id, token, args.allow_insecure_loopback
        )
        receipt = diagnostic(client, args.state)
        print(json.dumps({"disposition": receipt["disposition"], "id": receipt["id"]}))
        return 0 if receipt["disposition"] == "ACCEPTED" else 2
    except (ValueError, RuntimeError, OSError):
        print(
            "Agent run did not complete. Inspect the website and preserve the private state file."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
