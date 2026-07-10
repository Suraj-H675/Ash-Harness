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

The public models are exported from `providers`:

```python
from providers import (
    CanonicalMessage,
    CanonicalToolCall,
    ImageContentBlock,
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
