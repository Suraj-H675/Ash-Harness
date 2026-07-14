# System Prompts And Protocol Selection

Ash assembles a bounded system prompt from a stable runtime base plus trusted
instructions, activated skills, selected repository context, memory, and
session state. The running implementation in `src/ash/core/loop.py` is the
authoritative template; this document records the contract and invariants
rather than duplicating the full prompt text.

## Stable base

The default prompt identifies Ash, the canonical workspace, and operating
system. It states the least-privilege policy, approval boundary, command guard,
incremental workflow, and context-integrity rules. Repository-controlled
instructions and extensions are appended only after workspace trust succeeds.
The prompt does not claim that untrusted content can override host policy.

Callers may supply a complete custom system prompt through the SDK. Ash still
enforces provider message validation, tool policy, sandboxing, budgets, and
terminal completion independently of prompt wording.

## Capability-selected tool protocol

Exactly one tool protocol is active for a model request.

### Native providers

Providers declaring `ProviderCapabilities(native_tools=True)` receive the
visible tool catalog as native JSON schemas. The prompt tells the model to use
only the provider-native tool interface and to return ordinary assistant text
without XML wrappers. Text is streamed directly to the UI and may safely
contain comparisons, HTML, XML examples, or literal `<call_tool>` strings.
Only validated `CanonicalToolCall` objects from the provider adapter can enter
tool execution.

### XML fallback providers

Providers that do not declare native tools receive this fallback shape:

```xml
<call_tool name="tool_name">
  <arg name="parameter_name">value</arg>
</call_tool>
```

Final user-facing text may be wrapped in `<response>`, and reasoning may use
`<thought>`. The streaming parser emits text and reasoning incrementally,
assembles complete tool calls, preserves literal unknown markup at end of
stream, and rejects a stream that ends inside a recognized tool call or tool
argument. Content inside `<response>` is always literal user-facing text,
including apparent tool XML, regardless of provider chunk boundaries.
Recognized elements are processed in source order. A fallback provider that
emits native calls is rejected rather than silently crossing protocols.

Tool results are returned as a bounded JSON payload inside a `tool_response`
element so the fallback model can correlate success, output, errors,
truncation, and token count with the original call ID.

## Terminal safety

Protocol parsing does not prove that a provider response completed safely. The
loop separately requires a terminal `StreamChunk`, rejects output after that
terminal marker, and normalizes its stop reason. EOF, length truncation,
filtering, refusal, cancellation, rate limiting, errors, and unknown terminal
reasons cannot release pending tool calls. Only a complete terminal response is
converted to the typed `CompletionOutcome` consumed by the agent loop.

This boundary is prompt-independent: a model cannot turn incomplete text into
an executable call by producing a well-formed tag before its stream fails.

## Context ordering

Prompt assembly keeps cache-stable and high-authority content early. Dynamic
sections such as repository excerpts, memory matches, attachments, steering,
and recent state appear later and remain subject to per-section token budgets.
`ash /context` and structured context diagnostics expose selected sources,
truncation, sensitivity, and token usage without persisting attachment bytes or
secrets.

## Security model

Prompt text is guidance, not the security boundary. Ash relies on canonical
path checks, project trust, deny-first permission evaluation, explicit
approvals, sandbox enforcement, secret redaction, bounded tools, durable intent
journaling, and process-tree cancellation. Content read from files, websites,
MCP servers, plugins, agents, and model outputs remains untrusted data unless a
host policy grants the requested action.
