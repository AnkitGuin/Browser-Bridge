import hashlib
import json
import logging
import time

import redis
from sqlalchemy import and_, delete, or_, select, update

from . import config, db
from .contracts import ACTIVE, PARKED, TERMINAL, Mode, State, parse, transition
from .errors import BridgeError

logger = logging.getLogger("browserbridge.run")
LEASE_SECONDS = 90
QUEUE = (
    redis.Redis.from_url(config.REDIS_URL, socket_connect_timeout=1, socket_timeout=3)
    if config.REDIS_URL
    else None
)


def wake():
    if QUEUE:
        try:
            QUEUE.lpush("browserbridge:wake", "work")
            QUEUE.ltrim("browserbridge:wake", 0, 255)
        except redis.RedisError:
            logger.warning("queue_wakeup_failed")


def wait():
    if QUEUE:
        try:
            QUEUE.brpop("browserbridge:wake", timeout=2)
            return
        except redis.RedisError:
            pass
    time.sleep(0.35)


def create(actor, request, clock=None, *, benchmark=False):
    if request.mode == Mode.VISION and not (config.MODEL_NAME and config.MODEL_KEY):
        raise BridgeError("MODEL_ERROR", "Vision requires a configured multimodal provider", 422)
    if request.mode == Mode.SCRIPTED and not benchmark:
        raise BridgeError("POLICY_DENIED", "Scripted mode is available in the scenario lab", 403)
    spec = parse(request, clock or db.now())
    with db.transaction() as conn:
        if spec.kind in {"escalate", "transfer", "reorder"} and actor.role != "manager":
            if spec.kind != "escalate" or actor.role != "operator":
                raise BridgeError("POLICY_DENIED", "This identity lacks the required write permission", 403)
            team = db.row(
                conn, db.teams, db.teams.c.tenant_id == actor.tenant_id, db.teams.c.name == spec.team
            )
            if not team or team["id"] not in actor.teams:
                raise BridgeError(
                    "POLICY_DENIED", "This identity cannot escalate another team's tickets", 403
                )
        stamp = db.now()
        run_id = db.add(
            conn,
            db.runs,
            tenant_id=actor.tenant_id,
            user_id=actor.id,
            goal=request.goal,
            mode=request.mode,
            status=State.CREATED,
            spec=spec.model_dump(mode="json"),
            session_id=actor.session_id,
            checkpoint={},
            deadline=stamp + request.timeout_seconds,
            control="",
            sequence=0,
            created_at=stamp,
            updated_at=stamp,
        )
        run = db.row(conn, db.runs, db.runs.c.id == run_id)
        append(conn, run, State.CREATED, "RUN_CREATED", {"mode": request.mode})
    wake()
    return run_id


def append(conn, run, after, event, payload=None, **values):
    transition(run["status"], after)
    sequence = run["sequence"] + 1
    timestamp = db.now()
    step = db.add(
        conn,
        db.steps,
        run_id=run["id"],
        sequence=sequence,
        event=event,
        before=run["status"],
        after=after,
        payload=payload or {},
        created_at=timestamp,
    )
    conn.execute(
        update(db.runs)
        .where(db.runs.c.id == run["id"])
        .values(status=after, updated_at=timestamp, sequence=sequence, **values)
    )
    logger.info(json.dumps({"event": event, "run_id": run["id"], "sequence": sequence, "state": after}))
    return step


def claim(run_id=None):
    stamp = db.now()
    with db.transaction() as conn:
        stmt = select(db.runs).where(
            db.runs.c.status.in_(ACTIVE),
            or_(db.runs.c.leased_until.is_(None), db.runs.c.leased_until <= stamp),
        )
        if run_id:
            stmt = stmt.where(db.runs.c.id == run_id)
        else:
            stmt = stmt.where(~db.runs.c.tenant_id.startswith("case-"))
        run = (
            conn.execute(
                stmt.order_by(db.runs.c.created_at, db.runs.c.id).limit(1).with_for_update(skip_locked=True)
            )
            .mappings()
            .first()
        )
        if not run:
            return None
        token = db.uid()
        status = State.RECOVERING if run["lease"] else run["status"]
        append(
            conn,
            run,
            status,
            "LEASE_RECOVERED" if run["lease"] else "LEASE_ACQUIRED",
            lease=token,
            leased_until=stamp + LEASE_SECONDS,
        )
        return run["id"], token


def locked(conn, run_id, token):
    run = db.row(conn, db.runs, db.runs.c.id == run_id, lock=True)
    if not run or run["lease"] != token or run["leased_until"] <= db.now():
        raise BridgeError("LEASE_LOST", "This worker no longer owns the run", 409)
    return run


def check_control(run):
    if run["control"] == "cancel":
        raise BridgeError("RUN_CANCELLED", "Operator requested cancellation")
    if run["control"] == "pause":
        raise BridgeError("RUN_PAUSED", "Operator requested a pause")
    if run["deadline"] <= db.now():
        raise BridgeError("BUDGET_EXCEEDED", "Run deadline reached")


def intent_hash(run_id, operation, resource_id, parameters):
    canonical = json.dumps(
        [run_id, operation, resource_id, parameters], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def intent(conn, run, operation, resource_id, parameters):
    key = intent_hash(run["id"], operation, resource_id, parameters)
    existing = db.row(conn, db.intents, db.intents.c.id == key)
    if not existing:
        db.add(
            conn,
            db.intents,
            id=key,
            run_id=run["id"],
            resource_id=resource_id,
            operation=operation,
            parameters=parameters,
            status="PENDING",
            verification={},
            created_at=db.now(),
            updated_at=db.now(),
        )
    return key


def control(actor, run_id, action):
    with db.transaction() as conn:
        run = db.scoped(conn, db.runs, actor.tenant_id, run_id, lock=True)
        if not run or run["user_id"] != actor.id:
            raise BridgeError("NOT_FOUND", "Run not found", 404)
        state = State(run["status"])
        if state in TERMINAL:
            raise BridgeError("STATE_CONFLICT", "This run has ended", 409)
        if action == "resume":
            if state not in {State.PAUSED, State.WAITING_HUMAN_LOGIN}:
                raise BridgeError("STATE_CONFLICT", "Run is not waiting for operator resume", 409)
            append(
                conn,
                run,
                State.CREATED,
                "RUN_RESUMED",
                lease=None,
                leased_until=None,
                control="",
                session_id=actor.session_id,
                checkpoint={key: value for key, value in run["checkpoint"].items() if key != "stop_reason"},
            )
        elif action == "cancel" and (state in PARKED or not run["lease"]):
            append(conn, run, State.CANCELLED, "RUN_CANCELLED", lease=None, leased_until=None, control="")
            conn.execute(
                update(db.approvals)
                .where(db.approvals.c.run_id == run_id, db.approvals.c.status.in_(["pending", "approved"]))
                .values(status="cancelled")
            )
        elif action == "pause" and state not in ACTIVE:
            raise BridgeError("STATE_CONFLICT", "Run must be active to pause", 409)
        else:
            desired = "cancel" if run["control"] == "cancel" else action
            append(conn, run, state, "CONTROL_REQUESTED", {"control": desired}, control=desired)
    wake()


def decide(actor, approval_id, decision):
    if actor.role != "manager":
        raise BridgeError("POLICY_DENIED", "Only an authorized manager can review actions", 403)
    with db.transaction() as conn:
        approval = db.row(conn, db.approvals, db.approvals.c.id == approval_id)
        run = approval and db.scoped(conn, db.runs, actor.tenant_id, approval["run_id"], lock=True)
        if not run:
            raise BridgeError("NOT_FOUND", "Approval not found", 404)
        approval = db.row(conn, db.approvals, db.approvals.c.id == approval_id, lock=True)
        if (
            approval["requested_by"] == actor.id
            or approval["status"] != "pending"
            or approval["expires"] <= db.now()
            or approval["intent_id"] != decision.action_hash
            or run["status"] != State.WAITING_APPROVAL
            or run["deadline"] <= db.now()
        ):
            raise BridgeError(
                "APPROVAL_INVALID", "Approval is stale, mismatched, expired or self-approved", 409
            )
        status = "approved" if decision.decision == "approve" else "denied"
        conn.execute(
            update(db.approvals)
            .where(db.approvals.c.id == approval_id)
            .values(status=status, reason=decision.reason, decision_by=actor.id, decided_at=db.now())
        )
        append(
            conn,
            run,
            State.CREATED,
            "APPROVAL_DECIDED",
            {"approval_id": approval_id, "decision": status},
            lease=None,
            leased_until=None,
            control="",
        )
    wake()


def expire():
    stamp = db.now()
    with db.transaction() as conn:
        stale = (
            conn.execute(
                select(db.runs)
                .where(
                    or_(
                        and_(db.runs.c.status.in_(PARKED), db.runs.c.deadline <= stamp),
                        and_(
                            db.runs.c.status == State.WAITING_APPROVAL,
                            db.runs.c.id.in_(
                                select(db.approvals.c.run_id).where(
                                    db.approvals.c.status == "pending", db.approvals.c.expires <= stamp
                                )
                            ),
                        ),
                    )
                )
                .with_for_update(skip_locked=True)
            )
            .mappings()
            .all()
        )
        for run in stale:
            conn.execute(
                update(db.approvals)
                .where(db.approvals.c.run_id == run["id"], db.approvals.c.status == "pending")
                .values(status="expired")
            )
            if run["deadline"] <= stamp:
                append(
                    conn,
                    run,
                    State.TIMED_OUT,
                    "PARKED_RUN_EXPIRED",
                    {"code": "BUDGET_EXCEEDED"},
                    checkpoint={**run["checkpoint"], "stop_reason": "BUDGET_EXCEEDED"},
                )
            elif run["status"] == State.WAITING_APPROVAL:
                append(conn, run, State.CREATED, "APPROVAL_EXPIRED", lease=None, leased_until=None)
        conn.execute(delete(db.evidence).where(db.evidence.c.created_at < stamp - config.RETENTION_SECONDS))
        conn.execute(delete(db.sessions).where(db.sessions.c.expires < stamp))
