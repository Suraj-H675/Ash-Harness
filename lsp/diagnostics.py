"""LSP-compatible diagnostics emitter for Ash tool results."""

from typing import Any


class LSPDiagnosticsEmitter:
    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root

    def emit_tool_errors(
        self, tool_name: str, error: str, file_path: str
    ) -> dict[str, Any]:
        return {
            "resource": f"file://{self.workspace_root}/{file_path}",
            "diagnostics": [
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 0},
                    },
                    "severity": 1,  # Error
                    "message": f"[{tool_name}] {error}",
                    "source": "ash",
                }
            ],
        }
