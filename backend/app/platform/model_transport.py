"""Loopback-only protocol rehearsal. Public model calls are not released."""

import http.client
import json
import socket
import threading
import time
from app.platform.model_protocol import endpoint_parts


def send(endpoint, payload, secret, timeout, *, test_mode=False):
    url, port = endpoint_parts(endpoint, test_loopback=test_mode)
    if (
        not test_mode
        or url.scheme != "http"
        or url.hostname not in {"127.0.0.1", "::1"}
    ):
        raise ValueError(
            "Public model execution is disabled; only loopback test fixtures are supported"
        )
    if (
        not secret
        or not secret.isascii()
        or any(ord(c) < 33 or ord(c) == 127 for c in secret)
    ):
        raise ValueError("Invalid fixture credential format")
    deadline = time.monotonic() + timeout
    # stdlib HTTPConnection does not follow redirects or read environment proxies.
    connection = http.client.HTTPConnection(url.hostname, port, timeout=timeout)
    timer = None
    try:
        connection.connect()
        wire = connection.sock

        def interrupt():
            try:
                wire.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        timer = threading.Timer(remaining, interrupt)
        timer.daemon = True
        timer.start()
        wire.settimeout(remaining)
        connection.request(
            "POST",
            url.path,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + secret,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        response = connection.getresponse()
        if (
            response.status != 200
            or response.getheader("Content-Encoding", "identity") != "identity"
        ):
            raise ValueError(
                "Fixture did not return an uncompressed successful response"
            )
        content = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            chunk = response.read1(65536)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > 4 * 1024 * 1024:
                raise ValueError("Fixture response too large")
        return json.loads(content)
    finally:
        if timer:
            timer.cancel()
        connection.close()
