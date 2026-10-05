import pytest
import asyncio
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len

from ash.ui.picker import FilterPicker, PickerOption


class SizedDummyOutput(DummyOutput):
    def __init__(self, columns: int, rows: int = 24) -> None:
        self.columns = columns
        self.rows = rows

    def get_size(self) -> Size:
        return Size(rows=self.rows, columns=self.columns)


def _plain(value) -> str:
    return "".join(fragment[1] for fragment in to_formatted_text(value))


@pytest.mark.asyncio
async def test_filter_picker_filters_navigates_and_selects() -> None:
    options = [
        PickerOption("anthropic", "Anthropic", "Cloud API"),
        PickerOption("ollama", "Ollama", "Local runtime"),
        PickerOption("vllm", "vLLM", "Local runtime"),
    ]
    with create_pipe_input() as pipe:
        picker = FilterPicker(
            "Provider",
            options,
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(picker.run_async())
        pipe.send_text("local")
        pipe.send_bytes(b"\x1b[B")
        pipe.send_text("\r")

        assert await pending == "vllm"


@pytest.mark.asyncio
async def test_filter_picker_escape_clears_filter_before_closing() -> None:
    with create_pipe_input() as pipe:
        picker = FilterPicker(
            "Provider",
            [
                PickerOption("openai", "OpenAI"),
                PickerOption("ollama", "Ollama"),
            ],
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(picker.run_async())
        pipe.send_text("olla")
        pipe.send_bytes(b"\x1b")

        assert picker.filter_buffer.text == ""
        assert pending.done() is False

        pipe.send_bytes(b"\x1b")
        assert await pending is None


def test_filter_picker_starts_on_current_value() -> None:
    picker = FilterPicker(
        "Provider",
        [
            PickerOption("openai", "OpenAI"),
            PickerOption("ollama", "Ollama"),
        ],
        current_value="ollama",
        output=DummyOutput(),
    )

    assert picker._selected_option_value() == "ollama"


def test_filter_picker_refresh_preserves_filter_and_selected_value() -> None:
    picker = FilterPicker(
        "Model",
        [
            PickerOption("model-a", "model-a"),
            PickerOption("model-b", "model-b"),
        ],
        current_value="model-a",
        output=DummyOutput(),
    )
    picker.filter_buffer.text = "model"
    picker._selected = 1

    picker.set_options(
        [
            PickerOption("model-a", "model-a"),
            PickerOption("model-b", "model-b"),
            PickerOption("model-c", "model-c"),
        ]
    )
    picker.set_hint("3 live models")

    assert picker.filter_buffer.text == "model"
    assert picker._selected_option_value() == "model-b"
    assert "3 live models" in "".join(
        fragment[1] for fragment in picker._title_text()
    )


def test_filter_picker_uses_ash_identity_and_cell_safe_narrow_rows(monkeypatch) -> None:
    output = SizedDummyOutput(columns=28)
    picker = FilterPicker(
        "Model",
        [
            PickerOption(
                "wide",
                "模型👨‍💻-very-long-model-name",
                state="current-provider-with-long-state",
            )
        ],
        output=output,
    )
    monkeypatch.setattr("ash.ui.picker.get_app_or_none", lambda: picker.application)

    title = _plain(picker._title_text())
    row = _plain(picker._render_list()).rstrip("\n")

    assert title.startswith("ASH  ·  Model")
    assert cell_len(row) <= 28
    assert "模型" in row or "👨‍💻" in row
