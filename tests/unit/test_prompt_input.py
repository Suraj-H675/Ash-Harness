import asyncio
import io
import os
import stat
import sys
from pathlib import Path

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

import ash.ui.prompt as prompt_module
import ash.ui.history as history_module
from ash.ui.prompt import AshCompleter, PromptInput


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def cursor_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        prompt_module,
        "terminal_supports_cursor_ui",
        lambda **kwargs: True,
    )


def test_redirected_input_uses_line_fallback() -> None:
    stream = io.StringIO("hello\n")
    prompt = PromptInput(input_stream=stream)
    assert prompt.interactive is False
    assert asyncio.run(prompt.read()) == "hello"


def test_redirected_eof_is_reported() -> None:
    prompt = PromptInput(input_stream=io.StringIO(""))
    with pytest.raises(EOFError):
        asyncio.run(prompt.read())


def test_invalid_input_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="input_mode"):
        PromptInput(input_stream=io.StringIO(""), input_mode="modal")
    with pytest.raises(ValueError, match="tui_mode"):
        PromptInput(input_stream=io.StringIO(""), tui_mode="floating")


@pytest.mark.asyncio
async def test_inline_prompt_bracketed_paste_preserves_multiline_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cursor_ui: None,
) -> None:
    from prompt_toolkit import PromptSession

    with create_pipe_input() as pipe:
        real_session = PromptSession(
            input=pipe,
            output=DummyOutput(),
            multiline=False,
        )
        monkeypatch.setattr(
            prompt_module,
            "PromptSession",
            lambda **kwargs: real_session,
        )
        prompt = PromptInput(
            input_stream=TtyStringIO(),
            history_path=tmp_path / "history",
            tui_mode="inline",
        )
        pending = asyncio.create_task(prompt.read())
        pipe.send_bytes(b"\x1b[200~first\nsecond\x1b[201~")
        pipe.send_text("\r")

        assert await pending == "first\nsecond"


def test_screen_reader_mode_uses_reduced_dynamic_prompt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailPromptSession:
        def __init__(self, **kwargs) -> None:
            raise AssertionError("screen-reader mode must not create prompt-toolkit UI")

    monkeypatch.setattr(prompt_module, "PromptSession", FailPromptSession)
    prompt = PromptInput(
        input_stream=TtyStringIO(),
        history_path=tmp_path / "history",
        tui_mode="viewport",
        screen_reader_mode=True,
    )

    assert prompt.uses_viewport is False
    assert prompt.screen_reader_mode is True
    assert prompt._session is None
    assert prompt._completer is None


@pytest.mark.parametrize("term", ["dumb", "unknown"])
def test_terminal_capability_rejects_limited_terminals(term: str) -> None:
    assert (
        prompt_module.terminal_supports_cursor_ui(
            input_stream=TtyStringIO(),
            output_stream=TtyStringIO(),
            environment={"TERM": term},
        )
        is False
    )


def test_terminal_capability_requires_tty_output() -> None:
    assert (
        prompt_module.terminal_supports_cursor_ui(
            input_stream=TtyStringIO(),
            output_stream=io.StringIO(),
            environment={"TERM": "xterm-256color"},
        )
        is False
    )


@pytest.mark.asyncio
async def test_limited_terminal_uses_linear_reader_without_prompt_toolkit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailPromptSession:
        def __init__(self, **kwargs) -> None:
            raise AssertionError("limited terminals must not create prompt-toolkit UI")

    output = TtyStringIO()
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(prompt_module, "PromptSession", FailPromptSession)
    prompt = PromptInput(input_stream=TtyStringIO("hello\n"), tui_mode="viewport")

    assert prompt.linear_mode is True
    assert prompt.supports_full_screen_ui is False
    assert prompt.uses_viewport is False
    assert await prompt.read("limited> ") == "hello"
    assert output.getvalue() == "limited> "


@pytest.mark.asyncio
async def test_screen_reader_mode_reads_linearly_without_prompt_toolkit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()

    class TtyPipe:
        encoding = "utf-8"

        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return read_fd

    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    prompt = PromptInput(
        input_stream=TtyPipe(),
        history_path=tmp_path / "history",
        screen_reader_mode=True,
    )
    try:
        pending = asyncio.create_task(prompt.read("accessible> "))
        await asyncio.sleep(0)
        os.write(write_fd, b"hello\n")

        assert await pending == "hello"
        assert output.getvalue() == "accessible> "
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_prompt_completion_updates_after_plugin_reload(
    tmp_path, monkeypatch: pytest.MonkeyPatch, cursor_ui: None
) -> None:
    captured = {}

    class FakePromptSession:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(prompt_module, "PromptSession", FakePromptSession)
    prompt = PromptInput(
        input_stream=TtyStringIO(),
        history_path=tmp_path / "history",
        extra_commands=["old:command"],
    )

    prompt.set_extra_commands(["example:review"])
    completer = captured["completer"]
    completions = list(
        completer.get_completions(
            Document("/example:r"), CompleteEvent(completion_requested=True)
        )
    )

    assert [completion.text for completion in completions] == ["/example:review"]
    assert [completion.display_meta_text for completion in completions] == [
        "custom command"
    ]


def test_prompt_completion_preserves_custom_command_description(
    tmp_path, monkeypatch: pytest.MonkeyPatch, cursor_ui: None
) -> None:
    captured = {}

    class FakePromptSession:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(prompt_module, "PromptSession", FakePromptSession)
    PromptInput(
        input_stream=TtyStringIO(),
        history_path=tmp_path / "history",
        extra_commands={"project:review": "Review release notes"},
    )

    completions = list(
        captured["completer"].get_completions(
            Document("/project:r"), CompleteEvent(completion_requested=True)
        )
    )

    assert [completion.text for completion in completions] == ["/project:review"]
    assert [completion.display_meta_text for completion in completions] == [
        "Review release notes"
    ]


def test_builtin_slash_completion_has_description_while_filtering_prefix(
    tmp_path,
) -> None:
    completer = AshCompleter(["/model", "/models", "/status"], tmp_path)

    completions = list(
        completer.get_completions(
            Document("/mo"), CompleteEvent(completion_requested=True)
        )
    )

    assert [completion.text for completion in completions] == ["/model", "/models"]
    assert all(completion.display_meta_text for completion in completions)


def test_bare_slash_completion_keeps_curated_builtin_order(tmp_path) -> None:
    completer = AshCompleter(
        ["/agents", "/browser", "/help", "/status", "/model", "/custom:review"],
        tmp_path,
    )

    completions = list(
        completer.get_completions(
            Document("/"), CompleteEvent(completion_requested=True)
        )
    )

    assert [completion.text for completion in completions[:4]] == [
        "/help",
        "/status",
        "/model",
        "/agents",
    ]
    assert completions[-1].text == "/custom:review"


def test_slash_argument_completion_and_builtin_metadata_are_contextual(
    tmp_path,
) -> None:
    completer = AshCompleter(
        ["/model", "/permissions", "/mcp", "/status"],
        tmp_path,
        command_descriptions={"/status": "custom collision"},
    )
    event = CompleteEvent(completion_requested=True)

    model = list(completer.get_completions(Document("/model "), event))
    permissions = list(
        completer.get_completions(Document("/permissions a"), event)
    )
    mcp = list(completer.get_completions(Document("/mcp status "), event))
    plugin_install = list(
        completer.get_completions(Document("/plugins install demo "), event)
    )
    plugin_uninstall = list(
        completer.get_completions(Document("/plugins uninstall demo "), event)
    )
    browser_connect = list(
        completer.get_completions(
            Document("/browser connect http://127.0.0.1:9222 "),
            event,
        )
    )
    status = list(completer.get_completions(Document("/sta"), event))

    assert "openai/" in [completion.text for completion in model]
    assert {completion.text for completion in permissions} >= {
        "allow",
        "ask",
        "auto_edit",
        "auto_approve",
    }
    assert [completion.text for completion in mcp] == ["--json"]
    assert [completion.text for completion in plugin_install] == [
        "--replace",
        "--ref",
    ]
    assert [completion.text for completion in plugin_uninstall] == ["--yes"]
    assert [completion.text for completion in browser_connect] == [
        "--reuse-storage-state"
    ]
    assert [completion.display_meta_text for completion in status] == [
        "Show session and runtime status"
    ]


def test_model_completion_includes_custom_providers_and_known_models(tmp_path) -> None:
    completer = AshCompleter(
        ["/model"],
        tmp_path,
        model_choices=[
            "openai/gpt-6-astra",
            "my-gateway/agent-model",
            "my-gateway/fast-model",
        ],
    )
    event = CompleteEvent(completion_requested=True)

    providers = list(completer.get_completions(Document("/model my"), event))
    models = list(
        completer.get_completions(Document("/model my-gateway/"), event)
    )

    assert [(item.text, item.display_meta_text) for item in providers] == [
        ("my-gateway/", "Configured custom provider")
    ]
    assert [item.text for item in models] == [
        "my-gateway/agent-model",
        "my-gateway/fast-model",
    ]


def test_prompt_keeps_curated_builtin_command_order_before_custom_commands(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    cursor_ui: None,
) -> None:
    captured = {}

    class FakePromptSession:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(prompt_module, "PromptSession", FakePromptSession)
    PromptInput(
        input_stream=TtyStringIO(),
        history_path=tmp_path / "history",
        extra_commands=["zzz:custom", "aaa:custom"],
    )

    completions = list(
        captured["completer"].get_completions(
            Document("/"), CompleteEvent(completion_requested=True)
        )
    )
    words = [completion.text for completion in completions]

    assert words[:8] == [
        "/help",
        "/status",
        "/usage",
        "/cost",
        "/settings",
        "/cancel",
        "/model",
        "/models",
    ]
    assert words[-2:] == ["/aaa:custom", "/zzz:custom"]


def test_path_completion_scans_a_bounded_number_of_entries(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    original_iterdir = prompt_module.Path.iterdir

    def fake_iterdir(path):
        if path == root:
            return (root / f"file-{index:05d}.txt" for index in range(20_000))
        return original_iterdir(path)

    monkeypatch.setattr(prompt_module.Path, "iterdir", fake_iterdir)
    completer = AshCompleter([], root)

    completions = list(
        completer.get_completions(
            Document("@file-"), CompleteEvent(completion_requested=True)
        )
    )

    assert len(completions) == prompt_module.MAX_PATH_COMPLETIONS


def test_interactive_prompt_history_rejects_symlink(
    tmp_path, monkeypatch, cursor_ui: None
) -> None:
    target = tmp_path / "target"
    target.write_text("keep", encoding="utf-8")
    history_path = tmp_path / "history"
    try:
        history_path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    monkeypatch.setattr(prompt_module, "PromptSession", lambda **kwargs: None)

    with pytest.raises(ValueError, match="symlinked prompt history"):
        PromptInput(input_stream=TtyStringIO(), history_path=history_path)
    assert target.read_text(encoding="utf-8") == "keep"


def test_interactive_prompt_history_is_private_and_nofollow(
    tmp_path, monkeypatch, cursor_ui: None
) -> None:
    captured = {}

    class FakePromptSession:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(prompt_module, "PromptSession", FakePromptSession)
    history_path = tmp_path / "history"
    PromptInput(input_stream=TtyStringIO(), history_path=history_path)
    history = captured["history"]

    history.append_string("first\nsecond")
    assert "first" in history_path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(history_path.stat().st_mode) == 0o600

    target = tmp_path / "outside"
    target.write_text("keep", encoding="utf-8")
    history_path.unlink()
    try:
        history_path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises((OSError, ValueError), match="symlink|link|loop|follow"):
        history.append_string("redirected")
    assert target.read_text(encoding="utf-8") == "keep"


def test_interactive_prompt_history_redacts_secrets_before_persistence(
    tmp_path,
) -> None:
    history_path = tmp_path / "history"
    history = history_module.PrivateFileHistory(history_path)
    provider_secret = "sk-proj-" + "A" * 32
    signed_marker = "signed-history-marker"
    raw = (
        f"token={provider_secret} "
        "https://storage.example/object?"
        f"X-Amz-Signature={signed_marker}&view=complete"
    )

    history.append_string(raw)

    persisted = history_path.read_text(encoding="utf-8")
    loaded = list(history.load_history_strings())
    assert provider_secret not in persisted
    assert signed_marker not in persisted
    assert provider_secret not in loaded[0]
    assert signed_marker not in loaded[0]
    assert "[REDACTED]" in persisted
    assert "view=complete" in persisted


def test_interactive_prompt_history_retention_is_bounded(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history_module, "MAX_HISTORY_FILE_BYTES", 700)
    monkeypatch.setattr(history_module, "MAX_HISTORY_ENTRY_BYTES", 240)
    history_path = tmp_path / "history"
    history = history_module.PrivateFileHistory(history_path)

    for index in range(12):
        history.append_string(f"entry-{index}-" + ("x" * 90))

    loaded = list(history.load_history_strings())
    assert history_path.stat().st_size <= 700
    assert loaded[0].startswith("entry-11-")
    assert all(not item.startswith("entry-0-") for item in loaded)


def test_interactive_prompt_history_bounds_single_persisted_entry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history_module, "MAX_HISTORY_FILE_BYTES", 700)
    monkeypatch.setattr(history_module, "MAX_HISTORY_ENTRY_BYTES", 160)
    history_path = tmp_path / "history"
    history = history_module.PrivateFileHistory(history_path)

    history.append_string("prefix-" + ("y" * 2_000))

    loaded = list(history.load_history_strings())
    assert history_path.stat().st_size <= 700
    assert len(history_path.read_bytes()) < 300
    assert loaded[0].startswith("prefix-")
    assert loaded[0].endswith("[history entry truncated]")


def test_interactive_prompt_history_repairs_existing_posix_permissions(
    tmp_path, monkeypatch, cursor_ui: None
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permissions are unavailable")
    captured = {}

    class FakePromptSession:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(prompt_module, "PromptSession", FakePromptSession)
    history_path = tmp_path / "history"
    history_path.write_text("", encoding="utf-8")
    history_path.chmod(0o644)

    PromptInput(input_stream=TtyStringIO(), history_path=history_path)

    assert captured["history"] is not None
    assert stat.S_IMODE(history_path.stat().st_mode) == 0o600


def test_prompt_history_parent_swap_cannot_redirect_append(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "state"
    moved = tmp_path / "state-original"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    history_path = parent / "history"
    history = history_module.PrivateFileHistory(history_path)
    real_validate = history_module.validate_history_path
    swapped = False

    def validate_then_swap(path):
        nonlocal swapped
        real_validate(path)
        if not swapped:
            swapped = True
            parent.rename(moved)
            try:
                parent.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")

    monkeypatch.setattr(history_module, "validate_history_path", validate_then_swap)

    with pytest.raises((OSError, ValueError)):
        history.append_string("must stay local")

    assert swapped is True
    assert not (outside / "history").exists()


def test_prompt_history_closes_file_when_parent_validation_fails_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_path = tmp_path / "history"
    history_path.write_text("", encoding="utf-8")
    history = history_module.PrivateFileHistory(history_path)
    opened: list[int] = []
    real_open_file = history_module.AnchoredDirectory.open_file
    real_validation_path = history_module.AnchoredDirectory.validation_path
    validation_calls = 0

    def capture_open_file(self, *args, **kwargs):
        descriptor = real_open_file(self, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail_second_validation(self):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 2:
            raise history_module.AnchoredFilesystemError("parent identity changed")
        return real_validation_path(self)

    monkeypatch.setattr(history_module.AnchoredDirectory, "open_file", capture_open_file)
    monkeypatch.setattr(
        history_module.AnchoredDirectory,
        "validation_path",
        fail_second_validation,
    )

    with pytest.raises(ValueError, match="redirected prompt history path"):
        tuple(history.load_history_strings())

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_prompt_history_ancestor_symlink_cannot_create_external_parent(
    tmp_path, monkeypatch: pytest.MonkeyPatch, cursor_ui: None
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    monkeypatch.setattr(prompt_module, "PromptSession", lambda **kwargs: None)
    history_path = alias / "nested" / "history"

    with pytest.raises((OSError, ValueError)):
        PromptInput(input_stream=TtyStringIO(), history_path=history_path)

    assert not (outside / "nested").exists()
