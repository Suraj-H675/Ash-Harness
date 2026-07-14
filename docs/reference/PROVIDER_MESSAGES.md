# Canonical Provider Messages

Ash validates one provider-neutral message contract before every model request.
Built-in adapters validate again when called directly, then translate the
canonical shape to their provider SDK.

## Message roles

- `system`: text content only.
- `user`: text or typed text/image blocks.
- `assistant`: text or text blocks, plus optional canonical tool calls.
- `tool`: text content and a required `tool_call_id`.

Image blocks accept PNG, JPEG, GIF, or WebP media types and bounded valid base64
data. Images are user input only. Tool calls require a non-empty `call_id` and
name plus strictly JSON-serializable arguments. Orphaned tool results,
provider-specific extra fields, invalid role/field combinations, invalid media,
and histories above 10,000 messages fail before network I/O.

The public models are exported from `ash.providers`:

```python
from ash.providers import (
    CanonicalMessage,
    CanonicalToolCall,
    CompletionOutcome,
    CompletionStopCategory,
    ImageContentBlock,
    ProviderCompletionError,
    ProviderTerminalError,
    TextContentBlock,
    normalize_messages,
)
```

Existing `{"role": ..., "content": ...}` mappings remain accepted. Provider
authors should type `stream_chat` as `Sequence[MessageInput]`; Ash passes a
validated list of plain mappings so adapters in other processes or languages
can implement the same wire contract.

Provider responses use `StreamChunk`. Completed native tool calls are parsed
into `CanonicalToolCall` before the loop sees them: provider `id` aliases become
`call_id`, JSON argument strings must decode to objects, and malformed calls
fail the provider turn instead of being executed with guessed arguments.

## Streaming completion contract

Every `stream_chat` call must emit at least one chunk with `is_done=True` before
the async generator returns. A terminal chunk may be followed only by empty
usage metadata chunks; output after terminal completion is a protocol error.
The first terminal disposition is sticky. Later terminal metadata may supply a
previously absent reason or authoritative usage, but a conflicting stop category
is a protocol error. Stop reasons normalize into one of four categories:

- `complete`: normal text, stop sequence, or native tool completion.
- `truncated`: token/output limits such as `length` or `max_tokens`.
- `filtered`: safety, refusal, or content-filter termination.
- `error`: cancellation, timeout, rate limiting, provider failure, and unknown
  vendor reasons.

Only `complete` produces a `CompletionOutcome`. EOF without a terminal chunk,
post-terminal output, and every other category raise `ProviderCompletionError`.
Pending native or fallback tool calls are therefore never released from a
truncated, filtered, ambiguous, or failed response.

A metadata-only terminal `error` is represented by `ProviderTerminalError`.
Rate-limit and timeout dispositions are eligible for bounded retry and circuit
accounting; failover may select the next protocol-compatible provider because
no model output has escaped. Empty EOF is handled the same way by failover.
Once text or a tool call has appeared, Ash never replays the request through
another attempt or provider.

Providers with `capabilities.native_tools=True` receive JSON schemas and must
return completed `native_tool_calls`. Their textual output is plain text and is
never interpreted as XML. Providers that do not explicitly declare native
support receive the XML fallback instructions; native calls from that path are
rejected as a protocol mismatch. Unknown/custom providers default to the
conservative fallback until their capability resolver declares otherwise.
All providers in one failover chain must declare the same tool protocol because
the system prompt and tool schema are fixed before the request begins.
