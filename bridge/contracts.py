import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .errors import BridgeError

UNRESOLVED = ("open", "pending", "in_progress")
APPS = {"support", "crm", "inventory"}


class Mode(StrEnum):
    DOM = "dom"
    VISION = "vision"
    HYBRID = "hybrid"
    SCRIPTED = "scripted"


class State(StrEnum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    PLANNING = "PLANNING"
    OBSERVING = "OBSERVING"
    PROPOSING = "PROPOSING"
    POLICY_CHECK = "POLICY_CHECK"
    ACTING = "ACTING"
    VERIFYING = "VERIFYING"
    CHECKPOINTING = "CHECKPOINTING"
    RECOVERING = "RECOVERING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_HUMAN_LOGIN = "WAITING_HUMAN_LOGIN"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


TERMINAL = {State.COMPLETED, State.PARTIAL_SUCCESS, State.FAILED, State.CANCELLED, State.TIMED_OUT}
PARKED = {State.WAITING_APPROVAL, State.WAITING_HUMAN_LOGIN, State.PAUSED}
ACTIVE = set(State) - TERMINAL - PARKED
NEXT = {
    State.CREATED: {State.VALIDATING},
    State.VALIDATING: {State.PLANNING},
    State.PLANNING: {State.OBSERVING},
    State.OBSERVING: {State.PROPOSING, State.CHECKPOINTING, State.COMPLETED},
    State.PROPOSING: {State.POLICY_CHECK},
    State.POLICY_CHECK: {State.ACTING, State.WAITING_APPROVAL, State.CHECKPOINTING, State.VERIFYING},
    State.ACTING: {State.VERIFYING},
    State.VERIFYING: {State.CHECKPOINTING, State.RECOVERING},
    State.CHECKPOINTING: {State.OBSERVING, State.COMPLETED},
    State.RECOVERING: {State.OBSERVING, State.VERIFYING},
    State.WAITING_APPROVAL: {State.CREATED},
    State.WAITING_HUMAN_LOGIN: {State.CREATED},
    State.PAUSED: {State.CREATED},
}


def transition(before, after):
    if before == after or after in NEXT.get(before, set()):
        return
    if before in ACTIVE and after in {
        State.FAILED,
        State.PARTIAL_SUCCESS,
        State.CANCELLED,
        State.PAUSED,
        State.TIMED_OUT,
        State.WAITING_HUMAN_LOGIN,
        State.RECOVERING,
    }:
        return
    if before in PARKED and after in {State.CANCELLED, State.TIMED_OUT}:
        return
    raise BridgeError("STATE_CONFLICT", f"Cannot transition {before} to {after}", 409)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RunInput(Input):
    goal: str = Field(min_length=8, max_length=1800)
    mode: Mode = Mode.HYBRID
    apps: set[Literal["support", "crm", "inventory"]] = Field(default_factory=lambda: set(APPS))
    timeout_seconds: int = Field(default=240, ge=15, le=900)
    max_actions: int = Field(default=160, ge=10, le=1000)
    approval_required: bool = False


class Task(Input):
    kind: Literal["escalate", "customer", "ticket", "stock", "transfer", "reorder"]
    team: str | None = None
    priority: str | None = None
    statuses: tuple[str, ...] = ()
    created_before: int | None = None
    reference: str | None = None
    source: str | None = None
    target: str | None = None
    quantity: int | None = None
    apps: tuple[str, ...]
    approval_required: bool = False
    max_actions: int
    model_tokens: int = 16000
    retries: int = 2
    success_predicates: tuple[str, ...]


class Login(Input):
    email: str = Field(min_length=3, max_length=160)
    password: str = Field(min_length=1, max_length=160)


class Decision(Input):
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "deny"]
    reason: str = Field(default="", max_length=300)


ESCALATION = re.compile(
    r"(?:find and )?escalate unresolved (high|normal|low)[ -]priority team ([ab]) "
    r"tickets (?:older than|more than) (\d+) ?(hours?|days?)"
    r"(?:(?:\. check each linked crm (?:customer|account)| and verify linked crm)"
    r"(?: and summarize the outcome)?)?[.!]?"
)


def parse(request: RunInput, clock: int) -> Task:
    goal = " ".join(request.goal.lower().replace("–", "-").split())
    fields = {}
    required = {"support", "crm"}
    approval = request.approval_required
    if re.search(r"\b(escalat\w*|escalation queue)\b", goal) and "ticket" in goal:
        escalation = ESCALATION.fullmatch(goal)
        if not escalation:
            raise BridgeError(
                "VALIDATION_FAILED",
                "Specify one team using 'Escalate unresolved high-priority Team A tickets older than 48 hours'",
                422,
            )
        priority, team, amount, unit = escalation.groups()
        hours = int(amount) * (24 if unit.startswith("day") else 1)
        if not 1 <= hours <= 8760:
            raise BridgeError("VALIDATION_FAILED", "Age must be between one hour and one year", 422)
        kind = "escalate"
        fields = dict(
            team=f"Team {team.upper()}",
            priority=priority,
            statuses=UNRESOLVED,
            created_before=clock - hours * 3600,
        )
        predicates = (
            "every eligible ticket considered",
            "CRM identity confirmed before write",
            "fresh UI verifies queue and ticket invariants",
        )
    else:
        matches = re.findall(r"\b(?:crm|t|sku)-\d+\b", goal)
        if len(matches) != 1:
            raise BridgeError(
                "VALIDATION_FAILED",
                "Specify one supported objective and one CRM, ticket or SKU identifier",
                422,
            )
        ref = matches[0].upper()
        fields = {"reference": ref}
        if ref.startswith("SKU-"):
            required = {"inventory"}
            transfer = re.fullmatch(
                r"transfer\s+(\d+)\s+units?\s+of\s+sku-\d+\s+from\s+(east|west|central)\s+to\s+(east|west|central)[.!]?",
                goal,
            )
            reorder = re.fullmatch(
                r"reorder\s+(\d+)\s+units?\s+of\s+sku-\d+\s+(?:to|for)\s+(east|west|central)[.!]?", goal
            )
            if transfer:
                amount, source, target = transfer.groups()
                if source == target:
                    raise BridgeError("VALIDATION_FAILED", "Source and destination must differ", 422)
                fields.update(source=source.title(), target=target.title(), quantity=int(amount))
                kind, approval = "transfer", True
            elif reorder:
                amount, target = reorder.groups()
                fields.update(target=target.title(), quantity=int(amount))
                kind, approval = "reorder", True
            elif re.match(r"(inspect|check|read|look up|find)\b", goal):
                kind = "stock"
            else:
                raise BridgeError(
                    "VALIDATION_FAILED",
                    "Use Inspect stock for SKU-101 or specify a supported transfer or reorder",
                    422,
                )
        elif re.match(r"(inspect|check|read|look up|find|retrieve|show)\b", goal):
            kind = "customer" if ref.startswith("CRM-") else "ticket"
            required = {"crm" if kind == "customer" else "support"}
        else:
            raise BridgeError(
                "VALIDATION_FAILED", "This goal requires a supported lookup or inventory workflow", 422
            )
        predicates = (
            ("approved movement recorded", "fresh UI verifies stock quantities and versions")
            if kind in {"transfer", "reorder"}
            else ("stable record identity matched", "requested state independently read")
        )
        if fields.get("quantity") is not None and not 1 <= fields["quantity"] <= 10000:
            raise BridgeError("VALIDATION_FAILED", "Quantity must be between 1 and 10,000", 422)
    if not required <= request.apps:
        raise BridgeError(
            "POLICY_DENIED", "The objective needs an application outside the permitted scope", 403
        )
    return Task(
        kind=kind,
        apps=tuple(sorted(required)),
        approval_required=approval,
        max_actions=request.max_actions,
        success_predicates=predicates,
        **fields,
    )
