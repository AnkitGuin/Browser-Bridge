import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

scratch = tempfile.TemporaryDirectory(prefix="browserbridge-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{Path(scratch.name) / 'test.db'}")
os.environ["DEMO_IDENTITY"] = "true"
os.environ["TEST_MODE"] = "true"
os.environ["MODEL_API_KEY"] = ""
os.environ["MODEL_NAME"] = ""
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
os.environ["BROWSER_URL"] = f"http://127.0.0.1:{port}"
os.environ["ALLOWED_ORIGINS"] = os.environ["BROWSER_URL"]

from playwright.sync_api import sync_playwright  # noqa: E402

from bridge import config, db, seed  # noqa: E402


@pytest.fixture(scope="session")
def api():
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    with open(Path(scratch.name) / "api.log", "w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "bridge.main:app", "--port", str(port)], stdout=log, stderr=log
        )
        with httpx.Client(base_url=config.BROWSER_URL, trust_env=False, timeout=2) as client:
            for _ in range(70):
                try:
                    if client.get("/health/ready").is_success:
                        break
                except httpx.RequestError:
                    pass
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                time.sleep(0.1)
            else:
                process.terminate()
                log.seek(0)
                pytest.fail("API did not become ready\n" + log.read())
        try:
            yield config.BROWSER_URL
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.fixture
def tenant(api):
    prefix = "test-" + db.uid()[:10]
    seed.provision(prefix, db.now())
    return prefix


@pytest.fixture
def client(api):
    with httpx.Client(base_url=api, trust_env=False, headers={"Origin": api}, timeout=20) as session:
        yield session


@pytest.fixture(scope="session")
def chromium():
    with sync_playwright() as p:
        executable = config.BROWSER_CHROMIUM or p.chromium.executable_path
        # With an only-shell install the full Chrome path may not exist. Launch
        # normally and require success when the E2E gate is explicitly requested.
        try:
            browser = p.chromium.launch(executable_path=config.BROWSER_CHROMIUM)
        except Exception as exc:
            if os.getenv("REQUIRE_BROWSER") == "1":
                pytest.fail(str(exc))
            if config.BROWSER_CHROMIUM or shutil.which("chromium") or Path(executable).exists():
                pytest.fail(
                    "Chromium is present but could not start. Set BROWSER_CHROMIUM to a compatible binary."
                )
            pytest.skip("Install Playwright Chromium to run browser tests")
        else:
            browser.close()
    return config.BROWSER_CHROMIUM


def sign_in(client, tenant, account):
    response = client.post(
        "/api/v1/auth/login", json={"email": f"{account}@{tenant}.test", "password": config.DEV_PASSWORD}
    )
    assert response.status_code == 200, response.text
