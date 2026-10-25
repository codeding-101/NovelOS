"""Disposable loopback-only Chat Completions fixture; never calls any model."""

from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from types import SimpleNamespace


def completion():
    return {
        "id": "local-fixture-receipt",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "这是本机模拟服务的协议验收文本，不是真实模型生成。",
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


@contextmanager
def model_fixture():
    state = SimpleNamespace(
        response=completion(), status=200, calls=[], on_request=None
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            state.calls.append({"path": self.path, "body": data})
            if state.on_request:
                state.on_request()
            payload = json.dumps(deepcopy(state.response), ensure_ascii=False).encode(
                "utf-8"
            )
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if state.status == 302:
                self.send_header("Location", "/capture")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        state.endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield state
        finally:
            server.shutdown()
            thread.join(timeout=5)
