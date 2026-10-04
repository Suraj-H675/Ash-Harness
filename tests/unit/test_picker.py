import pytest
import asyncio
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ash.ui.picker import FilterPicker, PickerOption


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
