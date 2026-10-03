# Extension authoring

Ash has two extension layers:

- **direct user/project components** for local configuration: instruction
  skills, Markdown commands, custom agents, and hooks;
- **plugins** for a shareable, namespaced unit that can bundle those components,
  MCP servers, dependencies, and isolated executable tools.

Use MCP directly when the capability is already an MCP server. Use a plugin
when several Ash-facing components belong together or when the extension needs
Ash's install/update/provenance lifecycle.

## Plugin layout

A plugin is a regular directory with a root `plugin.json`. Ash rejects linked
plugin roots and linked entries inside a managed plugin tree.

The conventional layout is:

```text
my-plugin/
├── plugin.json
├── skills/
│   └── review/
│       └── SKILL.md
├── commands/
│   └── review.md
├── agents/
│   └── reviewer.md
├── hooks/
│   └── hooks.json
└── .mcp.json
```

When those default locations are used they do not need to be repeated in the
manifest. A minimal current manifest is:

```json
{
  "schemaVersion": 2,
  "name": "my-plugin",
  "version": "0.1.0",
  "description": "Project review helpers"
}
```

Explicit component paths are useful when the files live elsewhere:

```json
{
  "schemaVersion": 2,
  "name": "my-plugin",
  "version": "0.1.0",
  "description": "Project review helpers",
  "skills": ["components/review/SKILL.md"],
  "commands": ["components/review.md"],
  "agents": ["components/reviewer.md"],
  "hooks": ["components/hooks.json"],
  "mcpServers": ["components/mcp.json"]
}
```

Inline command, agent, hook, and MCP declarations are not supported. Keep those
components in files so Ash can validate and snapshot them independently.

Executable tools use the isolated Plugin API v1 rather than importing Ash
internals. See [Plugin API v1](../reference/PLUGIN_API_V1.md).

## Component formats

### Instruction skills

Skills follow the Agent Skills `SKILL.md` format and are loaded progressively.
The directory name must match the skill name:

```markdown
---
name: review
description: Review a change for correctness and regressions
---

Review the requested change. Prefer concrete repository evidence.
```

Plugin skills are exposed as `plugin-name:skill-name`. Standalone user skills
live under `~/.ash/skills/`; trusted project skills live under
`.ash/skills/`.

Executable Python/Markdown legacy skills are not part of the normal extension
workflow. They remain an explicit unsafe compatibility API.

### Markdown commands

Commands are prompt templates. The path supplies the default command name and
`$1`, `$2`, ... plus `$ARGUMENTS` expand the user's arguments:

```markdown
---
description: Review one path
---
Review $1. Additional instructions: $ARGUMENTS
```

Plugin commands are namespaced by plugin name. Standalone user commands live
under `~/.ash/commands/`; trusted project commands live under
`.ash/commands/`. Use `ash extensions commands` for the shared CLI inventory
or `/commands` inside an interactive Ash session.

### Custom agents

Custom agents are Markdown instruction files:

```markdown
---
description: Review correctness and tests
base-role: reviewer
tools: read_file, search_text
---
Review the requested work and report concrete defects with evidence.
```

Plugin agents are namespaced by plugin name. Standalone user agents live under
`~/.ash/agents/`; trusted project agents live under `.ash/agents/`.

### Hooks

Hooks are trusted command observers/gates in `hooks/hooks.json`. They execute
out of process with a scrubbed environment and bounded I/O. See
[Hook contract v1](../reference/HOOKS_V1.md) for event payloads, failure
semantics, and security boundaries.

Standalone user hooks live at `~/.ash/hooks.json`; project hooks live at
`.ash/hooks.json` and require project trust.

### MCP servers

A plugin can bundle an `.mcp.json` file. Plugin MCP server names are
namespaced by plugin name and run from the plugin root with `ASH_PLUGIN_ROOT`
available as plugin metadata. MCP remains the preferred extension boundary for
network services and independently maintained tool servers.

## Development workflow

Validate before activating a plugin:

```bash
ash extensions validate ./my-plugin
ash extensions inspect ./my-plugin
```

`validate` captures an immutable bounded snapshot and applies the same manifest
and component checks used by installation. It does not install or execute the
plugin. `inspect` reports the manifest, components, dependencies, runtime
protocol, warnings, and recorded managed provenance when applicable.

For fast project-local iteration, place the plugin at:

```text
<workspace>/.ash/plugins/my-plugin/
```

Project extensions load only after the workspace is trusted. After editing the
plugin, run `/reload-plugins` in the interactive Ash session. Reload validates
the replacement before publishing it and closes superseded executable-plugin
hosts.

For a user-level installation, Ash copies a validated snapshot instead of
linking the development directory:

```bash
ash extensions install ./my-plugin
```

After further edits to the source tree, replace the installed copy explicitly:

```bash
ash extensions validate ./my-plugin
ash extensions install ./my-plugin --replace
```

Then use `/reload-plugins` in a running interactive session. Ash intentionally
does not provide a symlink-style development install: managed plugin trees
reject links so the validated bytes, installed bytes, and later lifecycle
operations stay inside the same trust boundary.

Local-directory installs are not tracked for remote updates. `ash extensions
update NAME` is for plugins installed from a tracked Git source or signed
catalog.

## Dependencies

`dependencies` expresses plugin-to-plugin version constraints:

```json
{
  "dependencies": [
    {"name": "shared-review", "version": ">=1.2,<2"}
  ]
}
```

Ash validates dependency versions during discovery and lifecycle changes.
Enable/disable, uninstall, replacement, and update operations refuse transitions
that would expose an invalid enabled dependency graph. Coordinated `update
--all` can temporarily quiesce a connected dependency cohort when a valid
migration cannot be performed one plugin at a time.

## Publishing and installation

The smallest remote distribution path is an HTTPS Git repository plus an
explicit ref:

```bash
ash extensions install https://plugins.example/my-plugin.git --ref v1.0.0
```

Signed catalogs add publisher identity, exact source/ref/digest binding,
search, and update provenance. Registering a marketplace requires an
independently trusted Ed25519 publisher key; see the marketplace trust bootstrap
in the root [README](../../README.md).

For installed plugins:

```bash
ash extensions inspect my-plugin
ash extensions update my-plugin
ash extensions disable my-plugin
ash extensions enable my-plugin
ash extensions uninstall my-plugin --yes
```

Use `ash extensions update --all` for all tracked plugins. Managed lifecycle
changes are serialized and journaled so restart recovery can roll back an
interrupted pre-commit replacement or finish cleanup after a committed one.

## Distribution boundary

Direct user/project skills, commands, agents, and hooks are intentionally local
configuration surfaces. Ash does not give each one a separate package manager.
When one of those components should be shared, installed, updated, namespaced,
and provenance-tracked, package it as a plugin.

This keeps one install/update/trust model instead of creating parallel package
managers for every declarative component. A future public ecosystem may index
plugin bundles and skill-focused bundles, but that does not require changing
the local component model.
