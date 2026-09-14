"""Helpers for rendering untrusted text safely in a human terminal."""

from __future__ import annotations

import unicodedata


# Unicode bidi controls change visual ordering without being visible.  They
# are not all in the Cc category, so they need an explicit display policy.
_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,  # ARABIC LETTER MARK
        0x200E,  # LEFT-TO-RIGHT MARK
        0x200F,  # RIGHT-TO-LEFT MARK
        0x202A,  # LEFT-TO-RIGHT EMBEDDING
        0x202B,  # RIGHT-TO-LEFT EMBEDDING
        0x202C,  # POP DIRECTIONAL FORMATTING
        0x202D,  # LEFT-TO-RIGHT OVERRIDE
        0x202E,  # RIGHT-TO-LEFT OVERRIDE
        0x2066,  # LEFT-TO-RIGHT ISOLATE
        0x2067,  # RIGHT-TO-LEFT ISOLATE
        0x2068,  # FIRST STRONG ISOLATE
        0x2069,  # POP DIRECTIONAL ISOLATE
    }
)


def terminal_safe_text(value: str, *, single_line: bool = False) -> str:
    """Render terminal controls and bidi controls visibly.

    Newlines and tabs remain available for free-form text.  Callers rendering
    identifiers or labels can request ``single_line`` so those separators
    cannot manufacture additional terminal lines.
    """

    parts: list[str] = []
    for character in value:
        codepoint = ord(character)
        if (
            character in {"\n", "\t"}
            and not single_line
        ):
            parts.append(character)
            continue
        if (
            unicodedata.category(character) == "Cc"
            or codepoint in _BIDI_CONTROL_CODEPOINTS
        ):
            parts.append(
                f"\\x{codepoint:02x}" if codepoint <= 0xFF else f"\\u{codepoint:04x}"
            )
            continue
        parts.append(character)
    return "".join(parts)
