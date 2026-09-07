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


def test_structured_output_rejects_duplicate_json_fields() -> None:
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }

    with pytest.raises(ValueError, match="not valid JSON"):
        validate_structured_output('{"ok":false,"ok":true}', schema)
