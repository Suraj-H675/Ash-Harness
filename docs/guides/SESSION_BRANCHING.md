# Durable Session Branching

Ash stores a conversation tree as durable session nodes. A root session has no
parent. Each fork creates a child session containing an immutable snapshot of a
complete prefix of its parent's transcript.

## Stored lineage

Every session exposes:

- `parent_session_id`: the direct parent, or `null` for a root;
- `root_session_id`: the stable root shared by the whole tree;
- `fork_message_count`: the parent prefix copied into the child;
- `branch_name` and `branch_summary`: bounded, secret-redacted metadata;
- `depth`: the node's distance from the root.

`SessionStore.session_tree()` returns nodes in stable parent-first order and
includes each node's direct child IDs. Existing databases migrate to schema 9;
pre-existing sessions become independent roots, and Ash creates a consistent
backup before migration.

## Integrity rules

A fork cannot split an Ash turn or an assistant/tool-result pair. Ash copies the
new session row and transcript prefix in one SQLite transaction, preserves
message token metadata, clears inherited turn IDs, and only carries a context
summary when the complete transcript is copied. A failure rolls back the entire
child.

Retention is tree-aware. Ash deletes a tree only when every node is older than
the retention cutoff, preventing active descendants from becoming orphaned.
Exported JSONL session headers include lineage metadata for auditability;
imports intentionally create detached roots rather than references to sessions
that may not exist in the destination database.

## User interfaces

Interactive commands:

```text
/fork [message-count] [branch-name]
/tree
```

Top-level inspection:

```bash
ash sessions tree
ash sessions tree --session SESSION_ID_OR_EXACT_TITLE --json
```

SDK:

```python
forked_id = await client.fork(
    session_id,
    message_count=4,
    branch_name="alternate implementation",
    branch_summary="Why this path is worth trying",
)
tree = client.session_tree(forked_id)
```

HTTP:

- `POST /v1/sessions/{session_id}/fork`
- `GET /v1/sessions/{session_id}/tree`

JSON-RPC:

- `session/fork`
- `session/tree`

Forking through the SDK or a server adapter activates the new child for the
next turn. Direct storage calls only create the child and do not change a
runtime's active session.
