from __future__ import annotations

import os
from pathlib import Path

import pytest

from ash.lsp.config import LSPServerConfig
from ash.lsp.manager import LanguageServerManager


@pytest.mark.asyncio
async def test_managed_lsp_conforms_with_real_pyright(tmp_path: Path) -> None:
    executable = os.environ.get("ASH_PYRIGHT_LANGSERVER")
    if not executable:
        pytest.skip("set ASH_PYRIGHT_LANGSERVER to a real pyright-langserver")
    server = Path(executable)
    if not server.is_file():
        pytest.fail(f"configured pyright-langserver does not exist: {server}")

    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='ash-lsp-conformance'\nversion='0'\n",
        encoding="utf-8",
    )
    (tmp_path / "example.py").write_text(
        "def greet(name: str) -> str:\n"
        "    return 'hi ' + name\n\n"
        "message = greet(123)\n",
        encoding="utf-8",
    )
    manager = LanguageServerManager(
        tmp_path,
        {
            "pyright": LSPServerConfig(
                name="pyright",
                command=(str(server), "--stdio"),
                extensions={".py": "python"},
                root_markers=("pyproject.toml",),
                env={},
                settings={},
                source="hosted-conformance",
            )
        },
    )
    try:
        hover = await manager.query(
            "hover",
            file_path="example.py",
            line=4,
            character=11,
        )
        assert hover
        assert "def greet(name: str) -> str" in hover[0]["contents"]["value"]

        definitions = await manager.query(
            "definition",
            file_path="example.py",
            line=4,
            character=11,
        )
        assert definitions == [
            {
                "uri": "example.py",
                "range": {
                    "start": {"line": 0, "character": 4},
                    "end": {"line": 0, "character": 9},
                },
            }
        ]

        diagnostics = await manager.query("diagnostics", file_path="example.py")
        assert any(
            item.get("source") == "Pyright"
            and item.get("code") == "reportArgumentType"
            for item in diagnostics
        )
    finally:
        await manager.aclose()
