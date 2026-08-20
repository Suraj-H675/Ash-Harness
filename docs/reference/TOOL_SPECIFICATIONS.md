# ASH — Tool Specifications

This document defines the interface contracts, JSON schemas, input validation rules, and platform-specific security behaviors for the built-in tool suite of Ash.

---

## 1. Tool Base Interface Contract

Every tool in Ash must inherit from the `BaseTool` abstract class.

```python
from abc import ABC, abstractmethod
from typing import Dict, Any, Type
from pydantic import BaseModel, Field

class ToolResult(BaseModel):
    success: bool
    output: str
    error: str | None = None
    token_count: int = 0
    truncated: bool = False

class BaseTool(ABC):
    name: str = Field(..., description="Unique tool identifier registered with the LLM.")
    description: str = Field(..., description="Detailed instructions for the LLM outlining when to call this tool.")
    args_schema: Type[BaseModel] = Field(..., description="Pydantic model defining exact tool arguments.")

    @abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        """Executes the tool's core logic asynchronously. Arguments must match args_schema."""
        ...
```

### 1.1 Execution and Replay Contract

`BaseTool.execution_contract` declares the host's automatic replay policy.
The only currently supported policy is `ToolReplayPolicy.NEVER`, which is the
fail-closed default for every built-in, plugin, and MCP-backed tool.

Ash persists an approved tool intent before dispatch. It runs policy hooks and
pre-execution middleware first; a middleware skip never invokes `tool.run()`.
Immediately before `tool.run()` begins, Ash emits `tool.started`. From that
point forward the runtime invokes that call exactly once. A returned failed
`ToolResult` is a terminal tool outcome. `ToolResult.outcome` is `completed` by
default; transport-backed tools set it to `unknown` whenever they cannot prove
the remote result. An exception, transport loss, timeout, or process
interruption after dispatch is recorded as an **ambiguous** failure:
the side effect may have occurred, and Ash does not automatically replay it.

The tool call record carries whether dispatch began and an ambiguity error; the
audit entry and `tool.error` event carry explicit `dispatched`, `ambiguous`,
`replayed`, and replay-policy fields. Recovery may expose the interrupted
intent for inspection or a newly approved user/model action, but it must not
silently resume the original operation. Provider-request retries before visible
model output are separate transport mechanisms and never permit host-level
replay of a tool invocation.

---

## 2. Core Tool Definitions

### 2.1 `read_file`
Retrieves line-delimited content from the local workspace.

*   **Arguments Schema**:
    ```python
    class ReadFileArgs(BaseModel):
        file_path: str = Field(..., description="Absolute or relative path to the file to read.")
        start_line: int = Field(1, description="1-indexed starting line number (inclusive).")
        end_line: int | None = Field(None, description="1-indexed ending line number (inclusive).")
    ```
*   **Security & Safety Checks**:
    1.  Resolve path via `SafetyGuard.validate_path()`.
    2.  Check for binary signature: read first 8KB of file. If null bytes (`\x00`) are found, block execution unless explicitly overridden or flag set, returning `Error: Binary file detected. Use specialized image/binary viewer tools instead.`
    3.  Limit output to maximum tool response size (default: 800 lines or 50,000 tokens). Truncate and mark `truncated = True` if exceeded.
*   **Response Format (Success)**:
    ```
    1: # File Content Header
    2: import os
    3: ...
    ```

### 2.2 `write_file`
Creates a new file or overwrites an existing one.

*   **Arguments Schema**:
    ```python
    class WriteFileArgs(BaseModel):
        file_path: str = Field(..., description="Target path for writing content.")
        content: str = Field(..., description="Complete textual content to write.")
        overwrite: bool = Field(False, description="Set to true if target file already exists and overwrite is intended.")
    ```
*   **Security & Safety Checks**:
    1.  Validate path scope.
    2.  If file exists and `overwrite = False`, return error: `Error: Target file already exists. Set overwrite to true to replace it, or use replace_file_content to patch specific regions.`
    3.  Auto-create parent directories only if they are within project root boundaries.
    4.  Verify write permissions before execution.

### 2.3 `replace_file_content`
Performs structured Search and Replace operations on a contiguous block of text.

*   **Arguments Schema**:
    ```python
    class ReplaceFileContentArgs(BaseModel):
        file_path: str = Field(..., description="Path of target file to edit.")
        start_line: int = Field(..., description="Start of block range containing target content (1-indexed, inclusive).")
        end_line: int = Field(..., description="End of block range containing target content (1-indexed, inclusive).")
        target_content: str = Field(..., description="Exact string of characters to search for. Must match target range exactly, including leading spaces and tabs.")
        replacement_content: str = Field(..., description="The content to replace the target_content with.")
    ```
*   **Implementation Match & Write Algorithm**:
    1.  **Resolve & Read**: Resolve path using path-scoping rules. Read the entire target file as lines.
    2.  **Scope Verification**: Extract lines from index `start_line - 1` to `end_line` (inclusive).
    3.  **Normalize Line Endings**: Normalize carriage returns inside both the extracted file segment and `target_content` by converting all `\r\n` to standard `\n`. Strip any trailing newline characters strictly for comparison.
    4.  **Exact Matching**: Compare normalized strings.
        -   If they do NOT match exactly, raise an error immediately. Return a line-by-line diff comparison showing exactly where spacing or content mismatches occur.
        -   The match must be constrained *strictly* within the specified line range bounds. Do not search outside the specified range.
    5.  **Atomic Write Commit**:
        -   Re-assemble the full file content by substituting the targeted range with the `replacement_content` (ensuring the target's newline layout is preserved or normalized).
        -   Write the new content to a temporary companion file (e.g. `file_path.tmp`) in the same folder.
        -   Atomic replace: Rename the temporary file to overwrite the original `file_path` to prevent file corruption in case of terminal process interrupts (like Ctrl+C).
        -   Remove any left-over temporary files on completion.

### 2.4 `list_dir`
Scans workspace contents.

*   **Arguments Schema**:
    ```python
    class ListDirArgs(BaseModel):
        directory_path: str = Field(..., description="Directory path to scan.")
        recursive: bool = Field(False, description="Set to true to fetch full recursive tree structure.")
    ```
*   **Safety Limits**:
    1.  Path scope validation.
    2.  Recursive depth is hard-capped at 4 levels.
    3.  Maximum file entities list capacity is capped at 1,000 to prevent context flooding. If exceeded, return the first 1,000 and append `[Warning: Output truncated. Too many files inside directory tree.]`.

### 2.5 `run_command`
Executes user-approved commands in a subprocess shell.

*   **Arguments Schema**:
    ```python
    class RunCommandArgs(BaseModel):
        command_line: str = Field(..., description="The shell command string to execute.")
        cwd: str | None = Field(None, description="Directory path context to run the command in.")
    ```
*   **Security Policies (Critical)**:
    1.  Command string parsed into individual arguments.
    2.  Search arguments against command blocklist:
        -   **Windows Blocklist**: `Format-Volume`, `Remove-Item * -Recurse`, `del /s /q c:\*`, `diskpart`, `bootrec`, `net user`, `reg delete`.
        -   **Linux Blocklist**: `rm -rf /`, `mkfs`, `dd if=`, `chmod -R 777 /`, `chown`, `shutdown`, `reboot`, `passwd`.
    3.  If command contains blacklisted patterns, raise `SafetyViolation` immediately.
    4.  All commands run with standard output buffering. Max output size is limited to 100,000 characters.

### 2.6 `git_status` / `git_diff` / `git_commit`
Handles source-control synchronization.

*   **Arguments Schema**:
    ```python
    class GitCommitArgs(BaseModel):
        message: str = Field(..., description="Descriptive commit message.")
        files: list[str] | None = Field(None, description="Subset of files to stage and commit. If omitted, stages all changes in workspace.")
    ```
*   **Aider-style Transaction Logic**:
    1.  Before executing any file write, check git status.
    2.  After successful tool execution, automatically track modified files.
    3.  Create git commits programmatically on completion of each model loop step to ensure absolute session recovery.

### 2.7 `lsp`

Queries managed Language Server Protocol 3.18 processes for semantic code
intelligence. Supported operations are `status`, `diagnostics`, `hover`,
`definition`, `references`, `implementation`, `documentSymbol`,
`workspaceSymbol`, `prepareCallHierarchy`, `incomingCalls`, and
`outgoingCalls`.

* `file_path` is required except for `status` and `workspaceSymbol`.
* `line` and `character` are 1-based user coordinates. The client converts
  them to the server's negotiated UTF-8, UTF-16, or UTF-32 position units.
* `query` supplies the workspace-symbol search text.
* The tool is policy-read-only, but executable server configuration and local
  `node_modules/.bin` discovery require explicit workspace trust.
* Server commands run without a shell, use a scrubbed environment plus
  explicit overrides, cannot apply edits through `workspace/applyEdit`, and
  are never downloaded by Ash.
* Framing and documents are capped at 8 MiB. Each client retains at most 16
  open documents and 32 MiB of synchronized text. Semantic results are capped
  at 200 items/512 KiB, diagnostics at 100 per file, and stderr at 64 KiB.
* Full and incremental document sync, save options, pull/full/unchanged
  diagnostics, versioned push diagnostics, capability checks, external-URI
  filtering, request timeouts, LRU `didClose`, and process-tree cleanup are
  enforced.
* Post-edit diagnostics are advisory and share one three-second overall
  deadline for at most 20 edited files; they cannot turn a successful edit
  into a failed edit result.

---

## 3. Model Context Protocol (MCP) Integration

Ash supports direct interaction with external tool servers conforming to the Model Context Protocol (MCP) specification.

```
       ┌───────────┐           Model Context Protocol           ┌────────────┐
       │           │ <========================================> │            │
       │    Ash    │      (stdio or Streamable HTTP)            │ MCP Server │
       │  Harness  │                                            │ (Goose,    │
       │  Client   │ <--- list_tools()                          │  SQLite,   │
       │           │ ---> call_tool(name, args)                 │  Docker,   │
       └───────────┘                                            └────────────┘
```

### 3.1 Transport Mechanisms

1. **stdio** launches the configured server without a shell, exchanges bounded
   JSON-RPC messages up to 8 MiB over standard streams, fails pending requests
   immediately on framing or process errors, and shuts down the process tree.
2. **Streamable HTTP** exchanges JSON or SSE responses over HTTP, retains MCP
   session IDs, and supports explicit OAuth 2.1 authorization. The `sse`
   configuration spelling is currently a compatibility alias for this
   transport; deprecated HTTP+SSE endpoint discovery is not advertised.

When a session-bound POST is rejected with the specification-defined 404,
Ash serializes reinitialization, omits the expired session ID, sends
`notifications/initialized`, and resumes safe catalog/list traffic on the
replacement session. Concurrent rejections share that replacement session. A
rejected `tools/call` is never reposted, even if the replacement contract is
unchanged; its terminal result is ambiguous because the remote effect cannot be
proven absent. Timeouts, OAuth authentication rejection, 5xx responses,
malformed responses, and other ambiguous failures likewise never replay tool
calls.
The replacement handshake invalidates the server catalog: Ash reconciles and
atomically publishes the newly negotiated complete tool set for subsequent
calls. Every tool call proves its snapshot fingerprint against the active
verified session generation immediately before its HTTP POST or stdio write.
Paginated lists restart from page one after recovery so opaque cursors and
entries from different sessions are never combined.

### 3.2 Tool Schema Boundary

After initialization, Ash paginates `tools/list` and namespaces each remote
tool as `mcp__<server>__<tool>`. The declared `inputSchema` remains the
authoritative provider-facing schema; Ash does not translate it into a smaller
Pydantic type model.

Servers that declare `tools.listChanged` can update this catalog without a
restart. Ash coalesces notification bursts, paginates and validates a complete
replacement before an atomic per-server swap, and preserves the last good
catalog on failure. Self-sustaining notification storms are rate-limited and
suppressed with a runtime diagnostic. A provider iteration retains the exact
tool snapshot it was offered, while the next iteration and `search_tools` see
the new catalog. Receipt of a valid notification immediately quarantines calls
through the prior catalog. A failed refresh keeps that last-good catalog
visible for discovery and diagnostics but does not make it executable again;
a later complete refresh must verify it first. Declared resource and prompt
list changes emit revisioned runtime events; their list operations already
fetch live data and do not cache stale entries.

* An omitted `$schema` selects JSON Schema 2020-12 for MCP 2025-11-25. Older
  negotiated revisions use draft 7 for ecosystem compatibility; an explicit
  supported dialect always takes precedence.
* Invalid or unsupported dialects and non-object root schemas isolate only the
  malformed tool.
* `enum`, composition keywords, local references, nested arrays and objects,
  and `additionalProperties` retain their original semantics. Remote references
  are rejected rather than fetched from a server-controlled URI.
* Arguments are validated without type coercion and must be finite,
  JSON-serializable data. Invalid arguments never reach the remote server.
* Instance validation runs in a secret-free subprocess with strict input,
  output, CPU, memory, and wall-clock limits so hostile schemas cannot block
  Ash's event loop.
* `json_schema()` returns a defensive copy of the exact server declaration.
* Tools that require the experimental MCP task lifecycle are isolated with a
  catalog diagnostic until Ash implements task-augmented calls; optional-task
  tools remain callable through the ordinary request path.

### 3.3 Tool Result Boundary

MCP `tools/call` results require a `content` array. Ash preserves rich content,
`structuredContent`, `_meta`, `isError`, and extension fields as one JSON
envelope. Version-specific text, image, audio, resource-link, and embedded
resource blocks are checked before model exposure. A successful text-only
result without annotations, metadata, or extensions is rendered as text for
compatibility.

When a tool declares `outputSchema`, every result must include matching
`structuredContent`, including application-error results. Validation failures
retain the server response for diagnosis but produce a failed tool result.
`isError: true` is an application result exposed to the model, not a transport
failure and not a reason to replay the call. JSON-RPC failures similarly retain
their numeric `code` and distinguish absent `data` from explicit `null`; Ash
does not automatically repeat a potentially side-effecting call.

See the [MCP tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
and the [2025-11-25 schema](https://modelcontextprotocol.io/specification/2025-11-25/schema).

---

## 4. Platform-Specific Command Safety Policies

### Windows Safety Patterns
When Ash runs on a Windows host, the subprocess executor uses `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command` by default.

To prevent command injection and runtime exceptions:
1.  **Command Injection**: All target paths passed as arguments must be wrapped in matching single-quotes: `'$path'`.
2.  **Environment Variables**: Environment variables must be resolved strictly using environment dictionary keys rather than shell expansion (e.g. use `os.environ.get("VAR")`, do not pass `%VAR%` directly to shell execution).
3.  **Forbidden Chains**: Command chains using `;`, `&&`, or `||` are forbidden unless executing standard compiler commands (e.g., `cargo build && cargo test`).
4.  **Process Timeouts**: Every subprocess execution MUST have a hard timeout limit configured (default: 300 seconds) to prevent hanging processes from locking the harness.
5.  **UTF-8 Encoding Safety**: On Windows, subprocess byte streams can carry localized characters causing decode crashes. The execution runner must use robust byte decoding:
    ```python
    def decode_stream(raw_bytes: bytes) -> str:
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            # Fallback to system default encoding with character replacement to prevent crashes
            return raw_bytes.decode("cp1252", errors="replace")
    ```
6.  **Literal Path Parameterization**: When executing file operations via PowerShell commandlets, paths must be referenced using `-LiteralPath` instead of positional path variables to prevent bracket expressions `(x86)` from being parsed as executable code blocks.

---

## 5. Token Truncation & Limits

To prevent context window overflows from massive tool outputs, all tool executions route their outputs through a **Truncation Pipeline**.

```python
def truncate_tool_output(output_str: str, max_tokens: int, provider: ProviderABC) -> Tuple[str, bool]:
    """
    If output exceeds max_tokens, truncates middle of content and appends warning message.
    """
    total_tokens = provider.count_tokens(output_str)
    if total_tokens <= max_tokens:
        return output_str, False

    # Perform binary split truncation
    lines = output_str.splitlines()
    keep_lines = int(len(lines) * (max_tokens / total_tokens) * 0.8) // 2

    truncated_output = (
        "\n".join(lines[:keep_lines]) +
        f"\n\n... [TRUNCATED {len(lines) - (keep_lines * 2)} LINES TO RESPECT CONTEXT BUDGET] ...\n\n" +
        "\n".join(lines[-keep_lines:])
    )
    return truncated_output, True
```
Max default token allocations per tool execution:
*   `read_file`: 20,000 tokens.
*   `run_command`: 15,000 tokens.
*   `list_dir`: 10,000 tokens.
