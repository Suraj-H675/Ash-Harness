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

### Trusted host and OS-account boundary

Ash treats plugin packages, manifests, Git checkouts, workspace and generated
content, filesystem metadata, links, and other external inputs as untrusted.
Security-sensitive lifecycle operations use anchored and no-follow filesystem
handling, bounded immutable snapshots, validation before publication, private
state roots, and transactional replacement to prevent untrusted input from
redirecting Ash outside its intended filesystem scope.

Ash's local filesystem security model assumes that the operating-system
account running Ash and Ash-managed host state are not concurrently controlled
by an adversarial process with equivalent host write authority. Ash does not
provide an application-level isolation boundary against another hostile
process that can arbitrarily modify Ash-managed files or directories using the
same OS credentials as the Ash process. Such an attacker can interfere with
substantially more than plugin lifecycle operations and is outside this local
application boundary.

This boundary does not make plugin code or plugin inputs trusted. Malicious
plugin sources, remote Git repositories, generated content, manifests,
symlinks, path tricks, malformed metadata, oversized files, and dependency
configuration remain untrusted and are validated and confined accordingly.
Environments requiring isolation between mutually adversarial local principals
must separate them using distinct OS users, appropriately isolated
containers/VMs, or separate hosts.

### Browser network boundary

Browser sessions route HTTP(S) and WebSocket traffic through an Ash-owned
loopback policy proxy for the lifetime of that session. The proxy applies
allowed-domain and globally-public-address checks at connection time and
connects to the vetted address without terminating TLS. Chromium's implicit
loopback bypass is disabled for this proxy path. Existing browser URL and
route checks remain defense in depth.

This is a destination-policy boundary for browser HTTP(S)/WS(S) traffic, not a
claim that Chromium is a complete network sandbox. Environments requiring
broader browser network isolation should use an operating-system or container
network boundary in addition to Ash's browser policy.

Only the latest repository version is currently supported for security fixes.
