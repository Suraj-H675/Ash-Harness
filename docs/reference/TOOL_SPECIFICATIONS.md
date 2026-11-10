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
    sensitive_argument_fields: frozenset[str] = frozenset()

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

### 1.2 Sensitive Argument Persistence

Tools whose otherwise ordinary argument names can carry confidential values may
declare top-level field names in `BaseTool.sensitive_argument_fields`. Ash uses
this class metadata only when serializing durable tool intent, audit/event
arguments, and persisted assistant tool-call metadata. Declared values are
stored as `[REDACTED]`; the live approval and execution paths still receive the
original arguments so the user can make an informed one-time decision and the
tool can perform the requested action.

Sensitive-field metadata is declarative: Ash reads it from the tool class and
does not invoke plugin/tool instance code while preparing a pre-approval record.
Automatic exact-scope permission rules fail closed when a call contains a
declared-sensitive field rather than silently omitting it and broadening the
grant. The explicit scope editor may exclude sensitive fields and build a scope
from the remaining non-sensitive arguments after informing the user.

---

## 2. Core Tool Definitions

### 2.1 `read_file`
Retrieves numbered, bounded source text from the local workspace with a
whole-file SHA-256 digest and detected text encoding. The read is non-mutating.

*   **Arguments Schema**:
    ```python
    class ReadFileArgs(BaseModel):
        file_path: str = Field(..., description="Absolute or relative path to the file to read.")
        start_line: int = Field(1, description="1-indexed starting line number (inclusive).")
        end_line: int | None = Field(None, description="1-indexed ending line number (inclusive).")
        start_column: int = Field(1, description="1-indexed Unicode character offset within start_line.")
        max_chars: int = Field(32768, ge=1, le=131072, description="Source characters per read, excluding metadata and line numbers.")
        expected_sha256: str | None = Field(None, description="Optional digest from a prior read; reject changed files.")
    ```
*   **Security & Safety Checks**:
    1.  Resolve and descriptor-scope the file through `SafetyGuard` and scoped I/O. Refuse workspace escapes and unsafe file substitutions.
    2.  Read at most 8 MiB; reject binary files and unsupported text encodings. Preserve the detected encoding in metadata for subsequent edits.
    3.  If `expected_sha256` is provided, reject a changed file rather than returning a mismatched continuation page.
    4.  Return at most 800 numbered lines **and** at most `max_chars` source characters per call. `max_chars` does not include metadata or line-number labels and is a character, not an exact provider-token, limit.
    5.  On truncation, return an explicit next-page cursor. For a long line, use both `start_line=next_start_line` and `start_column=next_start_column`. Supply the first page's `sha256` as `expected_sha256` when reading further pages of the same snapshot.
*   **Response Format (Success)**:
    ```
    [read_file metadata: path=/workspace/example.py; sha256=...; encoding=utf-8; total_file_lines=3]
    1: # File Content Header
    2: import os
    3: ...
    ```
    If the source-character limit is reached, the result also includes:
    ```
    [read_file truncated: source_chars_limit=32768; next_start_line=3; next_start_column=128]
    ```
    A page limited only by the 800-line cap retains the existing line-based
    truncation metadata. Normal small reads retain their original numbering
    and output shape.

### 2.2 `write_file`
Creates a new file or overwrites an existing one.
An existing file is **never** overwritten implicitly. A complete rewrite
requires `overwrite=true`; use `replace_file_content` for a focused change.

*   **Arguments Schema**:
    ```python
    class WriteFileArgs(BaseModel):
        file_path: str = Field(..., description="Target path for writing content.")
        content: str = Field(..., description="Complete textual content to write.")
        overwrite: bool = Field(False, description="Existing files are refused by default. Set true for an intentional full-file rewrite; prefer replace_file_content for small edits.")
    ```
*   **Security & Safety Checks**:
    1.  Validate path scope.
    2.  If file exists and `overwrite = False`, return error: `Error: Target file already exists. Set overwrite to true to replace it, or use replace_file_content to patch specific regions.`
    3.  Auto-create parent directories only if they are within project root boundaries.
    4.  Verify write permissions before execution.

### 2.3 `replace_file_content`
Performs structured Search and Replace operations on a contiguous block of text.
`target_content` is **old text already present in the file** (without
`read_file`'s line-number prefixes); `replacement_content` is **new desired
text**. Submitting the new code as both arguments usually requests a no-op and
will be refused. An intentional newline/line-ending change is the exception.

*   **Arguments Schema**:
    ```python
    class ReplaceFileContentArgs(BaseModel):
        file_path: str = Field(..., description="Path of target file to edit.")
        start_line: int | None = Field(None, description="Optional 1-indexed inclusive start line.")
        end_line: int | None = Field(None, description="Optional 1-indexed inclusive end line.")
        target_content: str = Field(..., description="OLD existing complete lines to match, including indentation and excluding read_file line numbers.")
        replacement_content: str = Field(
            ...,
            description=(
                "NEW desired text. LF-only separators follow the replaced range's "
                "local style; replacements containing CR keep their supplied separators."
            ),
        )
    ```
*   **Implementation Match & Write Algorithm**:
    1.  **Resolve & Read**: Resolve path using path-scoping rules. Read the entire target file as lines.
    2.  **Scope Verification**: With both line bounds, extract lines from index `start_line - 1` to `end_line` (inclusive), retaining each source line's terminator. Without either bound, find exactly one nonempty block of complete lines matching `target_content` in the file. Reject missing, partial-line, and ambiguous matches without changing the file. Providing only one bound is invalid.
    3.  **Normalize for Matching Only**: Normalize CRLF and lone CR inside the extracted file segment and `target_content` to `\n`. Strip trailing newline characters strictly for comparison. Do not normalize the source file before reassembly.
    4.  **Exact Matching**: Compare normalized strings.
        -   If they do NOT match exactly, raise an error immediately. Return a line-by-line diff comparison showing exactly where spacing or content mismatches occur, with guidance to reread and copy exact lines. A single-range edit also explains that both line bounds can be omitted when the full-line target is unique.
        -   For explicitly bounded edits, the match must be constrained *strictly* within the specified line range bounds. Do not search outside the specified range.
        -   Unbounded unique-block matching still requires exact full-line content. If repeated content appears more than once, provide both line bounds to disambiguate. Optional `expected_sha256` protects either mode against stale reads.
    5.  **Choose Replacement Terminators**:
        -   Treat LF-only replacement separators as newline-neutral and map them to the most common terminator in the replaced range. Break ties by first occurrence. If the range has none, use the nearest surrounding CRLF, CR, or LF terminator, preferring the preceding line on a tie, or LF when none exists.
        -   If the replacement contains CR, preserve its supplied CRLF, CR, and LF separators as explicit. Unchanged source lines keep their original terminators.
        -   If nonempty replacement content omits a trailing terminator and the replaced range had one, carry that exact outgoing terminator forward. An explicit trailing terminator can change the boundary; an empty replacement removes the selected lines and their terminators.
    6.  **Atomic Write Commit**:
        -   Re-assemble the full file content by substituting only the targeted range. `replace_file_edits` uses the same matching and replacement rules for each non-overlapping range.
        -   For `replace_file_content`, reject a replacement that leaves the decoded file unchanged without rewriting the file. When identical target/replacement text fails to match any complete line block, report the invalid no-op intent explicitly. A replacement that **does** change the final newline or line-ending style remains valid even if its two argument strings are identical.
        -   Encode using the source file's detected encoding and BOM, then enforce the byte-size limit.
        -   Write through the scoped atomic writer with the original file digest as its compare-and-swap expectation; remove any temporary file on completion or failure.
    7.  **Scope Refusals**: `read_file`, `write_file`, `replace_file_content`, `replace_file_edits`, and `whole_edit` return an unsuccessful, known-completed `ToolResult` for paths denied by the workspace guard. None of the filesystem tools attempts to read or write the denied path.

### 2.4 `list_dir`
Scans workspace contents.

`list_dir`, `glob_files`, and `search_text` report file locations **relative to
Current Project Path**, even when `directory_path` selects a nested directory.
For example, listing `directory_path="pkg"` yields `pkg/app.py`, which can be
passed unchanged to `read_file`. Glob patterns are still matched relative to
the selected search directory: `pattern="app.py", directory_path="pkg"` finds
`pkg/app.py`. Search matches retain `path:line:text` formatting. All paths
remain subject to the same workspace guards; no denied path is rewritten.

`list_dir`, `glob_files`, and `search_text` return a known-completed,
unsuccessful tool result when their root path is outside the workspace instead
of raising an exception after dispatch. The refusal preserves the attempted
path and suggests using a project-relative path or `directory_path="."` to
discover workspace files; it never maps `/workspace` or another invented
absolute prefix into the real project. `read_file` gives the same guidance.
Neither read nor search is attempted at the denied location.

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
By default, it starts in **Current Project Path** (the workspace root), so
`python3 -B test_example.py` needs no leading `cd`. An optional `cwd` argument
selects a workspace-scoped directory. For a user-requested *exact* command,
pass the string verbatim as `command_line` and leave `cwd` unset unless the
user explicitly requests a different working directory. Adding a shell `cd`
prefix changes the command string and does **not** match a `command_line`-exact
permission grant. Authorization is checked before execution; Ash never
silently strips shell prefixes or upgrades a denied command to an allowed one.

An exact grant constrained only by `command_line` does not itself restrict the
separate `cwd` argument. Policies requiring a particular working directory must
scope that argument as well; the workspace boundary still applies to all `cwd`
values.

*   **Arguments Schema**:
    ```python
    class RunCommandArgs(BaseModel):
        command_line: str = Field(..., description="The shell command string to execute.")
        cwd: str | None = Field(None, description="Directory path context to run the command in.")
    ```
*   **Security Policies (Critical)**:
    1.  Command string parsed into individual arguments.
    2.  Search arguments against the supported-host command blocklist, including
        destructive patterns such as `rm -rf /`, `mkfs`, `dd if=`,
        `chmod -R 777 /`, `chown`, `shutdown`, `reboot`, and `passwd`.
    3.  If command contains blacklisted patterns, raise `SafetyViolation` immediately.
    4.  Commands retain a bounded output capture (100,000 bytes across
        stdout/stderr for pipe execution; 100,000 bytes for PTY execution).
        Ash continues draining up to 2,000,000 total bytes so ordinary verbose
        test/build commands can finish and report their real exit status even
        when their output is truncated. Beyond that hard limit, Ash stops the
        managed process tree and conservatively reports an unknown outcome.
        The live output stream is separately capped at 100,000 characters.

Tool results report `success` separately from process output and include an
`exit_code` when the process completed with a known status. Timeouts and other
unknown outcomes omit the code so a termination status is not presented as a
completed command result.

### 2.6 `git_status` / `git_diff` / `git_log` / `auto_commit`

The read-only Git tools expose bounded status, diff, and recent-log inspection.
`auto_commit` is the only commit tool and requires an explicit workspace-relative
path list; it refuses an empty scope so unrelated user work cannot be staged by
default. Commit messages and path counts are bounded, staged content is scanned
for secrets, and hook execution remains inside the runtime's sandbox and
environment policy.

Loop-level automatic commits are a separate optional feature. They are disabled
by default. When enabled, Ash snapshots Git state at turn start and, after the
turn completes, commits only paths Ash changed during that turn that still match
the recorded post-edit identity/digest checks. Pre-existing user changes,
concurrently modified files, and unrelated staged work are preserved rather than
absorbed into the commit.

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

### 2.8 `update_goal`
Records bounded evidence for the single current session Goal.

* **Arguments Schema**:
  ```python
  class UpdateGoalArgs(BaseModel):
      action: Literal["progress", "complete"]
      evidence: str = Field(min_length=1, max_length=16_384)
  ```
* **Lifecycle limits**:
  1. The tool cannot create, pause, resume, or clear a Goal.
  2. `progress` stores redacted evidence while keeping the Goal active.
  3. `complete` is accepted only for an active Goal and moves it to terminal
     `complete` state with the supplied verification evidence.
  4. Goal bookkeeping never counts as productive work for automatic
     continuation; at least one non-Goal tool call is required to continue.
* **Safety**:
  Goal updates mutate only Ash-owned session state, so normal interactive/plan
  policy may allow them without an approval prompt. Dry-run mode still denies
  them, and managed/user deny rules retain precedence.

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

After protocol negotiation, Ash paginates `tools/list` and namespaces each remote
tool as `mcp__<server>__<tool>` when that literal name is provider-portable. The
provider-facing name must match `[A-Za-z0-9_-]{1,64}`; when a server/tool identity
contains unsupported characters or would exceed that envelope, Ash derives a
deterministic hashed alias while retaining the exact remote MCP tool name for
wire calls, contract fingerprints, and durable task recovery. For MCP 2026-07-28,
negotiation uses the stateless `server/discover` path rather than `initialize`.
The declared `inputSchema` remains the authoritative provider-facing schema; Ash
does not translate it into a smaller Pydantic type model.

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
* The 2025-11-25 experimental task lifecycle remains supported only on legacy
  connections. Ash does not advertise or execute it on MCP 2026-07-28 because
  Tasks moved to the separate `io.modelcontextprotocol/tasks` extension.

### 3.3 Tool Result Boundary

MCP `tools/call` results require a `content` array. On MCP 2026-07-28 Ash also
requires and consumes the top-level `resultType`; bounded `input_required`
rounds are fulfilled in-band through declared roots, sampling, or elicitation
capabilities and retried with opaque `requestState` preserved. Ash preserves rich content,
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

See the [MCP 2026-07-28 release](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
and the [2025-11-25 tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
for the legacy stateful path. For MCP 2026-07-28, Ash automatically opens a
`subscriptions/listen` stream for every advertised tools/prompts/resources
`listChanged` capability, validates the acknowledged subset and subscription
ID, and routes correlated list changes through the existing bounded refresh
path. Abrupt subscription loss is surfaced as a runtime diagnostic. Resource
updates are explicit rather than implicit: `/mcp watch SERVER URI` opens a
bounded per-URI `resourceSubscriptions` stream, `/mcp unwatch SERVER URI`
closes only that watch, and `/mcp watches [SERVER]` lists the session-scoped
watch set. Legacy 2025 servers use `resources/subscribe`/`resources/unsubscribe`;
active watches are restored across targeted/full MCP reloads and safe HTTP
session recovery, and failed restoration clears stale watch state. Reading a
resource does not auto-subscribe it. The redesigned MCP 2026 Tasks extension
remains unimplemented.

---

## 4. Platform-Specific Command Safety Policies

Ash prioritizes Linux and preserves supported native macOS behavior. WSL2 can
reuse the Linux command/process model experimentally, but its real workflows
have not been verified. Native Windows PowerShell execution is not supported.

Supported hosts enforce workspace-scoped executable resolution, destructive
command blocking, bounded subprocess output, explicit timeouts, process-tree
cleanup, and robust UTF-8 decoding at the central command/process layer.

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
