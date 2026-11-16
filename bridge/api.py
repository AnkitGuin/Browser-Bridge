import asyncio
import base64
import json
import time
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import Field
from sqlalchemy import func, select

from . import auth, config, db, report, runstore
from .contracts import TERMINAL, Decision, Input, Login, Mode, RunInput, State
from .errors import BridgeError

router = APIRouter(prefix="/api/v1")
Human = Annotated[auth.Actor, Depends(auth.human)]
CSRF = [Depends(auth.csrf)]


@router.post("/auth/login", dependencies=CSRF)
def login(payload: Login, response: Response):
    token = auth.login(payload.email, payload.password)
    auth.cookie(response, token)
    return {"authenticated": True}


@router.post("/auth/logout", dependencies=CSRF)
def logout(request: Request, response: Response):
    auth.logout(request)
    response.delete_cookie(config.COOKIE)
    return {"authenticated": False}


@router.get("/auth/me")
def me(principal: Human):
    values = asdict(principal)
    return {key: values[key] for key in ("id", "tenant_id", "email", "name", "role", "teams")}


@router.get("/settings")
def settings(principal: Human):
    return {
        "model": config.MODEL_NAME if config.MODEL_KEY else None,
        "demo_identity": config.DEMO,
        "evidence_retention_seconds": config.RETENTION_SECONDS,
        "mode_default": "hybrid",
        "maximum_run_seconds": 900,
        "maximum_browser_actions": 1000,
    }


def public_run(run):
    cp = run["checkpoint"]
    return {
        **{
            key: run[key]
            for key in (
                "id",
                "goal",
                "mode",
                "status",
                "deadline",
                "created_at",
                "updated_at",
                "control",
                "spec",
                "user_id",
            )
        },
        "summary": {
            "actions": cp.get("actions", 0),
            "model_tokens": cp.get("model_tokens", 0),
            "verified": sum(item["status"] == "VERIFIED" for item in cp.get("results", [])),
            "discovered": len(cp.get("candidates", [])),
            "stop_reason": cp.get("stop_reason"),
        },
    }


def allowed_run(conn, principal, run_id):
    run = db.scoped(conn, db.runs, principal.tenant_id, run_id)
    if not run or (run["user_id"] != principal.id and principal.role != "manager"):
        raise BridgeError("NOT_FOUND", "Run not found", 404)
    return run


@router.post("/runs", status_code=201, dependencies=CSRF)
def create_run(payload: RunInput, principal: Human):
    return {"id": runstore.create(principal, payload)}


@router.get("/runs")
def history(
    principal: Human,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    mode: Mode | None = None,
    status: State | None = None,
    q: str = Query("", max_length=100),
    since: int | None = None,
):
    query = select(db.runs).where(db.runs.c.tenant_id == principal.tenant_id)
    if principal.role != "manager":
        query = query.where(db.runs.c.user_id == principal.id)
    with db.engine.connect() as conn:
        overview = conn.execute(
            select(db.runs.c.status, func.count()).where(query.whereclause).group_by(db.runs.c.status)
        ).all()
        if q:
            query = query.where(db.runs.c.goal.icontains(q, autoescape=True))
        if mode:
            query = query.where(db.runs.c.mode == mode)
        if status:
            query = query.where(db.runs.c.status == status)
        if since is not None:
            query = query.where(db.runs.c.created_at >= since)
        total = conn.scalar(select(func.count()).select_from(query.subquery()))
        rows = conn.execute(
            query.order_by(db.runs.c.created_at.desc(), db.runs.c.id).limit(limit).offset(offset)
        ).mappings()
        return {"items": [public_run(run) for run in rows], "total": total, "status_counts": dict(overview)}


@router.get("/runs/{run_id}")
def detail(run_id: str, principal: Human):
    with db.engine.connect() as conn:
        return public_run(allowed_run(conn, principal, run_id))


def step_data(item):
    return {
        "event_id": item["id"],
        "step_id": item["id"],
        "sequence": item["sequence"],
        "run_id": item["run_id"],
        "timestamp": db.iso(item["created_at"]),
        "event_type": item["event"],
        "state_before": item["before"],
        "state_after": item["after"],
        "correlation_id": item["run_id"],
        "payload": item["payload"],
    }


def get_steps(conn, run_id, after, limit):
    return [
        step_data(item)
        for item in conn.execute(
            select(db.steps)
            .where(db.steps.c.run_id == run_id, db.steps.c.sequence > after)
            .order_by(db.steps.c.sequence)
            .limit(limit)
        ).mappings()
    ]


@router.get("/runs/{run_id}/steps")
def steps(run_id: str, principal: Human, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=250)):
    with db.engine.connect() as conn:
        allowed_run(conn, principal, run_id)
        return {"items": get_steps(conn, run_id, after, limit)}


@router.get("/runs/{run_id}/events")
def stream(request: Request, run_id: str, principal: Human, after: int = Query(0, ge=0)):
    try:
        after = max(after, int(request.headers.get("last-event-id", 0)))
    except ValueError:
        raise BridgeError("VALIDATION_FAILED", "Invalid SSE cursor", 422) from None
    with db.engine.connect() as conn:
        allowed_run(conn, principal, run_id)

    def batch(cursor):
        with db.engine.connect() as conn:
            current_principal = auth.from_session(conn, principal.session_id)
            run = allowed_run(conn, current_principal, run_id)
            return run["status"], get_steps(conn, run_id, cursor, 100)

    async def events():
        cursor, last = after, time.monotonic()
        while not await request.is_disconnected():
            try:
                state, items = await asyncio.to_thread(batch, cursor)
            except BridgeError:
                yield "event: end\ndata: {}\n\n"
                return
            for item in items:
                cursor = item["sequence"]
                yield f"id: {cursor}\ndata: {json.dumps(item)}\n\n"
                last = time.monotonic()
            if state in TERMINAL and len(items) < 100:
                yield "event: end\ndata: {}\n\n"
                return
            if time.monotonic() - last > 12:
                yield ": keep-alive\n\n"
                last = time.monotonic()
            await asyncio.sleep(0.3)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/{action}", dependencies=CSRF)
def control(run_id: str, action: Literal["pause", "resume", "cancel"], principal: Human):
    runstore.control(principal, run_id, action)
    return {"accepted": True}


@router.get("/runs/{run_id}/evidence/{evidence_id}")
def evidence(run_id: str, evidence_id: str, principal: Human):
    with db.engine.connect() as conn:
        allowed_run(conn, principal, run_id)
        item = db.row(
            conn,
            db.evidence,
            db.evidence.c.run_id == run_id,
            db.evidence.c.id == evidence_id,
            db.evidence.c.created_at >= db.now() - config.RETENTION_SECONDS,
        )
        if not item or not item["redacted"]:
            raise BridgeError("NOT_FOUND", "Evidence unavailable", 404)
        return Response(
            base64.b64decode(item["body"]),
            media_type="image/png",
            headers={"ETag": f'"{item["sha256"]}"', "Cache-Control": "private, no-store"},
        )


@router.get("/runs/{run_id}/report")
def export(run_id: str, principal: Human, format: Literal["json", "csv", "md"] = "json"):
    with db.engine.connect() as conn:
        result = report.build(conn, allowed_run(conn, principal, run_id))
    if format == "json":
        return result
    body = report.as_csv(result) if format == "csv" else report.markdown(result)
    return Response(
        body,
        media_type="text/csv" if format == "csv" else "text/markdown",
        headers={"Content-Disposition": f'attachment; filename="browserbridge-{run_id}.{format}"'},
    )


@router.get("/approvals")
def approvals(principal: Human):
    with db.engine.connect() as conn:
        query = (
            select(db.approvals)
            .join(db.runs, db.runs.c.id == db.approvals.c.run_id)
            .where(
                db.runs.c.tenant_id == principal.tenant_id,
                db.approvals.c.status == "pending",
                db.approvals.c.expires > db.now(),
            )
        )
        if principal.role != "manager":
            query = query.where(db.approvals.c.requested_by == principal.id)
        rows = conn.execute(query.order_by(db.approvals.c.expires).limit(100)).mappings()
        return {"items": [dict(row) for row in rows]}


@router.post("/approvals/{approval_id}/decision", dependencies=CSRF)
def decide(approval_id: str, payload: Decision, principal: Human):
    runstore.decide(principal, approval_id, payload)
    return {"accepted": True}


class BenchmarkInput(Input):
    suite: Literal["smoke", "local120"] = "smoke"
    mode: Literal["all", "scripted", "dom", "vision", "hybrid"] = "hybrid"
    seed: int = Field(default=42, ge=0, le=1_000_000_000)
    layout: Literal["all", "base", "renamed", "reordered", "modal", "slow", "masked", "compact"] = "all"


def bench_access(principal):
    if not config.DEMO or principal.role != "manager":
        raise BridgeError("POLICY_DENIED", "The scenario lab requires a local manager", 403)


@router.get("/benchmark/cases")
def cases(principal: Human):
    from benchmarks.cases import generate

    bench_access(principal)
    return {"items": [case.public() for case in generate(42)]}


@router.post("/benchmark/runs", status_code=202, dependencies=CSRF)
def create_benchmark(payload: BenchmarkInput, principal: Human):
    bench_access(principal)
    with db.transaction() as conn:
        owner = db.scoped(conn, db.users, principal.tenant_id, principal.id, lock=True)
        count = conn.scalar(
            select(func.count())
            .select_from(db.bench_jobs)
            .where(db.bench_jobs.c.user_id == owner["id"], db.bench_jobs.c.status.in_(["queued", "running"]))
        )
        if count:
            raise BridgeError("STATE_CONFLICT", "A benchmark is already queued for this identity", 409)
        job_id = db.add(
            conn,
            db.bench_jobs,
            tenant_id=principal.tenant_id,
            user_id=principal.id,
            status="queued",
            options=payload.model_dump(),
            results={},
            created_at=db.now(),
        )
    runstore.wake()
    return {"id": job_id}


@router.get("/benchmark/runs")
def benchmark_history(principal: Human):
    bench_access(principal)
    with db.engine.connect() as conn:
        rows = conn.execute(
            select(db.bench_jobs)
            .where(db.bench_jobs.c.tenant_id == principal.tenant_id)
            .order_by(db.bench_jobs.c.created_at.desc())
            .limit(20)
        ).mappings()
        return {"items": [{k: row[k] for k in ("id", "status", "options", "created_at")} for row in rows]}


@router.get("/benchmark/runs/{job_id}")
@router.get("/benchmark/runs/{job_id}/results")
def benchmark_result(job_id: str, principal: Human):
    bench_access(principal)
    with db.engine.connect() as conn:
        job = db.scoped(conn, db.bench_jobs, principal.tenant_id, job_id)
        if not job:
            raise BridgeError("NOT_FOUND", "Benchmark not found", 404)
        return {key: job[key] for key in ("id", "status", "options", "results", "created_at")}
