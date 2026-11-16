from sqlalchemy import select, update

from . import auth, db
from .contracts import State, Task
from .errors import BridgeError

WRITE_OPERATIONS = {"escalate", "transfer", "reorder"}


def require_app(conn, actor, app):
    if actor.run_id:
        run = db.scoped(conn, db.runs, actor.tenant_id, actor.run_id)
        if not run or app not in run["spec"]["apps"]:
            raise BridgeError("POLICY_DENIED", "Application is outside the run scope", 403)


def ticket_access(actor, ticket, write=False):
    if not ticket or (actor.role == "operator" and ticket["team_id"] not in actor.teams):
        raise BridgeError("NOT_FOUND", "Ticket not found", 404)
    if write and actor.role not in {"operator", "manager"}:
        raise BridgeError("PERMISSION_DENIED", "Ticket write permission required", 403)


def eligible(task, ticket, team_name):
    return (
        team_name == task.team
        and ticket["status"] in task.statuses
        and ticket["priority"] == task.priority
        and ticket["created_at"] < task.created_before
    )


def require_intent(conn, actor, operation, resource_id, key, params, *, always=False):
    if not actor.run_id:
        if always:
            raise BridgeError(
                "APPROVAL_REQUIRED", "Submit this operation through the operator console for approval", 403
            )
        return None
    run = db.scoped(conn, db.runs, actor.tenant_id, actor.run_id, lock=True)
    if (
        not run
        or run["lease"] != actor.lease
        or run["status"] != State.ACTING
        or run["control"]
        or run["deadline"] <= db.now()
        or (run["leased_until"] or 0) <= db.now()
    ):
        raise BridgeError("POLICY_DENIED", "Run does not have an active write grant", 403)
    auth.from_session(conn, run["session_id"])
    item = db.row(conn, db.intents, db.intents.c.id == key, db.intents.c.run_id == run["id"])
    if (
        not item
        or item["operation"] != operation
        or item["resource_id"] != resource_id
        or item["status"] not in {"PENDING", "OUTCOME_UNKNOWN", "COMMITTED_VERIFIED"}
        or item["parameters"] != params
    ):
        raise BridgeError("POLICY_DENIED", "Operation is outside the persisted action intent", 403)
    task = Task.model_validate(run["spec"])
    if task.kind != operation:
        raise BridgeError("POLICY_DENIED", "Operation is outside the authorized task", 403)
    if task.approval_required or always:
        approval = db.row(conn, db.approvals, db.approvals.c.intent_id == key)
        approver = approval and db.scoped(conn, db.users, actor.tenant_id, approval["decision_by"])
        if (
            not approval
            or approval["status"] != "approved"
            or approval["expires"] <= db.now()
            or approval["requested_by"] != actor.id
            or not approver
            or not approver["active"]
            or approver["role"] != "manager"
            or approver["id"] == actor.id
        ):
            raise BridgeError("APPROVAL_REQUIRED", "A current scoped approval is required", 403)
    return task


def write_audit(conn, actor, resource, record_id, operation, before, after, key=None):
    db.add(
        conn,
        db.audit,
        tenant_id=actor.tenant_id,
        actor_id=actor.id,
        resource=resource,
        resource_id=record_id,
        operation=operation,
        before=before,
        after=after,
        run_id=actor.run_id,
        intent_id=key,
        created_at=db.now(),
    )


def consume_fault(conn, actor):
    if not actor.run_id:
        return ""
    variant = db.row(conn, db.variants, db.variants.c.tenant_id == actor.tenant_id, lock=True)
    if not variant or not variant["remaining"]:
        return ""
    conn.execute(update(db.variants).where(db.variants.c.tenant_id == actor.tenant_id).values(remaining=0))
    if variant["fault"] == "session_expiry":
        conn.execute(update(db.sessions).where(db.sessions.c.id == actor.session_id).values(expires=0))
    return variant["fault"]


def ticket_write(actor, record_id, operation, version, *, key="", content="", status=""):
    with db.transaction() as conn:
        require_app(conn, actor, "support")
        if operation == "escalate":
            params = {"queue": "escalation", "version": version}
            task = require_intent(conn, actor, operation, record_id, key, params)
        elif operation in {"note", "status"} and not actor.run_id:
            task = None
        else:
            raise BridgeError("POLICY_DENIED", "Operation is outside the permitted ticket actions", 403)
        ticket = db.scoped(conn, db.tickets, actor.tenant_id, record_id, lock=True)
        ticket_access(actor, ticket, write=True)
        team = db.scoped(conn, db.teams, actor.tenant_id, ticket["team_id"])
        if task and (not ticket["customer_id"] or not eligible(task, ticket, team["name"])):
            raise BridgeError("POLICY_DENIED", "Ticket no longer meets authorized scope", 403)
        if operation == "escalate" and ticket["queue"] == "escalation":
            return ""
        if ticket["version"] != version:
            raise BridgeError("STATE_CONFLICT", "Ticket changed. Reload and review the current version", 409)
        fault = consume_fault(conn, actor)
        if fault in {
            "before_commit",
            "misleading_toast",
            "session_expiry",
            "permission_changed",
            "rate_limit",
        }:
            return fault
        if fault == "version_conflict":
            conn.execute(
                update(db.tickets)
                .where(db.tickets.c.id == record_id)
                .values(version=version + 1, updated_at=db.now(), status="pending")
            )
            return fault
        before = {field: ticket[field] for field in ("queue", "status", "version", "team_id", "customer_id")}
        changes = {"version": version + 1, "updated_at": db.now()}
        if operation == "note":
            if not 1 <= len(content) <= 2000:
                raise BridgeError("VALIDATION_FAILED", "Enter a note of 1 to 2,000 characters", 422)
            db.add(
                conn,
                db.notes,
                tenant_id=actor.tenant_id,
                ticket_id=record_id,
                author_id=actor.id,
                content=content,
                created_at=db.now(),
            )
        elif operation == "status":
            if status not in {"open", "pending", "in_progress", "resolved", "closed", "cancelled"}:
                raise BridgeError("VALIDATION_FAILED", "Unknown status", 422)
            changes["status"] = status
        else:
            changes["queue"] = "escalation"
        conn.execute(update(db.tickets).where(db.tickets.c.id == record_id).values(**changes))
        after = {**before, **{field: value for field, value in changes.items() if field in before}}
        write_audit(conn, actor, "ticket", record_id, operation, before, after, key if actor.run_id else None)
        return fault


def crm_write(actor, record_id, version, note, status):
    if actor.role != "manager" or actor.run_id:
        raise BridgeError("PERMISSION_DENIED", "CRM edits require a human manager", 403)
    if len(note) > 2000 or status not in {"active", "inactive", "on_hold"}:
        raise BridgeError("VALIDATION_FAILED", "Invalid note or account status", 422)
    with db.transaction() as conn:
        item = db.scoped(conn, db.customers, actor.tenant_id, record_id, lock=True)
        if not item:
            raise BridgeError("NOT_FOUND", "Customer not found", 404)
        if item["version"] != version:
            raise BridgeError("STATE_CONFLICT", "Customer changed. Reload before saving", 409)
        conn.execute(
            update(db.customers)
            .where(db.customers.c.id == record_id)
            .values(note=note, status=status, version=version + 1, updated_at=db.now())
        )
        write_audit(
            conn,
            actor,
            "customer",
            record_id,
            "edit",
            {"status": item["status"], "version": version},
            {"status": status, "version": version + 1},
        )


def inventory_write(actor, record_id, kind, source, target, quantity, source_version, target_version, key):
    if actor.role != "manager":
        raise BridgeError("PERMISSION_DENIED", "Inventory writes require a manager", 403)
    if kind not in {"transfer", "reorder"} or not 1 <= quantity <= 10000 or source == target:
        raise BridgeError("VALIDATION_FAILED", "Invalid movement", 422)
    if kind == "reorder":
        source, source_version = None, None
    params = {
        "source": source,
        "target": target,
        "quantity": quantity,
        "source_version": source_version,
        "target_version": target_version,
    }
    with db.transaction() as conn:
        require_app(conn, actor, "inventory")
        task = require_intent(conn, actor, kind, record_id, key, params, always=True)
        product = db.scoped(conn, db.products, actor.tenant_id, record_id)
        source_location = source and db.scoped(conn, db.warehouses, actor.tenant_id, source)
        target_location = db.scoped(conn, db.warehouses, actor.tenant_id, target)
        if (
            not product
            or not product["active"]
            or not target_location
            or (kind == "transfer" and not source_location)
        ):
            raise BridgeError("NOT_FOUND", "Product or warehouse not found", 404)
        if (
            task.reference != product["sku"]
            or task.quantity != quantity
            or task.target != target_location["name"]
            or (source_location and task.source != source_location["name"])
        ):
            raise BridgeError("POLICY_DENIED", "Movement differs from the authorized goal", 403)
        if db.row(
            conn, db.movements, db.movements.c.tenant_id == actor.tenant_id, db.movements.c.intent_id == key
        ):
            return ""
        rows = conn.execute(
            select(db.stock)
            .where(
                db.stock.c.tenant_id == actor.tenant_id,
                db.stock.c.product_id == record_id,
                db.stock.c.warehouse_id.in_([value for value in (source, target) if value]),
            )
            .order_by(db.stock.c.warehouse_id)
            .with_for_update()
        ).mappings()
        levels = {item["warehouse_id"]: item for item in rows}
        start, end = levels.get(source), levels.get(target)
        if not end or (kind == "transfer" and not start):
            raise BridgeError("VALIDATION_FAILED", "Stock location is unavailable", 422)
        if end["version"] != target_version or (start and start["version"] != source_version):
            raise BridgeError("STATE_CONFLICT", "Stock levels changed. Review a new approval", 409)
        if start and start["quantity"] - start["reserved"] < quantity:
            raise BridgeError("VALIDATION_FAILED", "Transfer exceeds unreserved stock", 422)
        fault = consume_fault(conn, actor)
        if fault in {
            "before_commit",
            "misleading_toast",
            "session_expiry",
            "permission_changed",
            "rate_limit",
            "version_conflict",
        }:
            return fault
        before = {item["warehouse_id"]: item["quantity"] for item in levels.values()}
        after = before.copy()
        if start:
            after[source] -= quantity
            after[target] += quantity
            for warehouse_id, item in levels.items():
                conn.execute(
                    update(db.stock)
                    .where(db.stock.c.id == item["id"])
                    .values(quantity=after[warehouse_id], version=item["version"] + 1)
                )
        db.add(
            conn,
            db.movements,
            tenant_id=actor.tenant_id,
            product_id=record_id,
            source=source,
            target=target,
            quantity=quantity,
            kind=kind,
            intent_id=key,
            status="committed" if start else "requested",
            created_at=db.now(),
        )
        write_audit(conn, actor, "product", record_id, kind, before, after, key)
        return fault
