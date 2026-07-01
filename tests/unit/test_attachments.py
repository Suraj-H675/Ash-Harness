from __future__ import annotations

from pathlib import Path

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from cli.attachments import expand_file_mentions, prepare_file_mentions
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


def test_mentions_reject_in_scope_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("content", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink or junction"):
        expand_file_mentions("@link.txt", SafetyGuard(tmp_path))


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


def test_prepare_file_mentions_builds_bounded_canonical_image(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image-data")

    prepared = prepare_file_mentions(
        "inspect @image.png", SafetyGuard(tmp_path), allow_images=True
    )

    assert 'kind="image" path="image.png"' in prepared.prompt
    assert len(prepared.images) == 1
    assert prepared.images[0].media_type == "image/png"
    metadata = prepared.message_metadata()
    assert metadata is not None
    assert metadata["image_blocks"][0]["type"] == "image"
    assert "data" not in metadata["images"][0]


def test_prepare_file_mentions_rejects_image_for_nonvision_model(
    tmp_path: Path,
) -> None:
    (tmp_path / "image.jpg").write_bytes(b"\xff\xd8\xff" + b"image-data")

    with pytest.raises(ValueError, match="does not support vision"):
        prepare_file_mentions(
            "inspect @image.jpg", SafetyGuard(tmp_path), allow_images=False
        )


def test_prepare_file_mentions_enforces_combined_token_budget(tmp_path: Path) -> None:
    (tmp_path / "first.txt").write_text("a" * 40, encoding="utf-8")
    (tmp_path / "second.txt").write_text("b" * 40, encoding="utf-8")
    guard = SafetyGuard(tmp_path)

    with pytest.raises(ValueError, match=r"@second\.txt.*budget is 100"):
        prepare_file_mentions(
            "review @first.txt @second.txt",
            guard,
            allow_images=False,
            token_budget=100,
            count_tokens=len,
        )


def test_prepare_file_mentions_reports_attachment_token_usage(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")

    prepared = prepare_file_mentions(
        "review @note.txt",
        SafetyGuard(tmp_path),
        allow_images=False,
        token_budget=1000,
        count_tokens=lambda text: len(text.split()),
    )

    assert 0 < prepared.attachment_tokens <= 1000


def test_image_attachment_budget_includes_fixed_vision_estimate(
    tmp_path: Path,
) -> None:
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nimage")

    with pytest.raises(ValueError, match="1025 tokens"):
        prepare_file_mentions(
            "inspect @image.png",
            SafetyGuard(tmp_path),
            allow_images=True,
            token_budget=1023,
            count_tokens=lambda _: 0,
        )


def test_attachment_budget_requires_counter_pair(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provided together"):
        prepare_file_mentions(
            "nothing",
            SafetyGuard(tmp_path),
            allow_images=False,
            token_budget=100,
        )
