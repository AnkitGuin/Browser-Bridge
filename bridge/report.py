import csv
import io
import json
from collections import Counter

from sqlalchemy import select

from . import db


def build(conn, run):
    checkpoint = run["checkpoint"]
    outcomes = checkpoint.get("results", [])
    ledger = (
        conn.execute(
            select(db.intents)
            .where(db.intents.c.run_id == run["id"])
            .order_by(db.intents.c.created_at, db.intents.c.id)
        )
        .mappings()
        .all()
    )
    counts = Counter(item["status"] for item in outcomes)
    return {
        "run_id": run["id"],
        "mode": run["mode"],
        "status": run["status"],
        "goal": run["goal"],
        "task": run["spec"],
        "totals": {
            "considered": len(outcomes),
            "eligible_discovered": len(checkpoint.get("candidates", [])),
            **dict(counts),
        },
        "outcomes": outcomes,
        "action_ledger": [dict(item) for item in ledger],
        "browser_actions": checkpoint.get("actions", 0),
        "blocked_requests": checkpoint.get("blocked_requests", 0),
        "model_tokens": checkpoint.get("model_tokens", 0),
        "model_latency_ms": checkpoint.get("model_latency_ms", 0),
        "stop_reason": checkpoint.get("stop_reason"),
    }


def markdown(report):
    def escape(value):
        return str(value or "").replace("|", "&#124;").replace("<", "&lt;").replace("\n", " ")

    lines = [
        "# BrowserBridge run",
        "",
        f"Run `{report['run_id']}`",
        "",
        f"Status **{report['status']}**",
        "",
        escape(report["goal"]),
        "",
        f"Considered **{report['totals']['considered']}** records",
        "",
        "| Record | CRM account | Outcome | Prior queue | New queue | Reason | Evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in report["outcomes"]:
        lines.append(
            "| "
            + " | ".join(
                escape(value)
                for value in (
                    item["number"],
                    (item.get("customer") or {}).get("external_ref"),
                    item["status"],
                    item.get("prior_queue"),
                    item.get("new_queue"),
                    item.get("code"),
                    item.get("evidence"),
                )
            )
            + " |"
        )
    unresolved = [
        item for item in report["action_ledger"] if item["status"] in {"PENDING", "OUTCOME_UNKNOWN"}
    ]
    if unresolved:
        lines.extend(["", f"Unreconciled intents **{len(unresolved)}**"])
    if report["stop_reason"]:
        lines.extend(["", f"Stop reason `{report['stop_reason']}`"])
    return "\n".join(lines) + "\n"


def as_csv(report):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(
        [
            "run_id",
            "record_id",
            "reference",
            "status",
            "customer_id",
            "prior_queue",
            "new_queue",
            "reason",
            "evidence",
            "intent_id",
            "details",
        ]
    )
    for item in report["outcomes"]:
        writer.writerow(
            [
                report["run_id"],
                item["id"],
                item["number"],
                item["status"],
                (item.get("customer") or {}).get("id"),
                item.get("prior_queue"),
                item.get("new_queue"),
                item.get("code"),
                item.get("evidence"),
                item.get("intent_id"),
                json.dumps(item.get("record", {})),
            ]
        )
    return stream.getvalue()
