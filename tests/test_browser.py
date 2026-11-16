import os
import subprocess
import sys

import pytest
from conftest import sign_in
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from test_contracts import GOAL

from benchmarks.cases import generate
from benchmarks.harness import evaluate
from bridge import config, db, runstore, workflow
from bridge.browser import Browser
from bridge.errors import BridgeError

pytestmark = pytest.mark.browser


@pytest.mark.parametrize(
    "case_id",
    [
        "XAP-001",
        "LAY-002",
        "LAY-003",
        "LAY-004",
        "LAY-006",
        "REC-001",
        "REC-002",
        "REC-003",
        "REC-004",
        "REC-005",
        "SEC-001",
        "SEC-004",
        "SEC-006",
        "SGL-004",
        "SGL-005",
    ],
)
def test_benchmark_ground_truth(case_id, api, chromium):
    case = next(item for item in generate(42) if item.id == case_id)
    result = evaluate(case, "hybrid")
    assert result["task_success"], result
    assert result["unauthorized_effects"] == 0 and result["duplicate_effects"] == 0


def test_ambiguous_stale_and_disabled_target(api, chromium, tenant):
    with Browser({"support"}, "dom", "synthetic", lambda _: None) as browser:
        browser.page.set_content(
            "<main><h1>Test</h1><button>Escalate</button><button>Escalate</button>"
            "<button disabled>Delete</button><a href='https://attacker.example/'>Unsafe</a></main>"
        )
        with pytest.raises(BridgeError, match="Multiple"):
            browser.resolver.bind(browser.page.get_by_role("button", name="Escalate"))
        with pytest.raises(BridgeError, match="unavailable"):
            browser.resolver.bind(browser.page.get_by_role("button", name="Delete"))
        unique = browser.page.get_by_role("link", name="Unsafe")
        ref = browser.resolver.bind(unique)
        with pytest.raises(BridgeError, match="Observation"):
            browser.resolver.resolve(ref, "obsolete")
        for path in (
            "https://attacker.example/",
            "file:///tmp/example",
            "chrome://settings",
            "/api/v1/auth/me",
        ):
            with pytest.raises(BridgeError):
                browser.open(path)
        assert browser.blocked == 4


def test_evidence_download_and_pause_resume(client, tenant, chromium):
    sign_in(client, tenant, "operator")
    run = client.post("/api/v1/runs", json={"goal": GOAL, "mode": "dom"}).json()["id"]
    assert client.post(f"/api/v1/runs/{run}/pause", json={}).is_success
    workflow.execute(*runstore.claim(run))
    assert client.get(f"/api/v1/runs/{run}").json()["status"] == "PAUSED"
    assert client.post(f"/api/v1/runs/{run}/resume", json={}).is_success
    workflow.execute(*runstore.claim(run))
    report = client.get(f"/api/v1/runs/{run}/report").json()
    assert report["status"] == "COMPLETED"
    evidence_id = report["outcomes"][0]["evidence"]
    screenshot = client.get(f"/api/v1/runs/{run}/evidence/{evidence_id}")
    assert screenshot.content.startswith(b"\x89PNG")
    assert screenshot.headers["cache-control"] == "private, no-store"
    assert client.get(f"/api/v1/runs/{run}/report?format=csv").is_success
    assert "COMPLETED" in client.get(f"/api/v1/runs/{run}/report?format=md").text


def test_operator_console_runs_real_worker(api, tenant, chromium, tmp_path):
    if not config.CONSOLE.exists():
        pytest.fail("Build the React console before running browser tests")
    with (tmp_path / "worker.log").open("w+") as log:
        process = subprocess.Popen([sys.executable, "-m", "bridge.worker"], stdout=log, stderr=log)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=chromium)
                page = browser.new_page(viewport={"width": 1280, "height": 820})
                page.goto(api)
                page.get_by_label("Email").fill(f"operator@{tenant}.test")
                page.get_by_label("Password").fill(config.DEV_PASSWORD)
                page.get_by_role("button", name="Sign in", exact=True).click()
                page.get_by_role("heading", name="Overview", exact=True).wait_for()
                page.locator(".new-side").click()
                page.get_by_role("button", name="Use demo objective").click()
                page.get_by_role("button", name="Start run").click()
                page.get_by_text("completed", exact=True).first.wait_for(timeout=45_000)
                page.get_by_role("img", name="Redacted verification screenshot").wait_for()
                shots = os.getenv("BB_DOC_SCREENSHOTS")
                if shots:
                    from pathlib import Path

                    root = Path(shots)
                    root.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=root / "run-detail.png", animations="disabled")
                    page.get_by_role("button", name="Overview", exact=True).click()
                    page.get_by_role("heading", name="Recent runs").wait_for()
                    page.get_by_role("button", name=GOAL[:10], exact=False).count()
                    page.wait_for_function("document.querySelector('.stats strong')?.textContent === '1'")
                    page.screenshot(path=root / "overview.png", animations="disabled")
                    page.goto(api + "/support")
                    page.get_by_role("heading", name="Support desk").wait_for()
                    page.screenshot(path=root / "support.png", animations="disabled")
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=15)


def test_lease_exclusive(client, tenant):
    sign_in(client, tenant, "operator")
    run_id = client.post("/api/v1/runs", json={"goal": GOAL}).json()["id"]
    first = runstore.claim(run_id)
    assert first is not None and runstore.claim(run_id) is None
    with db.engine.connect() as conn:
        lease = conn.scalar(select(db.runs.c.lease).where(db.runs.c.id == run_id))
    assert lease == first[1]


def test_visual_adapter_sends_pixels_and_hybrid_fallback(api, chromium, monkeypatch):
    import base64
    import json
    import struct

    import httpx

    from benchmarks.harness import identity
    from bridge import auth, seed
    from bridge.browser import DOM, Hybrid, Target
    from bridge.contracts import RunInput
    from bridge.model import MultimodalModel

    tenant = "test-" + db.uid()[:10]
    seed.provision(tenant, db.now(), layout="masked")
    actor = identity(tenant, "operator")
    run = runstore.create(actor, RunInput(goal=GOAL))
    _, lease = runstore.claim(run)
    with db.transaction() as conn:
        token = auth.create_session(conn, actor.id, run_id=run, lease=lease)
    monkeypatch.setattr(config, "MODEL_NAME", "fixture-provider")
    monkeypatch.setattr(config, "MODEL_KEY", "fixture-secret")
    adapter = MultimodalModel(16000, db.now() + 120)
    with Browser({"support"}, "hybrid", token, lambda _: None, adapter) as runtime:
        runtime.open(f"/support/tickets/{tenant}-t1")
        button = runtime.page.locator('form[data-operation="escalate"] button')
        bounds = button.bounding_box()
        target = Target("Escalation submit", ("Escalate ticket", "Send to escalation queue"), button)
        with pytest.raises(BridgeError, match="missing"):
            DOM().choose(runtime, target, runtime.observe())
        requests = []

        def reply(request):
            payload = json.loads(request.content)
            url = payload["messages"][1]["content"][1]["image_url"]["url"]
            raw = base64.b64decode(url.split(",", 1)[1])
            assert raw[:8] == b"\x89PNG\r\n\x1a\n"
            width, height = struct.unpack("!II", raw[16:24])
            assert (width, height) == (1280, 820)
            requests.append(request)
            data = {
                "candidates": [
                    {
                        "label": "Send to escalation queue",
                        "x": (bounds["x"] + bounds["width"] / 2) / width,
                        "y": (bounds["y"] + bounds["height"] / 2) / height,
                        "confidence": 0.95,
                    }
                ]
            }
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(data)}}],
                    "usage": {"total_tokens": 400},
                },
            )

        client_type = httpx.Client
        monkeypatch.setattr(
            httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(reply), **kwargs)
        )
        selection = Hybrid().choose(runtime, target, runtime.observe())
        assert selection["method"] == "hybrid_vision" and selection["disagreement"]
        assert len(requests) == 1 and adapter.tokens == 400


def test_cancel_after_submit_reports_unreconciled_effect(client, tenant, chromium):
    from benchmarks.harness import identity

    sign_in(client, tenant, "operator")
    run_id = client.post("/api/v1/runs", json={"goal": GOAL}).json()["id"]
    owner = identity(tenant, "operator")

    def cancel(run, _):
        runstore.control(owner, run, "cancel")

    workflow.execute(*runstore.claim(run_id), after_submit=cancel)
    result = client.get(f"/api/v1/runs/{run_id}/report").json()
    assert result["status"] == "CANCELLED"
    assert result["outcomes"] == []
    assert [row["status"] for row in result["action_ledger"]] == ["OUTCOME_UNKNOWN"]
    with db.engine.connect() as conn:
        events = conn.execute(select(db.audit.c.intent_id).where(db.audit.c.run_id == run_id)).scalars().all()
    assert len(events) == 1


def test_console_reviews_and_runs_scenario_lab(api, tenant, chromium, tmp_path):
    with (tmp_path / "worker.log").open("w+") as log:
        process = subprocess.Popen([sys.executable, "-m", "bridge.worker"], stdout=log, stderr=log)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=chromium)
                owner, reviewer = [browser.new_page() for _ in range(2)]
                for page, role in ((owner, "manager"), (reviewer, "reviewer")):
                    page.goto(api)
                    page.get_by_label("Email").fill(f"{role}@{tenant}.test")
                    page.get_by_label("Password").fill(config.DEV_PASSWORD)
                    page.get_by_role("button", name="Sign in", exact=True).click()
                    page.get_by_role("heading", name="Overview", exact=True).wait_for()
                owner.locator(".new-side").click()
                owner.get_by_label("Objective").fill("Transfer 2 units of SKU-101 from East to West")
                owner.get_by_role("button", name="Start run").click()
                owner.get_by_text("waiting approval", exact=True).wait_for(timeout=30_000)
                reviewer.get_by_role("button", name="Approvals", exact=False).click()
                reviewer.get_by_role("button", name="Review approval").click()
                reviewer.get_by_role("button", name="Confirm", exact=True).click()
                owner.get_by_text("completed", exact=True).first.wait_for(timeout=30_000)
                reviewer.get_by_role("button", name="Scenario lab").click()
                reviewer.get_by_label("Layout").select_option("modal")
                reviewer.get_by_role("button", name="Run benchmark").click()
                reviewer.get_by_text("LAY-003", exact=True).wait_for(timeout=35_000)
                reviewer.get_by_text("1 / 1 passed", exact=True).wait_for()
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=15)
