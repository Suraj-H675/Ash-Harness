# Permissions And Managed Policy

Ash resolves every tool call through a fail-closed permission engine. The
effective decision uses this order:

1. Managed deny rules.
2. User deny rules.
3. Fail-closed modes such as `plan` or `dry_run`.
4. Managed ask rules.
5. User ask rules, read-only defaults, user allow rules, then the active mode.

Managed rules are administrator-owned. Users can inspect them with:

```bash
ash permissions status --json
```

They cannot edit them from `ash permissions`, project configuration, plugins,
or interactive session approvals. A malformed managed file prevents startup
rather than silently ignoring the restriction.

## Policy Locations

Place one or more JSON files in the platform policy directory:

- Linux: `/etc/ash/policy`
- macOS: `/Library/Application Support/Ash/policy`
- Windows: `%ProgramData%\Ash\policy`

Each file uses the same versioned schema as `permission-grants.json`, but keys
workspaces by canonical absolute path. For example:

```json
{
  "version": 3,
  "workspaces": {
    "/srv/app": [
      {
        "id": "managed-web-deny",
        "effect": "deny",
        "tool": "web_fetch",
        "matches": []
      }
    ]
  }
}
```

The `id` must be unique within each workspace. Files load in lexical filename
order; if two files use the same `id` for that workspace, the later file wins.
Keep at most 16 files in the directory. Ash reads only regular files and fails
closed if any unreadable or invalid policy is present.

## User Rules

Users can persist scoped rules for the current workspace:

```bash
ash permissions deny write_file --exact 'file_path=".env"'
ash permissions allow run_command --command-prefix pytest
ash permissions allow write_file --path-prefix file_path=docs --suffix file_path=.md
ash permissions allow read_file --path-glob 'file_path=packages/*/README.md'
ash permissions allow web_fetch --domain url=api.example.com
ash permissions remove RULE_ID
ash permissions clear --yes
```

Command-prefix matching accepts only simple commands. Compound commands,
redirection, substitution, and ambiguous quoting require exact approval so a
prefix grant cannot hide an unrelated command.

Multiple matchers in one rule are AND-combined, including multiple constraints
on the same argument. `--path-glob` matches the entire normalized workspace path
using only `*` (zero or more characters, including `/`) and `?` (exactly one
character); absolute, backslash, or traversal patterns are rejected. Domain rules
are least-privilege: `example.com` matches only that hostname, while
`*.example.com` matches subdomains but not the apex.
