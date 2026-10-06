from prompt_toolkit.styles import DummyStyle

from ash.ui.theme import (
    get_theme,
    normalize_theme_name,
    overlay_styles,
    prompt_style,
    terminal_styles,
)


def test_theme_names_are_validated_and_normalized():
    assert normalize_theme_name(" LIGHT ") == "light"

    try:
        normalize_theme_name("solarized")
    except ValueError as exc:
        assert "dark, light" in str(exc)
    else:
        raise AssertionError("invalid theme was accepted")


def test_default_and_light_themes_expose_distinct_palettes():
    dark = get_theme(None)
    light = get_theme("light")

    assert dark.name == "dark"
    assert dark.composer == "bg:#1c1c1c"
    assert dark.diff_added == "#c8c8c8 bg:#14532d"
    assert dark.diff_removed == "#c8c8c8 bg:#6b252e"
    assert light.composer == "bg:#eaeaea #111111"


def test_overlay_palette_tracks_selected_theme():
    dark = overlay_styles(get_theme("dark"))
    light = overlay_styles(get_theme("light"))

    assert dark["detail"] == get_theme("dark").composer
    assert light["detail"] == get_theme("light").composer
    assert dark["selected"] != light["selected"]


def test_no_color_uses_prompt_toolkit_dummy_style(tmp_path):
    assert isinstance(
        prompt_style(terminal_styles(get_theme("dark")), no_color=True),
        DummyStyle,
    )
