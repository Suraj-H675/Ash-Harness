"""Small deterministic LSP server used by subprocess integration tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    headers: dict[bytes, bytes] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        name, separator, value = line.partition(b":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    length = int(headers[b"content-length"])
    return json.loads(stream.read(length))


def write_message(stream: BinaryIO, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stream.write(body)
    stream.flush()


def respond(stream: BinaryIO, request_id: Any, result: Any) -> None:
    write_message(stream, {"jsonrpc": "2.0", "id": request_id, "result": result})


def log_event(payload: dict[str, Any]) -> None:
    destination = os.environ.get("FAKE_LSP_LOG")
    if destination:
        with Path(destination).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


def diagnostic() -> dict[str, Any]:
    return {
        "range": {
            "start": {"line": 1, "character": 0},
            "end": {"line": 1, "character": 7},
        },
        "severity": 2,
        "message": "fake problem",
        "source": "fake-lsp",
        "code": "F001",
    }


def main() -> int:
    fail_once = os.environ.get("FAKE_LSP_FAIL_ONCE_FILE")
    if fail_once and not Path(fail_once).exists():
        Path(fail_once).write_text("failed\n", encoding="utf-8")
        print("transient startup failure", file=sys.stderr, flush=True)
        return 2
    if os.environ.get("FAKE_LSP_FAIL"):
        print(os.environ["FAKE_LSP_FAIL"], file=sys.stderr, flush=True)
        return 2
    source = sys.stdin.buffer
    sink = sys.stdout.buffer
    root_uri = ""
    documents: dict[str, tuple[str, int]] = {}
    while message := read_message(source):
        log_event(message)
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params", {})
        if method == "initialize":
            time.sleep(float(os.environ.get("FAKE_LSP_INIT_DELAY", "0")))
            root_uri = str(params.get("rootUri", ""))
            write_message(
                sink,
                {
                    "jsonrpc": "2.0",
                    "id": "server-config",
                    "method": "workspace/configuration",
                    "params": {"items": [{"section": "fake.enabled"}]},
                },
            )
            configuration = read_message(source)
            log_event(configuration or {})
            if not configuration or configuration.get("result") != [True]:
                return 3
            respond(
                sink,
                request_id,
                {
                    "capabilities": {
                        "positionEncoding": "utf-8",
                        "textDocumentSync": (
                            {
                                "openClose": True,
                                "change": 2,
                                "save": {"includeText": False},
                            }
                            if os.environ.get("FAKE_LSP_INCREMENTAL") == "1"
                            else 1
                        ),
                        **(
                            {}
                            if os.environ.get("FAKE_LSP_PUSH_ONLY") == "1"
                            else {
                                "diagnosticProvider": {
                                    "interFileDependencies": False,
                                    "workspaceDiagnostics": False,
                                }
                            }
                        ),
                        "hoverProvider": True,
                        "definitionProvider": True,
                        "referencesProvider": True,
                        "documentSymbolProvider": True,
                        "workspaceSymbolProvider": True,
                        "callHierarchyProvider": True,
                    }
                },
            )
        elif method in {"textDocument/didOpen", "textDocument/didChange"}:
            document = params.get("textDocument", {})
            uri = str(document.get("uri", ""))
            if method == "textDocument/didOpen":
                text = str(document.get("text", ""))
            else:
                changes = params.get("contentChanges", [])
                text = str(changes[-1].get("text", "")) if changes else ""
            version = int(document.get("version", 0))
            documents[uri] = (text, version)
            write_message(
                sink,
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {
                        "uri": uri,
                        "version": version,
                        "diagnostics": [diagnostic()] if "problem" in text else [],
                    },
                },
            )
        elif method == "textDocument/diagnostic":
            uri = str(params.get("textDocument", {}).get("uri", ""))
            text, version = documents.get(uri, ("", 0))
            result_id = f"v{version}"
            if params.get("previousResultId") == result_id:
                respond(
                    sink,
                    request_id,
                    {"kind": "unchanged", "resultId": result_id},
                )
            else:
                respond(
                    sink,
                    request_id,
                    {
                        "kind": "full",
                        "resultId": result_id,
                        "items": [diagnostic()] if "problem" in text else [],
                    },
                )
        elif method == "textDocument/hover":
            respond(
                sink,
                request_id,
                {
                    "contents": {
                        "kind": "plaintext",
                        "value": f"character={params['position']['character']}",
                    }
                },
            )
        elif method in {"textDocument/definition", "textDocument/references"}:
            respond(
                sink,
                request_id,
                [
                    {
                        "uri": f"{root_uri}/example.py",
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                    },
                    {
                        "uri": "file:///etc/passwd",
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                    },
                ],
            )
        elif method == "textDocument/documentSymbol":
            respond(sink, request_id, [{"name": "example", "kind": 12}])
        elif method == "workspace/symbol":
            respond(
                sink,
                request_id,
                [{"name": params.get("query", ""), "kind": 12}],
            )
        elif method == "textDocument/prepareCallHierarchy":
            respond(
                sink,
                request_id,
                [
                    {
                        "name": "example",
                        "kind": 12,
                        "uri": f"{root_uri}/example.py",
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "selectionRange": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                    }
                ],
            )
        elif method in {"callHierarchy/incomingCalls", "callHierarchy/outgoingCalls"}:
            respond(sink, request_id, [])
        elif method == "shutdown":
            respond(sink, request_id, None)
        elif method == "exit":
            return 0
        elif request_id is not None:
            write_message(
                sink,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "unsupported"},
                },
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
