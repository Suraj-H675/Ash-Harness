# Security Policy

## Reporting a Vulnerability

Do not disclose suspected vulnerabilities in a public issue. Use GitHub's
private vulnerability reporting or a private security advisory for this
repository. Include the affected version, reproduction steps, impact, and any
suggested mitigation. Remove API keys, access tokens, private prompts, and
repository data from the report.

Maintainers will validate the report, assess affected boundaries, prepare a
fix and regression coverage, and coordinate disclosure after users have a
reasonable upgrade path.

## Security Boundaries

Ash executes tools and integrates with external providers, plugins, hooks, MCP
servers, language servers, browsers, and remote agents. A trusted workspace is
not equivalent to trusted generated content. Reports involving path escape,
command-policy bypass, credential exposure, origin confusion, sandbox escape,
or cross-session data access are especially important.

Only the latest repository version is currently supported for security fixes.
