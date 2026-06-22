from __future__ import annotations

from pathlib import Path

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from cli.attachments import expand_file_mentions
from safety.guard import SafetyGuard
from ui.prompt import AshCompleter


def test_file_and_directory_mentions_are_bounded_and_marked_untrusted(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    guard = SafetyGuard(tmp_path)

    expanded = expand_file_mentions("review @src/main.py and @src", guard)

    assert "untrusted workspace data" in expanded
    assert 'kind="file" path="src/main.py"' in expanded
    assert "print('ok')" in expanded
    assert 'kind="directory" path="src"' in expanded
    assert "main.py" in expanded


def test_unknown_mentions_and_email_addresses_are_unchanged(tmp_path: Path) -> None:
    prompt = "ask dev@example.com about @missing.py"
    assert expand_file_mentions(prompt, SafetyGuard(tmp_path)) == prompt


def test_mentions_reject_escape_binary_sensitive_and_oversized_paths(
    tmp_path: Path,
) -> None:
    guard = SafetyGuard(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "image.bin").write_bytes(b"x\x00y")
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / "large.txt").write_bytes(b"x" * 512_001)

    with pytest.raises(ValueError, match="outside project scope"):
        expand_file_mentions(f"@{outside}", guard)
    with pytest.raises(ValueError, match="binary"):
        expand_file_mentions("@image.bin", guard)
    with pytest.raises(ValueError, match="sensitive"):
        expand_file_mentions("@.env", guard)
    with pytest.raises(ValueError, match="exceeds"):
        expand_file_mentions("@large.txt", guard)


def test_workspace_path_completer_handles_files_spaces_and_directories(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "some file.py").write_text("", encoding="utf-8")
    completer = AshCompleter(["/help"], tmp_path)

    root_items = list(completer.get_completions(Document("@s", 2), CompleteEvent()))
    inserted = {item.text for item in root_items}
    assert "@src/" in inserted
    assert '@"some file.py"' in inserted
