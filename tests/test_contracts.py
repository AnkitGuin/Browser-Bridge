import pytest

from benchmarks.cases import generate
from benchmarks.harness import percentile, wilson
from bridge.browser import ACTION, Browser, pixel
from bridge.contracts import RunInput, State, parse, transition
from bridge.errors import BridgeError
from bridge.runstore import intent_hash

GOAL = "Find and escalate unresolved high-priority Team A tickets older than 48 hours and verify linked CRM."


def test_task_clock_and_scope():
    task = parse(RunInput(goal=GOAL), 1_000_000)
    assert task.created_before == 827_200
    assert task.statuses == ("open", "pending", "in_progress")
    assert task.apps == ("crm", "support")
    with pytest.raises(BridgeError, match="outside the permitted scope"):
        parse(RunInput(goal=GOAL, apps={"support"}), 1_000_000)
    with pytest.raises(BridgeError, match="Specify one team"):
        parse(RunInput(goal=GOAL + " Also Team B."), 1_000_000)


@pytest.mark.parametrize(
    "goal",
    [
        f"Do not {GOAL.lower()}",
        f"{GOAL} Exclude the first ticket.",
        f"Preview what happens if I {GOAL.lower()}",
    ],
)
def test_escalation_requires_an_unambiguous_affirmative_goal(goal):
    with pytest.raises(BridgeError, match="Specify one team"):
        parse(RunInput(goal=goal), 1_000_000)


def test_state_and_canonical_intent():
    transition(State.VERIFYING, State.CHECKPOINTING)
    transition(State.OBSERVING, State.WAITING_HUMAN_LOGIN)
    with pytest.raises(BridgeError):
        transition(State.COMPLETED, State.ACTING)
    with pytest.raises(BridgeError):
        transition(State.WAITING_APPROVAL, State.ACTING)
    assert intent_hash("run", "transfer", "record", {"a": 1, "b": 2}) == intent_hash(
        "run", "transfer", "record", {"b": 2, "a": 1}
    )
    assert intent_hash("run", "transfer", "record", {"a": 1}) != intent_hash(
        "run", "transfer", "record", {"a": 2}
    )


def test_typed_browser_action_and_origin():
    for action in (
        {"kind": "shell", "code": "test"},
        {"kind": "press_key", "key": "Control+L"},
        {"kind": "navigate", "route": "/support", "script": "return 0"},
    ):
        with pytest.raises(ValueError):
            ACTION.validate_python(action)
    runtime = Browser({"support"}, "dom", "synthetic", lambda _: None)
    assert runtime.allowed(f"{runtime.base.geturl()}/support/tickets")
    for url in (
        "https://attacker.example/",
        "file:///tmp/example",
        "chrome://settings",
        f"{runtime.base.geturl()}/api/v1/runs",
        f"{runtime.base.geturl()}/crm",
    ):
        assert not runtime.allowed(url)
    assert pixel(0.5, 0.5, 1280, 820) == (640, 410)
    with pytest.raises(BridgeError):
        pixel(1, 0.1, 1280, 820)


def test_seeded_definition_split_and_metrics():
    a, b = generate(42), generate(43)
    assert len(a) == 120 and a == generate(42) and a != b
    assert len({item.id for item in a}) == 120
    assert sum(item.split == "held_out" for item in a) == 85
    assert percentile([9, 1, 2], 0.5) == 2
    assert wilson(0, 0) is None
    assert wilson(8, 8)[1] == 1


def test_redirect_guard_checks_location_before_following():
    from types import SimpleNamespace
    from unittest.mock import Mock

    runtime = Browser({"support"}, "dom", "synthetic", lambda _: None)
    route = Mock()
    route.request.url = f"{runtime.base.geturl()}/support"
    route.request.is_navigation_request.return_value = True
    response = SimpleNamespace(status=302, headers={"location": "https://attacker.example/"}, dispose=Mock())
    route.fetch.return_value = response
    runtime.route_request(route)
    route.fetch.assert_called_once_with(max_redirects=0, timeout=5500)
    route.abort.assert_called_once()
    route.fulfill.assert_not_called()
    response.dispose.assert_called_once()
    assert runtime.blocked == 1
