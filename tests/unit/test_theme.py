from ash.ui.theme import get_theme, normalize_theme_name, viewport_styles
from ash.ui.viewport import TranscriptViewport
from ash.ui.transcript import Transcript


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
    assert light.composer == "bg:#eaeaea #111111"


def test_viewport_uses_selected_theme_styles(tmp_path):
    viewport = TranscriptViewport(
        transcript=Transcript(),
        history_path=tmp_path / "history",
        theme="light",
    )
    styles = viewport_styles(get_theme("light"))

    assert dict(viewport.application.style.style_rules) == styles
