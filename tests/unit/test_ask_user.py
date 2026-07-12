import pytest

from ash.safety.guard import SafetyGuard
from ash.tools.ask_user import AskUserTool


@pytest.mark.asyncio
async def test_ask_user_uses_typed_callback(tmp_path) -> None:
    seen = []

    def answer(question, options):
        seen.append((question, options))
        return "option-a"

    result = await AskUserTool(SafetyGuard(tmp_path), answer).run(
        question="Choose", options=["option-a", "option-b"]
    )
    assert result.success is True
    assert result.output == "option-a"
    assert seen == [("Choose", ["option-a", "option-b"])]
