import json

import pytest
from conftest import sign_in
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from test_contracts import GOAL

from benchmarks.harness import identity
from bridge import auth, db, runstore, workflow
from bridge.contracts import RunInput, State
from bridge.errors import BridgeError


def test_api_scope_and_direct_writes(client, tenant):
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/support").status_code == 401
    sign_in(client, tenant, "operator")
    own_ticket = f"{tenant}-t1"
    assert client.get(f"/support/tickets/{own_ticket}").status_code == 200
    assert client.get(f"/support/tickets/{tenant}-t5").status_code == 404
    assert client.get("/crm/customers/foreign-c1").status_code == 404
    assert client.get("/inventory/products/foreign-p1").status_code == 404
    assert client.get("/support/tickets/foreign-t1").status_code == 404
    assert (
        client.post(
            f"/crm/customers/{tenant}-c1", data={"version": 1, "note": "x", "status": "inactive"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/inventory/products/{tenant}-p1",
            data={"operation": "transfer", "quantity": 4, "target": f"{tenant}-west", "target_version": 1},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/runs", headers={"Origin": "https://attacker.example"}, json={"goal": GOAL}
        ).json()["code"]
        == "CSRF_REJECTED"
    )
    response = client.post(
        f"/support/tickets/{own_ticket}",
        data={"version": 1, "operation": "note", "content": "<script>untrusted markup</script>"},
        follow_redirects=True,
    )
    assert "&lt;script&gt;" in response.text
    assert (
        client.post(
            f"/support/tickets/{own_ticket}", data={"version": 1, "operation": "escalate"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/support/tickets/{own_ticket}", data={"version": 2, "operation": "escalate"}
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/support/tickets/{own_ticket}", data={"version": 3, "operation": "escalate"}
        ).status_code
        == 303
    )
    with db.engine.connect() as conn:
        effects = conn.execute(select(db.audit).where(db.audit.c.resource_id == own_ticket)).mappings().all()
    assert [effect["operation"] for effect in effects].count("escalate") == 1


def test_crm_manager_edit_and_stale_conflict(client, tenant):
    sign_in(client, tenant, "manager")
    url = f"/crm/customers/{tenant}-c1"
    data = {"version": 1, "note": "Follow up in two days", "status": "on_hold"}
    assert client.post(url, data=data).status_code == 303
    assert client.post(url, data=data).status_code == 409
    assert "Follow up in two days" in client.get(url).text
    assert (
        client.post(f"/crm/customers/{tenant}-c1", data={**data, "version": 2, "status": "root"}).status_code
        == 422
    )


def test_run_cancel_sse_replay_and_cross_tenant(client, tenant):
    sign_in(client, tenant, "operator")
    create = client.post("/api/v1/runs", json={"goal": GOAL, "mode": "dom"})
    assert create.status_code == 201
    run = create.json()["id"]
    assert client.get("/api/v1/runs").json()["total"] == 1
    assert client.post(f"/api/v1/runs/{run}/cancel", json={}).is_success
    result = client.get(f"/api/v1/runs/{run}/events", headers={"Last-Event-ID": "1"})
    items = [
        json.loads(line[6:])
        for line in result.text.splitlines()
        if line.startswith("data: {") and line != "data: {}"
    ]
    assert [event["sequence"] for event in items] == [2]
    assert items[0]["event_type"] == "RUN_CANCELLED"
    assert client.get(f"/api/v1/runs/{run}/steps?after=1&limit=1").json()["items"] == items
    other = "test-" + db.uid()[:10]
    from bridge import seed

    seed.provision(other, db.now())
    sign_in(client, other, "manager")
    assert client.get(f"/api/v1/runs/{run}").status_code == 404
    assert client.get(f"/api/v1/runs/{run}/evidence/fake").status_code == 404
    assert client.get(f"/api/v1/runs/{run}/report").status_code == 404


@pytest.mark.parametrize("action, expected", [("pause", "PAUSED"), ("cancel", "CANCELLED")])
def test_control_requested_while_collecting_approval_evidence(client, tenant, monkeypatch, action, expected):
    sign_in(client, tenant, "manager")
    run_id = (
        client.post("/api/v1/runs", json={"goal": "Transfer 2 units of SKU-101 from East to West"})
        .raise_for_status()
        .json()["id"]
    )
    owner = identity(tenant, "manager")
    execution = workflow.Execution(*runstore.claim(run_id))
    execution.start()
    execution.event(State.PROPOSING, "TEST_INTENT_READY")
    monkeypatch.setattr(execution, "evidence", lambda: runstore.control(owner, run_id, action))
    with pytest.raises(BridgeError) as control:
        execution.approval({"key": "fixture"})
    execution.fail(control.value.code)
    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == expected and run["control"] == ""
    assert client.get("/api/v1/approvals").json()["items"] == []
    if action == "pause":
        assert client.post(f"/api/v1/runs/{run_id}/resume", json={}).is_success
        assert client.get(f"/api/v1/runs/{run_id}").json()["summary"]["stop_reason"] is None


def test_failure_during_verification_preserves_earlier_success(client, tenant):
    sign_in(client, tenant, "operator")
    owner = identity(tenant, "operator")
    run_id = runstore.create(owner, RunInput(goal=GOAL))
    execution = workflow.Execution(*runstore.claim(run_id))
    execution.start()
    execution.complete_item({"id": "first", "number": "T-01"}, "VERIFIED")
    for state in [State.PROPOSING, State.POLICY_CHECK, State.ACTING, State.VERIFYING]:
        execution.event(state, "TEST_NEXT_ACTION")
    execution.fail("BROWSER_ERROR")
    result = client.get(f"/api/v1/runs/{run_id}/report").json()
    assert result["status"] == "PARTIAL_SUCCESS"
    assert result["stop_reason"] == "BROWSER_ERROR"
    assert result["totals"]["VERIFIED"] == 1


def test_intent_and_result_checkpoint_commit_together(tenant, monkeypatch):
    owner = identity(tenant, "operator")
    run_id = runstore.create(owner, RunInput(goal=GOAL))
    execution = workflow.Execution(*runstore.claim(run_id))
    execution.start()
    item = {"id": f"{tenant}-t1", "number": "T-101"}
    pending = execution.prepare(item, {"queue": "escalation", "version": 1}, {})
    for state in [State.POLICY_CHECK, State.ACTING, State.VERIFYING]:
        execution.event(state, "TEST_NEXT_ACTION")

    def fail_write(*args, **kwargs):
        raise SQLAlchemyError("Simulated failure while writing the checkpoint")

    monkeypatch.setattr(runstore, "append", fail_write)
    with pytest.raises(SQLAlchemyError):
        execution.complete_item(item, "VERIFIED")
    with db.engine.connect() as conn:
        saved_run = db.row(conn, db.runs, db.runs.c.id == run_id)
        saved_intent = db.row(conn, db.intents, db.intents.c.id == pending["key"])
        assert saved_run["checkpoint"]["pending"]["key"] == pending["key"]
        assert not saved_run["checkpoint"].get("results")
        assert saved_intent["status"] == "PENDING"


def test_expired_parked_run_times_out(client, tenant):
    sign_in(client, tenant, "operator")
    run_id = client.post("/api/v1/runs", json={"goal": GOAL}).raise_for_status().json()["id"]
    execution = workflow.Execution(*runstore.claim(run_id))
    execution.start()
    execution.fail("RUN_PAUSED")
    with db.transaction() as conn:
        conn.execute(update(db.runs).where(db.runs.c.id == run_id).values(deadline=db.now() - 1))
    runstore.expire()
    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == "TIMED_OUT"
    assert run["summary"]["stop_reason"] == "BUDGET_EXCEEDED"


def test_forged_stale_and_self_approval(client, tenant):
    owner = identity(tenant, "manager")
    run_id = runstore.create(owner, RunInput(goal="Transfer 2 units of SKU-101 from East to West"))
    with db.transaction() as conn:
        run = db.row(conn, db.runs, db.runs.c.id == run_id)
        key = runstore.intent(conn, run, "transfer", f"{tenant}-p1", {"quantity": 2})
        conn.execute(update(db.runs).where(db.runs.c.id == run_id).values(status=State.WAITING_APPROVAL))
        approval = db.add(
            conn,
            db.approvals,
            run_id=run_id,
            intent_id=key,
            requested_by=owner.id,
            status="pending",
            reason="",
            expires=db.now() + 60,
            details={},
        )
    route = f"/api/v1/approvals/{approval}/decision"
    sign_in(client, tenant, "manager")
    assert client.post(route, json={"action_hash": key, "decision": "approve"}).status_code == 409
    sign_in(client, tenant, "operator")
    assert client.post(route, json={"action_hash": key, "decision": "approve"}).status_code == 403
    sign_in(client, tenant, "reviewer")
    assert client.post(route, json={"action_hash": "f" * 64, "decision": "approve"}).status_code == 409
    with db.transaction() as conn:
        conn.execute(update(db.approvals).where(db.approvals.c.id == approval).values(expires=db.now() - 1))
    assert client.post(route, json={"action_hash": key, "decision": "approve"}).status_code == 409
    with db.engine.connect() as conn:
        assert not conn.execute(select(db.movements).where(db.movements.c.tenant_id == tenant)).all()


def test_run_session_cannot_use_console_and_inactive_session(client, tenant):
    sign_in(client, tenant, "operator")
    run_id = client.post("/api/v1/runs", json={"goal": GOAL}).json()["id"]
    _, lease = runstore.claim(run_id)
    with db.transaction() as conn:
        token = auth.create_session(conn, f"{tenant}-operator", run_id=run_id, lease=lease)
    client.cookies.set("bb_session", token)
    assert client.get("/api/v1/auth/me").status_code == 403
    assert client.get("/crm/customers").status_code == 200
    assert client.get("/inventory/products").status_code == 403


def test_two_approved_transfers_cannot_share_one_stock_version(api, tenant):
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    owner = identity(tenant, "manager")
    reviewer = identity(tenant, "reviewer")
    goal = RunInput(goal="Transfer 2 units of SKU-101 from East to West")
    params = {
        "source": f"{tenant}-east",
        "target": f"{tenant}-west",
        "quantity": 2,
        "source_version": 1,
        "target_version": 1,
    }

    def prepare():
        run_id = runstore.create(owner, goal)
        _, lease = runstore.claim(run_id)
        with db.transaction() as conn:
            run = db.row(conn, db.runs, db.runs.c.id == run_id)
            key = runstore.intent(conn, run, "transfer", f"{tenant}-p1", params)
            db.add(
                conn,
                db.approvals,
                run_id=run_id,
                intent_id=key,
                requested_by=owner.id,
                decision_by=reviewer.id,
                status="approved",
                reason="Synthetic concurrency test",
                decided_at=db.now(),
                expires=db.now() + 60,
                details={},
            )
            conn.execute(update(db.runs).where(db.runs.c.id == run_id).values(status=State.ACTING))
            token = auth.create_session(conn, owner.id, run_id=run_id, lease=lease)
        return key, token

    def submit(request):
        key, token = request
        with httpx.Client(
            base_url=api, trust_env=False, cookies={"bb_session": token}, headers={"Origin": api}
        ) as session:
            return session.post(
                f"/inventory/products/{tenant}-p1", data={**params, "operation": "transfer", "intent": key}
            ).status_code

    requests = [prepare(), prepare()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, requests)) == [303, 409]
    with db.engine.connect() as conn:
        assert len(conn.execute(select(db.movements).where(db.movements.c.tenant_id == tenant)).all()) == 1
        actual = (
            conn.execute(select(db.stock).where(db.stock.c.product_id == f"{tenant}-p1")).mappings().all()
        )
        assert sum(row["version"] == 2 for row in actual) == 2
        assert all(row["quantity"] >= row["reserved"] for row in actual)
