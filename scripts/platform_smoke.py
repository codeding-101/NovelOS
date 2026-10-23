"""Exercise the M1 website in an isolated database and headless browser.

Requires backend/requirements-browser.txt and an installed Chromium browser.
Does not load or alter the legacy database, .env files or existing servers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def wait_url(url: str, process: subprocess.Popen, seconds: int = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Smoke service exited before readiness")
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, TimeoutError):
            pass
        time.sleep(0.5)
    raise TimeoutError(f"Smoke service did not become ready: {url}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> None:
    from cryptography.fernet import Fernet
    from playwright.sync_api import sync_playwright, expect
    from app.platform.config import PlatformSettings
    from app.platform.database import PlatformDatabase
    from app.platform.security import create_owner

    output = ROOT / "data" / "validation"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="platform-smoke-", dir=output, ignore_cleanup_errors=True
    ) as temp:
        folder = Path(temp)
        api_port, web_port = free_port(), free_port()
        origin = f"http://127.0.0.1:{web_port}"
        password = secrets.token_urlsafe(24)
        settings = PlatformSettings(
            environment="test",
            master_key=Fernet.generate_key().decode(),
            database_url=f"sqlite:///{(folder / 'platform.db').as_posix()}",
            public_origin=origin,
        )
        db = PlatformDatabase(settings)
        db.upgrade()
        with db.sessions.begin() as session:
            create_owner(session, "owner", password)
        db.engine.dispose()
        env = {
            **os.environ,
            "NOVELOS_ENV": "test",
            "NOVELOS_DATABASE_URL": settings.database_url,
            "NOVELOS_MASTER_KEY": settings.master_key,
            "NOVELOS_PUBLIC_ORIGIN": origin,
            "NOVELOS_BACKEND_URL": f"http://127.0.0.1:{api_port}",
            "NEXT_TELEMETRY_DISABLED": "1",
        }
        processes = []
        logs = []

        def start(command: list[str], cwd: Path, label: str):
            log = (folder / f"{label}.log").open("wb")
            logs.append(log)
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            processes.append(process)
            return process

        api_command = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.platform.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(api_port),
            "--no-proxy-headers",
        ]
        try:
            api = start(api_command, ROOT / "backend", "api")
            wait_url(f"http://127.0.0.1:{api_port}/readyz", api)
            web = start(
                [
                    "node",
                    "node_modules/next/dist/bin/next",
                    "dev",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(web_port),
                ],
                ROOT / "frontend",
                "web",
            )
            wait_url(origin, web)
            browser_path = os.getenv("NOVELOS_BROWSER_PATH")
            if not browser_path and os.name == "nt":
                browser_path = next(
                    (
                        str(path)
                        for path in [
                            Path(
                                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
                            ),
                            Path(
                                r"C:\Program Files\Google\Chrome\Application\chrome.exe"
                            ),
                        ]
                        if path.exists()
                    ),
                    None,
                )
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True, executable_path=browser_path
                )
                context = browser.new_context(viewport={"width": 1440, "height": 1100})
                page = context.new_page()
                page.goto(origin)
                page.get_by_label("用户名").fill("owner")
                page.get_by_label("密码", exact=True).fill(password)
                page.get_by_role("button", name="进入工作空间").click()
                expect(
                    page.get_by_role("heading", name="创作环境已建立")
                ).to_be_visible()
                assert any(
                    cookie["name"] == "novelos_session" and cookie["httpOnly"]
                    for cookie in context.cookies()
                )
                page.get_by_label("名称", exact=True).fill("浏览器验证凭证")
                page.get_by_label("密钥", exact=True).fill(
                    "smoke-secret-not-for-production"
                )
                page.get_by_role("button", name="加密保存").click()
                expect(
                    page.get_by_text("凭证已加密保存。", exact=False)
                ).to_be_visible()
                expect(page.get_by_label("密钥", exact=True)).to_have_value("")
                api.terminate()
                api.wait(timeout=15)
                api = start(api_command, ROOT / "backend", "api-restart")
                wait_url(f"http://127.0.0.1:{api_port}/readyz", api)
                page.reload()
                expect(page.get_by_text("浏览器验证凭证", exact=True)).to_be_visible()
                page.screenshot(
                    path=str(output / "platform-desktop.png"), full_page=True
                )
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(
                    path=str(output / "platform-mobile.png"), full_page=True
                )
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                page.get_by_role("button", name="删除凭证 浏览器验证凭证").click()
                expect(
                    page.get_by_text("还没有保存服务凭证。你可以稍后添加。")
                ).to_be_visible()
                page.get_by_role("button", name="退出登录").click()
                expect(page.get_by_role("heading", name="登录 NovelOS")).to_be_visible()
                assert context.request.get(origin + "/api/v1/runtime").status == 401
                browser.close()
            (output / "platform-browser.json").write_text(
                json.dumps(
                    {
                        "result": "passed",
                        "checks": [
                            "login",
                            "http_only_session",
                            "encrypted_credential_form",
                            "api_restart_persists_session_and_credential",
                            "mobile_no_horizontal_overflow",
                            "delete",
                            "logout",
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(
                "PASS: browser login, credential management, restart persistence, mobile layout and logout"
            )
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            capture_output=True,
                        )
                    else:
                        process.terminate()
                    process.wait(timeout=15)
            for log in logs:
                log.close()
            # Windows child processes may release inherited log handles after the parent exits.
            # Check both listeners are gone; leave only ignored test artifacts if deletion lags.
            deadline = time.monotonic() + 10
            while True:
                listening = False
                for port in (api_port, web_port):
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            listening = True
                    except OSError:
                        pass
                if not listening:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("A smoke service did not release its test port")
                time.sleep(0.25)


if __name__ == "__main__":
    main()
