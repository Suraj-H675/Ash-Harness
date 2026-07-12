# Hook Contract v1

Ash command hooks are trusted, out-of-process observers and gates. User hooks
live at `~/.ash/hooks.json`; project and project-plugin hooks load only after
the workspace is trusted. User and project hooks run from the active workspace
and receive `ASH_PROJECT_ROOT`. Plugin hooks run with the plugin directory as
their working directory and receive `ASH_PLUGIN_ROOT`.

Each hook command receives one JSON object on standard input. Lifecycle
payloads always contain:

```json
{
  "schema_version": 1,
  "event": "turn_end"
}
```

Event-specific fields are additive. Breaking payload changes require a new
schema version.

## Events

| Event | Role | Important fields |
|---|---|---|
| `session_start` | observer/context | `session_id`, `source`, `project_path`, `model` |
| `session_end` | observer | `session_id`, `reason` (`switch`, `reload`, or `shutdown`) |
| `turn_start` | observer | `session_id`, `turn_id`, redacted `input` |
| `turn_end` | observer | IDs, `status`, redacted `response`, final `usage` when available |
| `turn_error` | observer | IDs, redacted `error` |
| `pre_model` | observer | IDs, `model`, `iteration`, message/tool counts |
| `post_model` | observer | IDs, model/iteration, redacted response, tool-call and usage counts |
| `pre_tool` | critical gate | `tool`, `arguments` |
| `post_tool` | observer | `tool`, `arguments`, `result` |
| `tool_error` | observer | IDs, call/tool, redacted arguments and error |

`pre_tool`, `post_tool`, and `tool_error` entries accept a regular-expression
`matcher` against the tool name. Other lifecycle events do not use matchers.

## Control Responses

Only `pre_tool` can change control flow. Empty output and legacy plain-text
stdout allow the call. A hook denies a call with:

```json
{"decision":"deny","reason":"organization policy rejected this operation"}
```

Malformed structured output, non-zero exit, or timeout from a matching
`pre_tool` hook fails closed. Hooks cannot rewrite arguments after permission
evaluation.

`session_start` may return plain text or structured context:

```json
{"additional_context":"Instructions for this session"}
```

All other hooks are observers. Their failures never relabel or undo completed
work. Ash retains the latest 100 redacted diagnostics and emits a versioned
`hook.error` runtime event.

## Resource And Trust Boundaries

- Commands use argv execution, never a shell.
- The environment is scrubbed to basic operational variables plus explicit
  plugin metadata.
- Each hook has the registry timeout (10 seconds by default).
- Config files, input payloads, and combined stdout/stderr are capped at 1 MiB.
- Injected session context is capped at 65,536 characters.
- Cancellation terminates the hook process group and propagates to the turn.
- Dry-run mode suppresses all hook execution, including lifecycle observers.
- Project and plugin code is never loaded merely by discovering an untrusted
  workspace.

Example:

```json
{
  "pre_tool": [
    {"matcher": "write_.*", "command": ["python", "check_write.py"]}
  ],
  "turn_end": [
    {"command": ["python", "record_usage.py"]}
  ]
}
```
