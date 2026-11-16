import base64
import hashlib
import logging
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from sqlalchemy import delete, update
from sqlalchemy.exc import SQLAlchemyError

from . import auth, config, db, runstore, telemetry
from .browser import Browser, Target, scrub
from .contracts import State, Task
from .errors import BridgeError
from .model import MultimodalModel

log = logging.getLogger("browserbridge.workflow")
ROUTES = {
    "customer": "/crm/customers",
    "ticket": "/support/tickets",
    "stock": "/inventory/products",
    "transfer": "/inventory/products",
    "reorder": "/inventory/products",
}
PATHS = {"customer": "/crm/customers/", "ticket": "/support/tickets/", "product": "/inventory/products/"}


class Parked(Exception):
    pass


def record_id(path, expected):
    if not path or not urlsplit(path).path.startswith(PATHS[expected]):
        raise BridgeError("TARGET_AMBIGUOUS", "Linked record type does not match task")
    return PurePosixPath(urlsplit(path).path).name


def fields(page, resource):
    return page.locator(f'[data-resource="{resource}"] [data-field]').evaluate_all(
        "elements => Object.fromEntries(elements.map(e=>[e.dataset.field,e.textContent.trim()]))"
    )


def stock_fields(page):
    rows = page.locator("[data-warehouse]").evaluate_all(
        "els=>els.map(e=>[e.dataset.warehouse,...Array.from(e.cells,c=>c.textContent.trim())])"
    )
    return {
        name: {"id": identifier, "quantity": int(qty), "reserved": int(reserved), "version": int(version)}
        for identifier, name, qty, reserved, version in rows
    }


class Execution:
    def __init__(self, run_id, lease, after_submit=None):
        self.run_id = run_id
        self.lease = lease
        self.after_submit = after_submit
        self.cp = {}
        self.model = None
        self.browser = None

    def event(self, state, event, payload=None, *, release=False, control_check=False, intent_result=None):
        with db.transaction() as conn:
            run = runstore.locked(conn, self.run_id, self.lease)
            if control_check:
                runstore.check_control(run)
            if intent_result:
                self.update_intent(conn, *intent_result)
            if self.model:
                self.cp["model_tokens"] = self.model.tokens
                self.cp["model_latency_ms"] = self.model.latency_ms
            if self.browser:
                self.cp["blocked_requests"] = self.browser.blocked + self.blocked_before
            runstore.append(
                conn,
                run,
                state or run["status"],
                event,
                payload,
                checkpoint=self.cp,
                lease=None if release else self.lease,
                leased_until=None if release else db.now() + runstore.LEASE_SECONDS,
                **({"control": ""} if release else {}),
            )

    def consume(self, operation):
        actions = self.cp.get("actions", 0)
        if actions >= self.task.max_actions:
            raise BridgeError("BUDGET_EXCEEDED", "Browser action budget exhausted")
        self.cp["actions"] = actions + 1
        self.event(None, "BROWSER_ACTION", {"operation": operation}, control_check=True)

    def start(self):
        with db.transaction() as conn:
            run = runstore.locked(conn, self.run_id, self.lease)
            self.cp = dict(run["checkpoint"])
            self.blocked_before = self.cp.get("blocked_requests", 0)
            self.task = Task.model_validate(run["spec"])
            self.mode = run["mode"]
            self.deadline = run["deadline"]
            self.initial = run["status"]
            runstore.check_control(run)
            user = auth.from_session(conn, run["session_id"])
            self.actor_id = user.id
            self.tenant_id = user.tenant_id
            token = auth.create_session(
                conn, user.id, run_id=self.run_id, lease=self.lease, expires=self.deadline
            )
            self.browser_session = auth.digest(token)
        if self.initial == State.CREATED:
            self.event(State.VALIDATING, "VALIDATING_TASK", control_check=True)
            self.event(State.PLANNING, "PLAN_ACCEPTED", {"kind": self.task.kind, "apps": self.task.apps})
        self.event(State.OBSERVING, "OBSERVING", control_check=True)
        if self.mode in {"vision", "hybrid"} and config.MODEL_KEY and config.MODEL_NAME:
            self.model = MultimodalModel(self.task.model_tokens, self.deadline)
            self.model.tokens = self.cp.get("model_tokens", 0)
            self.model.latency_ms = self.cp.get("model_latency_ms", 0)
        return token

    def run(self):
        try:
            token = self.start()
            with Browser(self.task.apps, self.mode, token, self.consume, self.model) as self.browser:
                if self.task.kind == "escalate":
                    self.escalations()
                else:
                    self.single()
                rows = self.cp.get("results", [])
                good = sum(row["status"] in {"VERIFIED", "NOOP"} for row in rows)
                state = (
                    State.COMPLETED if good == len(rows) else State.PARTIAL_SUCCESS if good else State.FAILED
                )
                self.event(
                    state,
                    "RUN_FINISHED",
                    {"verified": good, "considered": len(rows)},
                    release=True,
                    control_check=True,
                )
        except Parked:
            pass
        except (BridgeError, PlaywrightError) as exc:
            code = exc.code if isinstance(exc, BridgeError) else "BROWSER_ERROR"
            self.fail(code)
        except SQLAlchemyError:
            # The next lease resumes the last committed checkpoint.
            raise
        except Exception:
            log.exception("workflow_failed run_id=%s", self.run_id)
            self.fail("INTERNAL_ERROR")
        finally:
            if hasattr(self, "browser_session"):
                with db.transaction() as conn:
                    conn.execute(delete(db.sessions).where(db.sessions.c.id == self.browser_session))

    def fail(self, code):
        if code == "LEASE_LOST":
            return
        state = {
            "AUTH_EXPIRED": State.WAITING_HUMAN_LOGIN,
            "RUN_PAUSED": State.PAUSED,
            "RUN_CANCELLED": State.CANCELLED,
            "BUDGET_EXCEEDED": State.TIMED_OUT,
        }.get(code)
        if state is None:
            state = (
                State.PARTIAL_SUCCESS
                if any(row["status"] in {"VERIFIED", "NOOP"} for row in self.cp.get("results", []))
                else State.FAILED
            )
        self.cp["stop_reason"] = "NEEDS_HUMAN_LOGIN" if code == "AUTH_EXPIRED" else code
        try:
            self.event(state, "RUN_STOPPED", {"code": self.cp["stop_reason"]}, release=True)
        except BridgeError as exc:
            if exc.code != "LEASE_LOST":
                raise

    @property
    def page(self):
        return self.browser.page

    def observe(self, resource=None):
        item = self.browser.observe().public()
        self.event(
            None,
            "OBSERVATION_CAPTURED",
            {
                "url": item["url"],
                "title": scrub(item["title"]),
                "observation_id": item["observation_id"],
                "resource_id": resource,
                "semantic_elements": item["semantic_elements"][:18],
            },
        )
        return item

    def click(self, target, *, mutation=False, navigation=True):
        def selected(obs, selection):
            self.event(
                None,
                "TARGET_SELECTED",
                {
                    "url": obs.url,
                    "observation_id": selection["observation_id"],
                    **{
                        key: selection[key] for key in ("method", "candidate_count", "disagreement", "reason")
                    },
                    "point": selection.get("point"),
                    "mutation": mutation,
                },
                control_check=True,
            )

        return self.browser.click(target, before_click=selected, navigation=navigation)

    def open(self, path):
        self.browser.execute({"kind": "navigate", "route": path})

    def search(self, ref):
        search = self.page.get_by_role("search")
        self.browser.field(search.get_by_role("textbox", name="Search"), "fill", ref)
        self.click(Target("the Search button in the search form", ("Search",), search.get_by_role("button")))

    def list_record(self, ref):
        self.search(ref)
        matches = self.page.locator("[data-record]").get_by_role("link", name=ref, exact=True)
        if matches.count() != 1:
            raise BridgeError(
                "TARGET_AMBIGUOUS" if matches.count() > 1 else "NOT_FOUND",
                "Expected one matching stable reference",
            )
        self.click(Target(f"the record link labeled {ref}", (ref,), matches, "link"))
        return urlsplit(self.page.url).path

    def enumerate(self):
        if self.cp.get("enumerated"):
            return self.cp.get("candidates", [])
        candidates = {entry["id"]: entry for entry in self.cp.get("candidates", [])}
        start = self.cp.get("list_path", "/support/tickets")
        self.open(start)
        visited = set()
        while True:
            path = urlsplit(self.page.url)
            address = path.path + ("?" + path.query if path.query else "")
            if address in visited:
                raise BridgeError("TARGET_AMBIGUOUS", "Pagination loop detected")
            visited.add(address)
            self.observe()
            rows = self.page.locator("tr[data-record]").evaluate_all(
                """els=>els.map(e=>Object.fromEntries([...Array.from(e.cells,c=>[c.dataset.field,c.textContent.trim()]),
                    ['path',e.querySelector('a')?.getAttribute('href')]]))"""
            )
            for row in rows:
                if (
                    row["team"] != self.task.team
                    or row["priority"] != self.task.priority
                    or row["status"] not in self.task.statuses
                    or datetime.fromisoformat(row["created_at"]).timestamp() >= self.task.created_before
                ):
                    continue
                identifier = record_id(row["path"], "ticket")
                candidates[identifier] = {"id": identifier, "path": row["path"], "number": row["number"]}
            next_link = self.page.locator('nav[aria-label="Pagination"] a[rel="next"]')
            count = next_link.count()
            next_path = next_link.get_attribute("href") if count == 1 else None
            if count > 1 or (count == 1 and not next_path):
                raise BridgeError("TARGET_AMBIGUOUS", "Expected one valid next-page link")
            self.cp.update(candidates=list(candidates.values()), list_path=address, enumerated=not next_path)
            self.event(
                None,
                "ENUMERATION_CHECKPOINT",
                {"candidates": len(candidates), "page": address},
                control_check=True,
            )
            if not next_path:
                return self.cp["candidates"]
            self.click(
                Target(
                    "the pagination link to the next page", ("Next page", "More records"), next_link, "link"
                )
            )

    def escalations(self):
        candidates = self.enumerate()
        done = {row["id"] for row in self.cp.get("results", [])}
        for item in candidates:
            if item["id"] in done:
                continue
            try:
                self.escalate(item)
            except BridgeError as exc:
                if exc.code in {
                    "AUTH_EXPIRED",
                    "RUN_CANCELLED",
                    "RUN_PAUSED",
                    "BUDGET_EXCEEDED",
                    "LEASE_LOST",
                }:
                    raise
                self.complete_item(item, "UNVERIFIED", code=exc.code)

    def ticket_snapshot(self):
        state = fields(self.page, "ticket")
        link = self.page.locator('[data-field="customer"] a')
        state["customer_path"] = link.get_attribute("href") if link.count() == 1 else None
        return state

    def escalate(self, item):
        self.open(item["path"])
        current = self.ticket_snapshot()
        if current.get("number") != item["number"]:
            raise BridgeError("TARGET_AMBIGUOUS", "Ticket identity changed")
        pending = self.cp.get("pending")
        if pending and pending["id"] == item["id"]:
            if current["queue"] == "escalation":
                return self.verify_ticket(item, pending, current)
            if current != pending["before"]:
                raise BridgeError(
                    "STATE_CONFLICT", "Ticket changed before an uncertain action could be retried"
                )
            customer = pending["customer"]
        else:
            if not current["customer_path"]:
                return self.complete_item(item, "SKIPPED", code="CRM_LINK_MISSING")
            if (
                current["team"] != self.task.team
                or current["status"] not in self.task.statuses
                or current["priority"] != self.task.priority
                or datetime.fromisoformat(current["created_at"]).timestamp() >= self.task.created_before
            ):
                return self.complete_item(item, "SKIPPED", code="STATE_CONFLICT")
            link = self.page.locator('[data-field="customer"] a')
            self.click(
                Target(f"the linked CRM account {current['customer']}", (current["customer"],), link, "link")
            )
            customer = {**fields(self.page, "customer"), "id": record_id(self.page.url, "customer")}
            if customer.get("external_ref") != current["customer"] or customer["id"] != record_id(
                current["customer_path"], "customer"
            ):
                raise BridgeError("TARGET_AMBIGUOUS", "Linked CRM identity could not be verified")
            customer["company"] = scrub(customer["company"])
            self.open(item["path"])
            if current != self.ticket_snapshot():
                raise BridgeError("STATE_CONFLICT", "Ticket changed during CRM lookup")
            if current["queue"] == "escalation":
                return self.complete_item(
                    item,
                    "NOOP",
                    customer=customer,
                    prior_queue="escalation",
                    new_queue="escalation",
                    evidence=self.evidence(),
                )
            params = {"queue": "escalation", "version": int(current["version"])}
            pending = self.prepare(item, params, current, customer=customer)
        self.approval(pending)
        return self.attempt(item, pending, self.submit_ticket, self.verify_ticket)

    def single(self):
        if self.cp.get("results"):
            return
        kind = self.task.kind
        self.open(ROUTES[kind])
        path = self.list_record(self.task.reference)
        resource = "product" if kind in {"stock", "transfer", "reorder"} else kind
        item = {"id": record_id(path, resource), "path": path, "number": self.task.reference}
        state = fields(self.page, resource)
        key = {"customer": "external_ref", "ticket": "number", "product": "sku"}[resource]
        if state.get(key) != self.task.reference:
            raise BridgeError("TARGET_AMBIGUOUS", "Resource identifier changed after opening")
        if resource == "product":
            state["stock"] = stock_fields(self.page)
        if kind not in {"transfer", "reorder"}:
            self.open(path)
            fresh = fields(self.page, resource)
            if fresh.get(key) != state[key]:
                raise BridgeError("VERIFICATION_FAILED", "Independent read disagreed on identity")
            if resource == "product":
                fresh["stock"] = stock_fields(self.page)
            if "company" in fresh:
                fresh["company"] = scrub(fresh["company"])
            return self.complete_item(item, "VERIFIED", record=fresh, evidence=self.evidence())
        pending = self.cp.get("pending")
        if pending:
            if pending["id"] != item["id"]:
                raise BridgeError("STATE_CONFLICT", "The original product has changed")
            if self.movement(pending["key"]):
                return self.verify_inventory(item, pending, state)
            if state != pending["before"]:
                return self.complete_item(item, "UNVERIFIED", code="STATE_CONFLICT")
        else:
            source = state["stock"].get(self.task.source)
            target = state["stock"].get(self.task.target)
            if not target or (kind == "transfer" and not source):
                raise BridgeError("NOT_FOUND", "Requested warehouse not found")
            if source and source["quantity"] - source["reserved"] < self.task.quantity:
                raise BridgeError("VALIDATION_FAILED", "Transfer exceeds unreserved stock")
            params = {
                "source": source["id"] if source else None,
                "target": target["id"],
                "quantity": self.task.quantity,
                "source_version": source["version"] if source else None,
                "target_version": target["version"],
            }
            pending = self.prepare(item, params, state)
        self.approval(pending)
        return self.attempt(item, pending, self.submit_inventory, self.verify_inventory)

    def prepare(self, item, parameters, before, **details):
        with db.transaction() as conn:
            run = runstore.locked(conn, self.run_id, self.lease)
            key = runstore.intent(conn, run, self.task.kind, item["id"], parameters)
            pending = {
                "key": key,
                "id": item["id"],
                "before": before,
                "parameters": parameters,
                "tries": 0,
                **details,
            }
            self.cp["pending"] = pending
            runstore.append(
                conn,
                run,
                State.PROPOSING,
                "ACTION_PROPOSED",
                {"intent_id": key, "operation": self.task.kind, "resource_id": item["id"]},
                checkpoint=self.cp,
            )
        return pending

    def approval(self, pending):
        self.event(State.PROPOSING, "ACTION_PREPARED")
        self.event(State.POLICY_CHECK, "POLICY_CHECK", {"intent_id": pending["key"]}, control_check=True)
        if not self.task.approval_required:
            return
        evidence_id = self.evidence()
        with db.transaction() as conn:
            run = runstore.locked(conn, self.run_id, self.lease)
            runstore.check_control(run)
            approval = db.row(conn, db.approvals, db.approvals.c.intent_id == pending["key"])
            if approval and approval["status"] == "approved" and approval["expires"] > db.now():
                return
            if approval and (approval["status"] != "pending" or approval["expires"] <= db.now()):
                raise BridgeError("APPROVAL_INVALID", "Approval denied, cancelled or expired")
            if not approval:
                approval_id = db.add(
                    conn,
                    db.approvals,
                    run_id=self.run_id,
                    intent_id=pending["key"],
                    requested_by=run["user_id"],
                    status="pending",
                    expires=min(db.now() + 600, run["deadline"]),
                    reason="",
                    details={
                        "operation": self.task.kind,
                        "resource_id": pending["id"],
                        "before": pending["before"],
                        "requested": pending["parameters"],
                        "evidence_id": evidence_id,
                    },
                )
            else:
                approval_id = approval["id"]
            runstore.append(
                conn,
                run,
                State.WAITING_APPROVAL,
                "APPROVAL_REQUESTED",
                {"approval_id": approval_id, "evidence": evidence_id},
                checkpoint=self.cp,
                lease=None,
                leased_until=None,
            )
        raise Parked

    def submit_ticket(self, pending):
        trigger = self.page.get_by_role("button", name="Review escalation", exact=True)
        if trigger.count():
            self.click(
                Target("the Review escalation button", ("Review escalation",), trigger), navigation=False
            )
        form = self.page.locator('form[data-operation="escalate"]')
        self.browser.prepare_intent(form, {"intent": pending["key"]})
        target = (
            form.get_by_role("button") if self.mode == "scripted" else form.locator("button[type=submit]")
        )
        self.click(
            Target(
                "the button that sends this ticket to the escalation queue",
                ("Escalate ticket", "Send to escalation queue"),
                target,
            ),
            mutation=True,
        )

    def submit_inventory(self, pending):
        form = self.page.locator('form[data-operation="inventory"]')
        params = pending["parameters"]
        for label, value in (
            ("Operation", self.task.kind),
            ("Source warehouse", params["source"] or ""),
            ("Destination warehouse", params["target"]),
        ):
            self.browser.field(form.get_by_label(label, exact=True), "select_option", value)
        self.browser.field(form.get_by_label("Quantity"), "fill", str(params["quantity"]))
        self.browser.prepare_intent(
            form,
            {
                "intent": pending["key"],
                "source_version": params["source_version"],
                "target_version": params["target_version"],
            },
        )
        self.click(
            Target(
                "the Submit stock movement button", ("Submit stock movement",), form.get_by_role("button")
            ),
            mutation=True,
        )

    def attempt(self, item, pending, submit, verify):
        while pending["tries"] <= self.task.retries:
            pending["tries"] += 1
            self.event(
                State.ACTING,
                "ACTION_ATTEMPT",
                {"intent_id": pending["key"], "attempt": pending["tries"]},
                control_check=True,
            )
            code = None
            try:
                submit(pending)
                if self.after_submit:
                    self.after_submit(self.run_id, pending["key"])
            except BridgeError as exc:
                code = exc.code
                if code in {"RUN_PAUSED", "RUN_CANCELLED", "BUDGET_EXCEEDED", "LEASE_LOST"}:
                    raise
            self.event(
                State.VERIFYING,
                "VERIFYING_ACTION",
                {"intent_id": pending["key"], "submit_code": code},
                intent_result=(pending["key"], "OUTCOME_UNKNOWN", {"code": code or "AWAITING_FRESH_READ"}),
            )
            # Budget/control requests gate new effects. A pending intent remains available
            # for reconciliation on resume if the operator pauses before this fresh read.
            self.open(item["path"])
            current = (
                self.ticket_snapshot()
                if self.task.kind == "escalate"
                else {**fields(self.page, "product"), "stock": stock_fields(self.page)}
            )
            applied = (
                current.get("queue") == "escalation"
                if self.task.kind == "escalate"
                else bool(self.movement(pending["key"]))
            )
            if applied:
                return verify(item, pending, current)
            if (
                code in {"COMMIT_UNCERTAIN", "APP_NOT_READY", "LOCATOR_STALE"}
                and current == pending["before"]
                and pending["tries"] <= self.task.retries
            ):
                self.event(State.RECOVERING, "RECOVERY_STARTED", {"code": code, "intent_id": pending["key"]})
                self.event(State.OBSERVING, "REOBSERVING")
                self.event(State.PROPOSING, "RETRY_PROPOSED")
                self.event(State.POLICY_CHECK, "RETRY_POLICY_CHECK")
                continue
            if code == "AUTH_EXPIRED":
                raise BridgeError(code, "Browser session expired")
            reason = code or "VERIFICATION_FAILED"
            return self.complete_item(item, "UNVERIFIED", code=reason, customer=pending.get("customer"))
        return self.complete_item(item, "UNVERIFIED", code="RETRY_BUDGET_EXCEEDED")

    def movement(self, key):
        locator = self.page.locator(f'[data-movement="{key}"]')
        if locator.count() != 1:
            return None
        return {
            "source": locator.get_attribute("data-source") or None,
            "target": locator.get_attribute("data-target"),
            "data": locator.locator("td").all_text_contents(),
        }

    def verify_ticket(self, item, pending, current):
        before = pending["before"]
        effect = self.page.locator(f'[data-effect="{pending["key"]}"]').count() == 1
        checks = {
            "effect_id_matches": effect,
            "queue": current["queue"] == "escalation",
            "version": int(current["version"]) == int(before["version"]) + 1,
            "identity_and_invariants": all(
                current[key] == before[key]
                for key in ("number", "status", "priority", "team", "customer", "customer_path", "created_at")
            ),
        }
        return self.verified(
            item,
            pending,
            checks,
            customer=pending["customer"],
            prior_queue=before["queue"],
            new_queue=current["queue"],
        )

    def verify_inventory(self, item, pending, current):
        params = pending["parameters"]
        movement = self.movement(pending["key"])
        operation = self.task.kind
        checks = {
            "matching_movement": bool(
                movement
                and movement["source"] == params["source"]
                and movement["target"] == params["target"]
                and movement["data"][:3]
                == [
                    operation,
                    str(params["quantity"]),
                    "committed" if operation == "transfer" else "requested",
                ]
            ),
            "identity": current["sku"] == pending["before"]["sku"],
        }
        for name, old in pending["before"]["stock"].items():
            delta = 0
            if operation == "transfer":
                delta = (
                    -params["quantity"]
                    if name == self.task.source
                    else params["quantity"]
                    if name == self.task.target
                    else 0
                )
            new = current["stock"].get(name, {})
            checks[f"stock_{name}"] = (
                new.get("quantity") == old["quantity"] + delta
                and new.get("reserved") == old["reserved"]
                and new.get("version") == old["version"] + int(bool(delta))
            )
        return self.verified(item, pending, checks, record=current)

    def verified(self, item, pending, checks, **details):
        ok = all(checks.values())
        evidence_id = self.evidence()
        verification = {"tier": "fresh_ui", "predicates": checks, "evidence_id": evidence_id}
        self.complete_item(
            item,
            "VERIFIED" if ok else "UNVERIFIED",
            evidence=evidence_id,
            verification=verification,
            **details,
        )

    def update_intent(self, conn, key, status, verification):
        conn.execute(
            update(db.intents)
            .where(db.intents.c.id == key, db.intents.c.run_id == self.run_id)
            .values(status=status, verification=verification, updated_at=db.now())
        )

    def complete_item(self, item, status, **result):
        pending = self.cp.get("pending")
        intent_result = None
        if pending and pending["id"] == item["id"]:
            verification = result.get("verification", {})
            if status == "UNVERIFIED":
                verification = {"code": result.get("code", "VERIFICATION_FAILED"), **verification}
            intent_result = (
                pending["key"],
                "COMMITTED_VERIFIED" if status == "VERIFIED" else "UNVERIFIED",
                verification,
            )
            del self.cp["pending"]
        self.cp.setdefault("results", []).append(
            {**item, "status": status, **result, "intent_id": intent_result[0] if intent_result else None}
        )
        self.event(
            State.CHECKPOINTING,
            "ACTION_VERIFIED" if status == "VERIFIED" else "ITEM_RECONCILED",
            {"status": status, "resource_id": item["id"], **result},
            intent_result=intent_result,
        )
        self.event(State.OBSERVING, "CHECKPOINT_COMMITTED")

    def evidence(self):
        try:
            screenshot = self.browser.screenshot()
        except PlaywrightError:
            return None
        with db.transaction() as conn:
            runstore.locked(conn, self.run_id, self.lease)
            return db.add(
                conn,
                db.evidence,
                run_id=self.run_id,
                kind="screenshot",
                sha256=hashlib.sha256(screenshot).hexdigest(),
                created_at=db.now(),
                body=base64.b64encode(screenshot).decode(),
                redacted=True,
            )


def execute(run_id, token, *, after_submit=None):
    with telemetry.tracer.start_as_current_span("browserbridge.execute") as span:
        span.set_attribute("browserbridge.run_id", run_id)
        Execution(run_id, token, after_submit).run()
