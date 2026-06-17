# tests/unit/test_turn_context.py
from context.turn import TurnContext


def test_turn_context_set_get() -> None:
    ctx = TurnContext(session_id="s1", turn_id="t1")
    ctx.set("count", 5)
    assert ctx.get("count") == 5
    assert ctx.get("missing", "default") == "default"


def test_turn_context_has() -> None:
    ctx = TurnContext(session_id="s1", turn_id="t1")
    ctx.set("key", "value")
    assert ctx.has("key") is True
    assert ctx.has("missing") is False


def test_turn_context_clear() -> None:
    ctx = TurnContext(session_id="s1", turn_id="t1")
    ctx.set("a", 1)
    ctx.set("b", 2)
    ctx.clear()
    assert ctx.get("a") is None
    assert ctx.get("b") is None
