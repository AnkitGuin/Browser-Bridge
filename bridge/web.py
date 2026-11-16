import time
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select

from . import auth, business, db
from .errors import BridgeError

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["iso"] = db.iso
Principal = Annotated[auth.Actor, Depends(auth.actor)]


def variant(conn, actor):
    item = db.row(conn, db.variants, db.variants.c.tenant_id == actor.tenant_id)
    return item["layout"] if item else "base"


def render(request, template, principal=None, **context):
    return templates.TemplateResponse(request, template, {"actor": principal, **context})


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html", title="Sign in")


@router.post("/login", dependencies=[Depends(auth.csrf)])
def login_form(
    request: Request,
    email: Annotated[str, Form(max_length=160)],
    password: Annotated[str, Form(max_length=160)],
):
    try:
        token = auth.login(email, password)
    except BridgeError:
        response = render(request, "login.html", title="Sign in", error="Invalid credentials")
        response.status_code = 401
        return response
    response = RedirectResponse("/", 303)
    auth.cookie(response, token)
    return response


def result(request, path, fault):
    errors = {
        "before_commit": (504, "COMMIT_UNCERTAIN", "The connection timed out before saving."),
        "after_commit": (504, "COMMIT_UNCERTAIN", "The connection timed out during confirmation."),
        "session_expiry": (401, "AUTH_EXPIRED", "Sign in with an authorized account."),
        "permission_changed": (403, "PERMISSION_DENIED", "Your write access was revoked."),
        "rate_limit": (429, "APP_NOT_READY", "Please wait before retrying."),
        "version_conflict": (409, "STATE_CONFLICT", "This record changed while you worked."),
    }
    if fault in errors:
        status, code, message = errors[fault]
        raise BridgeError(code, message, status)
    return RedirectResponse(path + "?saved=1", 303)


def page_of(conn, stmt, order_by, page, size, query):
    count = conn.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = conn.execute(stmt.order_by(order_by).offset((page - 1) * size).limit(size)).mappings().all()
    next_query = {**{k: v for k, v in query.items() if k != "page"}, "page": page + 1}
    prev_query = {**next_query, "page": page - 1}
    return {
        "rows": rows,
        "page": page,
        "count": count,
        "next": "?" + urlencode(next_query) if count > page * size else None,
        "previous": "?" + urlencode(prev_query) if page > 1 else None,
    }


@router.get("/support")
@router.get("/support/tickets")
def tickets(
    request: Request,
    principal: Principal,
    page: int = 1,
    q: str = "",
    priority: str = "",
    status: str = "",
    team: str = "",
    before: str = "",
    sort: str = "",
):
    if not 1 <= page <= 1000:
        raise BridgeError("VALIDATION_FAILED", "Invalid page")
    with db.engine.connect() as conn:
        business.require_app(conn, principal, "support")
        layout = variant(conn, principal)
        stmt = (
            select(db.tickets, db.teams.c.name.label("team"))
            .join(db.teams, db.tickets.c.team_id == db.teams.c.id)
            .where(db.tickets.c.tenant_id == principal.tenant_id)
        )
        if principal.role == "operator":
            stmt = stmt.where(db.tickets.c.team_id.in_(principal.teams))
        if priority:
            stmt = stmt.where(db.tickets.c.priority == priority)
        if status:
            stmt = stmt.where(db.tickets.c.status == status)
        if team:
            stmt = stmt.where(db.teams.c.name == team)
        if before:
            from datetime import UTC, date, datetime
            from datetime import time as wall_time

            try:
                cutoff = int(datetime.combine(date.fromisoformat(before), wall_time.min, UTC).timestamp())
            except ValueError as exc:
                raise BridgeError("VALIDATION_FAILED", "Use a valid ISO date") from exc
            stmt = stmt.where(db.tickets.c.created_at < cutoff)
        if q:
            stmt = stmt.where(
                or_(
                    db.tickets.c.number.icontains(q[:100], autoescape=True),
                    db.tickets.c.title.icontains(q[:100], autoescape=True),
                )
            )
        order = (
            db.tickets.c.created_at.desc()
            if sort == "newest"
            else db.tickets.c.number.desc()
            if layout in {"reordered", "compact"}
            else db.tickets.c.number
        )
        result_page = page_of(conn, stmt, order, page, 3 if layout != "compact" else 2, request.query_params)
    if layout == "slow":
        time.sleep(0.25)
    return render(
        request, "list.html", principal, title="Support desk", app="support", layout=layout, **result_page
    )


@router.get("/support/tickets/{record_id}")
def ticket(request: Request, record_id: str, principal: Principal):
    with db.engine.connect() as conn:
        business.require_app(conn, principal, "support")
        item = db.scoped(conn, db.tickets, principal.tenant_id, record_id)
        business.ticket_access(principal, item)
        team = db.scoped(conn, db.teams, principal.tenant_id, item["team_id"])
        customer = db.scoped(conn, db.customers, principal.tenant_id, item["customer_id"])
        notes = (
            conn.execute(
                select(db.notes)
                .where(db.notes.c.tenant_id == principal.tenant_id, db.notes.c.ticket_id == record_id)
                .order_by(db.notes.c.created_at.desc())
            )
            .mappings()
            .all()
        )
        history = (
            conn.execute(
                select(db.audit)
                .where(db.audit.c.tenant_id == principal.tenant_id, db.audit.c.resource_id == record_id)
                .order_by(db.audit.c.created_at.desc())
            )
            .mappings()
            .all()
        )
        layout = variant(conn, principal)
    return render(
        request,
        "ticket.html",
        principal,
        title=item["number"],
        app="support",
        item=item,
        customer=customer,
        team=team,
        notes=notes,
        history=history,
        layout=layout,
    )


@router.post("/support/tickets/{record_id}", dependencies=[Depends(auth.csrf)])
def update_ticket(
    request: Request,
    record_id: str,
    principal: Principal,
    operation: Annotated[Literal["escalate", "note", "status"], Form()],
    version: Annotated[int, Form(ge=1)],
    intent: Annotated[str, Form(max_length=64)] = "",
    content: Annotated[str, Form(max_length=2000)] = "",
    status: Annotated[str, Form(max_length=20)] = "",
):
    fault = business.ticket_write(
        principal, record_id, operation, version, key=intent, content=content.strip(), status=status
    )
    return result(request, f"/support/tickets/{record_id}", fault)


@router.get("/crm")
@router.get("/crm/customers")
def customers(request: Request, principal: Principal, page: int = 1, q: str = "", tier: str = ""):
    if not 1 <= page <= 1000:
        raise BridgeError("VALIDATION_FAILED", "Invalid page")
    with db.engine.connect() as conn:
        business.require_app(conn, principal, "crm")
        layout = variant(conn, principal)
        stmt = select(db.customers).where(db.customers.c.tenant_id == principal.tenant_id)
        if q:
            stmt = stmt.where(
                or_(
                    db.customers.c.company.icontains(q[:100], autoescape=True),
                    db.customers.c.external_ref.icontains(q[:100], autoescape=True),
                )
            )
        if tier:
            stmt = stmt.where(db.customers.c.tier == tier)
        result_page = page_of(
            conn,
            stmt,
            db.customers.c.external_ref.desc() if layout == "reordered" else db.customers.c.external_ref,
            page,
            2 if layout == "compact" else 3,
            request.query_params,
        )
    return render(
        request, "list.html", principal, title="Customer directory", app="crm", layout=layout, **result_page
    )


@router.get("/crm/customers/{record_id}")
def customer(request: Request, record_id: str, principal: Principal):
    with db.engine.connect() as conn:
        business.require_app(conn, principal, "crm")
        item = db.scoped(conn, db.customers, principal.tenant_id, record_id)
        if not item:
            raise BridgeError("NOT_FOUND", "Customer not found", 404)
        layout = variant(conn, principal)
    return render(
        request, "customer.html", principal, title=item["company"], app="crm", item=item, layout=layout
    )


@router.post("/crm/customers/{record_id}", dependencies=[Depends(auth.csrf)])
def update_customer(
    record_id: str,
    principal: Principal,
    version: Annotated[int, Form(ge=1)],
    note: Annotated[str, Form(max_length=2000)],
    status: Annotated[str, Form(max_length=20)],
):
    business.crm_write(principal, record_id, version, note, status)
    return RedirectResponse(f"/crm/customers/{record_id}?saved=1", 303)


@router.get("/inventory")
@router.get("/inventory/products")
def inventory(request: Request, principal: Principal, page: int = 1, q: str = ""):
    if not 1 <= page <= 1000:
        raise BridgeError("VALIDATION_FAILED", "Invalid page")
    with db.engine.connect() as conn:
        business.require_app(conn, principal, "inventory")
        layout = variant(conn, principal)
        stmt = select(db.products).where(
            db.products.c.tenant_id == principal.tenant_id, db.products.c.active.is_(True)
        )
        if q:
            stmt = stmt.where(
                or_(
                    db.products.c.sku.icontains(q[:100], autoescape=True),
                    db.products.c.name.icontains(q[:100], autoescape=True),
                )
            )
        result_page = page_of(
            conn,
            stmt,
            db.products.c.sku.desc() if layout == "reordered" else db.products.c.sku,
            page,
            2 if layout == "compact" else 3,
            request.query_params,
        )
    return render(
        request,
        "list.html",
        principal,
        title="Inventory catalog",
        app="inventory",
        layout=layout,
        **result_page,
    )


@router.get("/inventory/products/{record_id}")
def product(request: Request, record_id: str, principal: Principal):
    with db.engine.connect() as conn:
        business.require_app(conn, principal, "inventory")
        item = db.scoped(conn, db.products, principal.tenant_id, record_id)
        if not item:
            raise BridgeError("NOT_FOUND", "Product not found", 404)
        layout = variant(conn, principal)
        levels = (
            conn.execute(
                select(db.stock, db.warehouses.c.name)
                .join(db.warehouses, db.warehouses.c.id == db.stock.c.warehouse_id)
                .where(db.stock.c.tenant_id == principal.tenant_id, db.stock.c.product_id == record_id)
                .order_by(db.warehouses.c.name.desc() if layout == "reordered" else db.warehouses.c.name)
            )
            .mappings()
            .all()
        )
        history = (
            conn.execute(
                select(db.movements)
                .where(
                    db.movements.c.tenant_id == principal.tenant_id, db.movements.c.product_id == record_id
                )
                .order_by(db.movements.c.created_at.desc(), db.movements.c.id)
            )
            .mappings()
            .all()
        )
    return render(
        request,
        "inventory.html",
        principal,
        title=item["name"],
        app="inventory",
        item=item,
        levels=levels,
        history=history,
        layout=layout,
    )


@router.post("/inventory/products/{record_id}", dependencies=[Depends(auth.csrf)])
def update_inventory(
    request: Request,
    record_id: str,
    principal: Principal,
    operation: Annotated[Literal["transfer", "reorder"], Form()],
    quantity: Annotated[int, Form(ge=1, le=10000)],
    target: Annotated[str, Form(max_length=48)],
    target_version: Annotated[int, Form(ge=1)],
    intent: Annotated[str, Form(max_length=64)] = "",
    source: Annotated[str, Form(max_length=48)] = "",
    source_version: Annotated[str, Form(pattern=r"^\d{0,9}$")] = "",
):
    fault = business.inventory_write(
        principal,
        record_id,
        operation,
        source or None,
        target,
        quantity,
        int(source_version) if source_version else None,
        target_version,
        intent,
    )
    return result(request, f"/inventory/products/{record_id}", fault)
