import time
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    insert,
    select,
)

from . import config

metadata = MetaData()


def uid() -> str:
    return uuid4().hex


def now() -> int:
    return int(time.time())


def iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def idcol():
    return Column("id", String(48), primary_key=True)


def tenant():
    return Column("tenant_id", ForeignKey("tenants.id"), nullable=False)


def required(name, kind=String):
    return Column(name, kind, nullable=False)


def timestamps():
    return required("created_at", Integer), required("updated_at", Integer)


def tenant_fk(field, target):
    return ForeignKeyConstraint(["tenant_id", field], [f"{target}.tenant_id", f"{target}.id"])


tenants = Table("tenants", metadata, idcol(), required("name"), required("status"))
users = Table(
    "users",
    metadata,
    idcol(),
    tenant(),
    required("email"),
    required("name"),
    required("role"),
    required("active", Boolean),
    required("created_at", Integer),
    UniqueConstraint("tenant_id", "id"),
    UniqueConstraint("email"),
)
teams = Table(
    "teams",
    metadata,
    idcol(),
    tenant(),
    required("name"),
    UniqueConstraint("tenant_id", "id"),
    UniqueConstraint("tenant_id", "name"),
)
memberships = Table(
    "memberships",
    metadata,
    tenant(),
    Column("user_id", String(48), primary_key=True),
    Column("team_id", String(48), primary_key=True),
    tenant_fk("user_id", "users"),
    tenant_fk("team_id", "teams"),
)
sessions = Table(
    "sessions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", ForeignKey("users.id"), nullable=False),
    required("expires", Integer),
    Column("run_id", ForeignKey("runs.id")),
    Column("lease", String(48)),
)
customers = Table(
    "customers",
    metadata,
    idcol(),
    tenant(),
    required("external_ref"),
    required("company"),
    Column("contact", String),
    Column("email", String),
    required("tier"),
    required("risk", Boolean),
    required("status"),
    required("note", Text),
    required("version", Integer),
    *timestamps(),
    UniqueConstraint("tenant_id", "id"),
    UniqueConstraint("tenant_id", "external_ref"),
)
tickets = Table(
    "tickets",
    metadata,
    idcol(),
    tenant(),
    Column("customer_id", String(48)),
    required("number"),
    required("title"),
    required("description", Text),
    required("status"),
    required("priority"),
    required("team_id"),
    Column("user_id", String(48)),
    required("queue"),
    required("version", Integer),
    *timestamps(),
    UniqueConstraint("tenant_id", "id"),
    UniqueConstraint("tenant_id", "number"),
    tenant_fk("customer_id", "customers"),
    tenant_fk("team_id", "teams"),
    tenant_fk("user_id", "users"),
    CheckConstraint("status IN ('open','pending','in_progress','resolved','closed','cancelled')"),
    CheckConstraint("priority IN ('high','normal','low')"),
    CheckConstraint("queue IN ('inbox','escalation')"),
)
Index(
    "ticket_filters",
    tickets.c.tenant_id,
    tickets.c.team_id,
    tickets.c.priority,
    tickets.c.status,
    tickets.c.created_at,
)
notes = Table(
    "ticket_notes",
    metadata,
    idcol(),
    tenant(),
    required("ticket_id"),
    required("author_id"),
    required("content", Text),
    required("created_at", Integer),
    tenant_fk("ticket_id", "tickets"),
    tenant_fk("author_id", "users"),
)
products = Table(
    "products",
    metadata,
    idcol(),
    tenant(),
    required("sku"),
    required("name"),
    required("reorder_point", Integer),
    required("active", Boolean),
    UniqueConstraint("tenant_id", "id"),
    UniqueConstraint("tenant_id", "sku"),
    CheckConstraint("reorder_point >= 0"),
)
warehouses = Table(
    "warehouses",
    metadata,
    idcol(),
    tenant(),
    required("name"),
    UniqueConstraint("tenant_id", "id"),
)
stock = Table(
    "stock_levels",
    metadata,
    idcol(),
    tenant(),
    required("product_id"),
    required("warehouse_id"),
    required("quantity", Integer),
    required("reserved", Integer),
    required("version", Integer),
    UniqueConstraint("tenant_id", "product_id", "warehouse_id"),
    tenant_fk("product_id", "products"),
    tenant_fk("warehouse_id", "warehouses"),
    CheckConstraint("quantity >= reserved AND reserved >= 0"),
)
movements = Table(
    "stock_movements",
    metadata,
    idcol(),
    tenant(),
    required("product_id"),
    Column("source", String(48)),
    required("target"),
    required("quantity", Integer),
    required("kind"),
    required("status"),
    required("intent_id"),
    required("created_at", Integer),
    UniqueConstraint("tenant_id", "intent_id"),
    tenant_fk("product_id", "products"),
    tenant_fk("source", "warehouses"),
    tenant_fk("target", "warehouses"),
    CheckConstraint("quantity > 0"),
    CheckConstraint("kind IN ('transfer','reorder')"),
)
audit = Table(
    "business_audit_events",
    metadata,
    idcol(),
    tenant(),
    required("actor_id"),
    required("resource"),
    required("resource_id"),
    required("operation"),
    required("before", JSON),
    required("after", JSON),
    required("created_at", Integer),
    Column("run_id", String(48)),
    Column("intent_id", String(64)),
    tenant_fk("actor_id", "users"),
    UniqueConstraint("intent_id"),
)
runs = Table(
    "runs",
    metadata,
    idcol(),
    tenant(),
    required("user_id"),
    required("goal", Text),
    required("mode"),
    required("status"),
    required("session_id", String(64)),
    required("spec", JSON),
    required("checkpoint", JSON),
    required("deadline", Integer),
    required("control"),
    required("sequence", Integer),
    Column("lease", String(48)),
    Column("leased_until", Integer),
    *timestamps(),
    tenant_fk("user_id", "users"),
)
Index("run_claim", runs.c.status, runs.c.leased_until)
steps = Table(
    "steps",
    metadata,
    idcol(),
    Column("run_id", ForeignKey("runs.id"), nullable=False),
    required("sequence", Integer),
    required("event"),
    required("before"),
    required("after"),
    required("payload", JSON),
    required("created_at", Integer),
    UniqueConstraint("run_id", "sequence"),
)
intents = Table(
    "action_intents",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False, index=True),
    required("resource_id"),
    required("operation"),
    required("parameters", JSON),
    required("status"),
    required("verification", JSON),
    *timestamps(),
)
approvals = Table(
    "approvals",
    metadata,
    idcol(),
    Column("run_id", ForeignKey("runs.id"), nullable=False),
    Column("intent_id", ForeignKey("action_intents.id"), nullable=False, unique=True),
    required("requested_by"),
    Column("decision_by", String),
    required("status"),
    required("expires", Integer),
    Column("decided_at", Integer),
    required("reason"),
    required("details", JSON),
)
evidence = Table(
    "evidence",
    metadata,
    idcol(),
    Column("run_id", ForeignKey("runs.id"), nullable=False, index=True),
    required("kind"),
    required("sha256"),
    required("created_at", Integer),
    required("body", Text),
    required("redacted", Boolean),
    Column("step_id", ForeignKey("steps.id")),
)
bench_jobs = Table(
    "benchmark_runs",
    metadata,
    idcol(),
    tenant(),
    required("user_id"),
    required("status"),
    required("options", JSON),
    required("results", JSON),
    required("created_at", Integer),
    Column("lease", String(48)),
    Column("leased_until", Integer),
)
variants = Table(
    "scenario_variants",
    metadata,
    Column("tenant_id", ForeignKey("tenants.id"), primary_key=True),
    required("layout"),
    required("fault"),
    required("remaining", Integer),
)


def engine_for(url):
    engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def configure(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_mode=WAL")

    return engine


engine = engine_for(config.DATABASE_URL)


@contextmanager
def transaction():
    with engine.connect() as conn:
        if conn.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        with conn.begin() if not conn.in_transaction() else _existing(conn):
            yield conn


@contextmanager
def _existing(conn):
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def row(conn, table, *where, lock=False):
    stmt = select(table).where(*where)
    if lock:
        stmt = stmt.with_for_update()
    return conn.execute(stmt).mappings().first()


def scoped(conn, table, tenant_id, record_id, *, lock=False):
    return row(conn, table, table.c.tenant_id == tenant_id, table.c.id == record_id, lock=lock)


def add(conn, table, **fields):
    if "id" in table.c and "id" not in fields:
        fields["id"] = uid()
    conn.execute(insert(table).values(**fields))
    return fields.get("id")
