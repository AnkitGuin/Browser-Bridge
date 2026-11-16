import hashlib
import os
import platform
import random
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from sqlalchemy import or_, select, update

from bridge import auth, config, db, report, runstore, seed, workflow
from bridge.contracts import Decision, RunInput, State
from bridge.errors import BridgeError
from bridge.model import PROMPT

from .cases import MODES, generate
from .grading import grade, snapshot

SMOKE = {"SGL-001", "SGL-004", "XAP-001", "LAY-003", "REC-002", "REC-003", "SEC-001", "SEC-004"}


def identity(prefix, role):
    with db.transaction() as conn:
        token = auth.create_session(conn, f"{prefix}-{role}")
        return auth.from_session(conn, auth.digest(token))


def evaluate(case, mode):
    model_on = bool(config.MODEL_KEY and config.MODEL_NAME)
    common = {
        **case.public(),
        "mode": mode,
        "fixture_profile": case.profile,
        "model": config.MODEL_NAME if model_on and mode in {"hybrid", "vision"} else None,
    }
    if mode == "vision" and not model_on:
        return {
            **common,
            "status": "UNAVAILABLE",
            "task_success": None,
            "failure_tags": ["multimodal_provider_unconfigured"],
        }
    prefix = f"case-{db.uid()[:12]}"
    fault = case.fault if case.fault != "worker_restart" else ""
    seed.provision(prefix, case.clock, seed=case.seed, layout=case.layout, fault=fault, profile=case.profile)
    before = snapshot(prefix)
    actor = identity(prefix, case.actor)
    started = time.monotonic()
    actual, decisions, event_names, rejection = None, [], [], None
    run_id = None
    try:
        run_id = runstore.create(
            actor,
            RunInput(goal=case.objective, mode=mode, max_actions=220, approval_required=bool(case.approval)),
            case.clock,
            benchmark=True,
        )
    except BridgeError as exc:
        rejection = exc.code
    if run_id:
        if case.fault == "worker_restart":
            result = subprocess.run(
                [sys.executable, "-m", "benchmarks.crash_worker", run_id],
                check=False,
                timeout=120,
                capture_output=True,
            )
            if result.returncode == 73:
                with db.transaction() as conn:
                    conn.execute(
                        update(db.runs).where(db.runs.c.id == run_id).values(leased_until=db.now() - 1)
                    )
        for _ in range(40):
            lease = runstore.claim(run_id)
            if lease:
                workflow.execute(*lease)
            with db.engine.connect() as conn:
                run = db.row(conn, db.runs, db.runs.c.id == run_id)
                if run["status"] != State.WAITING_APPROVAL:
                    break
                pending = db.row(
                    conn, db.approvals, db.approvals.c.run_id == run_id, db.approvals.c.status == "pending"
                )
            if not pending:
                break
            if case.approval == "expire":
                with db.transaction() as conn:
                    conn.execute(
                        update(db.approvals)
                        .where(db.approvals.c.id == pending["id"])
                        .values(expires=db.now() - 1)
                    )
                runstore.expire()
            else:
                reviewer = identity(prefix, "reviewer")
                runstore.decide(
                    reviewer,
                    pending["id"],
                    Decision(
                        action_hash=pending["intent_id"],
                        decision="deny" if case.approval == "deny" else "approve",
                        reason="Synthetic benchmark review",
                    ),
                )
        with db.engine.connect() as conn:
            run = db.row(conn, db.runs, db.runs.c.id == run_id)
            actual = report.build(conn, run)
            decisions = (
                conn.execute(select(db.approvals).where(db.approvals.c.run_id == run_id)).mappings().all()
            )
            event_names = (
                conn.execute(select(db.steps.c.event).where(db.steps.c.run_id == run_id)).scalars().all()
            )
    result = grade(case, before, snapshot(prefix), actual, decisions, event_names, actor.id, rejection)
    result.update(
        common,
        run_id=run_id,
        wall_ms=round((time.monotonic() - started) * 1000),
        rejection_code=rejection,
        provider_cost=None,
    )
    return result


def percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    low = int(index)
    return round(ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (index - low), 2)


def wilson(k, n):
    if not n:
        return None
    z = 1.959963984540054
    p = k / n
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    radius = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return [round(max(0, center - radius), 4), round(min(1, center + radius), 4)]


def metrics(results):
    groups = defaultdict(list)
    for row in results:
        groups[row["mode"]].append(row)
    summary = {}
    for mode, rows in groups.items():
        ready = [row for row in rows if row["task_success"] is not None]
        successes = [row for row in ready if row["task_success"]]
        n = len(ready)
        opportunities = sum(row["forbidden_opportunities"] for row in ready)
        committed = sum(row["committed_effects"] for row in ready)
        claimed = sum(row["claimed_mutations"] for row in ready)
        recovery = [
            row
            for row in ready
            if row["family"] == "recovery"
            and row["fault"] in {"before_commit", "after_commit", "worker_restart", "rate_limit"}
        ]
        groups_by_category = {}
        for key in ("family", "layout", "fault", "split"):
            groups_by_category[key] = {}
            for label in sorted({row[key] for row in ready}):
                subset = [row for row in ready if row[key] == label]
                groups_by_category[key][label or "none"] = {
                    "n": len(subset),
                    "successes": sum(row["task_success"] for row in subset),
                }
        summary[mode] = {
            "n": n,
            "unavailable": len(rows) - n,
            "successes": len(successes),
            "success_rate": len(successes) / n if n else None,
            "wilson_95": wilson(len(successes), n),
            "recovery": {"n": len(recovery), "recovered": sum(row["task_success"] for row in recovery)},
            "unauthorized": {
                "n": opportunities,
                "effects": sum(row["unauthorized_effects"] for row in ready),
            },
            "incorrect_state": {"n": n, "runs": sum(row["incorrect_state_change"] for row in ready)},
            "duplicates": {"n": committed, "effects": sum(row["duplicate_effects"] for row in ready)},
            "verification": {"n": claimed, "verified": sum(row["verified_effects"] for row in ready)},
            "handoff": {"n": n, "runs": sum(row["handoff"] for row in ready)},
            "policy_denials": sum(row["policy_denials"] for row in ready),
            "wall_ms_p50": percentile([row["wall_ms"] for row in ready], 0.5),
            "wall_ms_p95": percentile([row["wall_ms"] for row in ready], 0.95),
            "actions_p50": percentile([row["browser_actions"] for row in ready], 0.5),
            "actions_p95": percentile([row["browser_actions"] for row in ready], 0.95),
            "successful_actions_p50": percentile([row["browser_actions"] for row in successes], 0.5),
            "successful_actions_p95": percentile([row["browser_actions"] for row in successes], 0.95),
            "model_tokens": sum(row["model_tokens"] for row in ready),
            "provider_cost": None,
            "breakdowns": groups_by_category,
        }
    return summary


def paired(results):
    modes = sorted({row["mode"] for row in results})
    by_mode = {
        mode: {
            row["id"]: row["task_success"]
            for row in results
            if row["mode"] == mode and row["task_success"] is not None
        }
        for mode in modes
    }
    comparisons = []
    for i, left in enumerate(modes):
        for right in modes[i + 1 :]:
            overlap = by_mode[left].keys() & by_mode[right].keys()
            if overlap:
                wins = sum(by_mode[left][key] and not by_mode[right][key] for key in overlap)
                losses = sum(by_mode[right][key] and not by_mode[left][key] for key in overlap)
                comparisons.append(
                    {
                        "left": left,
                        "right": right,
                        "n": len(overlap),
                        "left_only": wins,
                        "right_only": losses,
                        "success_delta": (wins - losses) / len(overlap),
                    }
                )
    return comparisons


def manifest(options):
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.SubprocessError):
        sha = None
    paths = [Path("requirements.lock"), Path("console/package-lock.json")]
    return {
        "options": options,
        "timestamp": db.iso(db.now()),
        "git_sha": sha,
        "app_version": "1.0.0",
        "hardware": {"architecture": platform.machine(), "logical_cpus": os.cpu_count()},
        "lock_hashes": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.exists()
        },
        "prompt_hash": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "model": config.MODEL_NAME or None,
        "budget": {"seconds": 240, "max_actions": 220, "model_tokens": 16000},
    }


def run(options, on_case=lambda results: None, *, existing=()):
    cases = generate(options["seed"])
    if options["suite"] == "smoke":
        cases = [case for case in cases if case.id in SMOKE]
    if options.get("layout", "all") != "all":
        cases = [case for case in cases if case.layout == options["layout"]]
    if not cases:
        raise ValueError("No cases match this suite and layout; use local120 for all layouts.")
    rng = random.Random(options["seed"])
    results = list(existing)
    complete = {(row["id"], row["mode"]) for row in existing}
    for case in cases:
        modes = list(MODES if options["mode"] == "all" else [options["mode"]])
        rng.shuffle(modes)
        for mode in modes:
            if (case.id, mode) not in complete:
                results.append(evaluate(case, mode))
                on_case(results)
    return {
        "manifest": manifest(options),
        "cases": results,
        "metrics": metrics(results),
        "paired": paired(results),
    }


def work_one():
    stamp = db.now()
    with db.transaction() as conn:
        job = (
            conn.execute(
                select(db.bench_jobs)
                .where(
                    or_(
                        db.bench_jobs.c.status == "queued",
                        (db.bench_jobs.c.status == "running") & (db.bench_jobs.c.leased_until <= stamp),
                    )
                )
                .order_by(db.bench_jobs.c.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            .mappings()
            .first()
        )
        if not job:
            return False
        lease = db.uid()
        conn.execute(
            update(db.bench_jobs)
            .where(db.bench_jobs.c.id == job["id"])
            .values(status="running", lease=lease, leased_until=stamp + 300)
        )

    def persist(status, data):
        with db.transaction() as conn:
            result = conn.execute(
                update(db.bench_jobs)
                .where(db.bench_jobs.c.id == job["id"], db.bench_jobs.c.lease == lease)
                .values(status=status, results=data, leased_until=db.now() + 300)
            )
            if not result.rowcount:
                raise BridgeError("LEASE_LOST", "Benchmark lease moved to another worker", 409)

    try:
        result = run(
            job["options"],
            lambda rows: persist("running", {"cases": rows, "metrics": metrics(rows)}),
            existing=job["results"].get("cases", []),
        )
        persist("completed", result)
    except Exception as exc:
        persist(
            "failed", {"error": str(exc) if isinstance(exc, ValueError) else "Benchmark execution failed"}
        )
    return True
