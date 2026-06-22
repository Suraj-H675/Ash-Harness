import pytest

from ash.cli import validate_structured_output


def test_structured_output_validation() -> None:
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    assert validate_structured_output('{"ok":true}', schema) == {"ok": True}
    with pytest.raises(ValueError, match="schema validation"):
        validate_structured_output('{"ok":"yes"}', schema)
