# Ash parity audit — 2026-09-01

## Blunt answer

No. Ash is not currently on the same overall level as Hermes Agent, OpenClaw,
OpenCode, Claude Code, Codex CLI, Gemini CLI, or Aider.

Ash is a substantial local-first terminal coding harness. Its coding core has
strong coverage: durable sessions, provider-neutral streaming, guarded file and
command tools, approval/policy layers, sandbox integration, MCP, local
provider-backed delegation, automation, an SDK, JSON-RPC, HTTP, ACP, and A2A.
That is meaningful parity in one product slice. It is not product-wide parity.

## What the evidence says

This audit used first-party comparator documentation/source and direct Ash
repository inspection. Ash has 58,000+ source/test lines, 121 unit-test files,
four integration-test files, and four E2E files. The full local suite has
passed, but much of the suite uses deterministic providers and mocks; optional
browser and PTY tests are gated. The repository's “Verified locally” label is
therefore evidence of an Ash-local contract, not evidence of cross-vendor
interoperability or feature equivalence.

The latest bounded command smoke audit exercised the installed CLI's status,
profile, provider catalog, doctor, sandbox, config, storage, metrics, audit,
sessions, plans, cron, permissions, extensions, and agent surfaces. The
browser setup timeout path was also fixed and tested. These checks support the
local reliability claims; they do not change the product-scope verdict.

## Material gaps versus the public baselines

| Area | Ash today | Why this matters |
|---|---|---|
| Product surface | Terminal/TUI, SDK, HTTP/JSON-RPC, ACP, and A2A | OpenClaw is a gateway spanning chat channels, WebChat/Control UI, mobile nodes, media, voice, and routing ([official feature list](https://docs.openclaw.ai/concepts/features)). Ash has no equivalent channel/mobile/media product surface. |
| Provider/auth breadth | Thirteen built-in descriptors, OpenAI-compatible custom routes, local servers, and API-key configuration | OpenClaw documents 35+ providers and subscription OAuth; Hermes documents many hosted/local routes and OAuth options ([OpenClaw](https://docs.openclaw.ai/concepts/features), [Hermes](https://github.com/NousResearch/hermes-agent)). Ash's custom endpoint support is useful but is not the same as native provider integration breadth. |
| Remote/distributed agents | A real provider-backed worker path exists in `SpawnAgentTool`, but it runs in-process; `SubprocessAgent.spawn_subprocess()` launches a simple driver that does not serialize the supplied Python runner | Ash's parity matrix previously called this cross-process capacity. That wording was corrected to Partial. The low-level subprocess API is not proof of provider-backed OS-process isolation. |
| Ecosystem | Local plugins, skills, hooks, MCP, and signed catalog support | OpenClaw and Claude Code expose large plugin/marketplace ecosystems; Gemini documents packaged extensions containing MCP, commands, themes, hooks, sub-agents, and skills ([Gemini extensions](https://geminicli.com/docs/extensions/), [Claude extensions](https://code.claude.com/docs/en/features-overview)). Ash has lifecycle machinery, not comparable ecosystem scale or curation. |
| Client and operations breadth | Linux/macOS CI, a TUI, optional browser automation, and local automation | Codex documents CLI plus IDE, cloud, remote, plugins/marketplaces, review, and automation surfaces; OpenClaw documents gateway operations and mobile/desktop clients ([Codex CLI](https://developers.openai.com/codex/cli/features), [OpenClaw docs](https://docs.openclaw.ai/)). Ash's Windows workflow is intentionally deferred and its browser cannot attach to an existing browser through CDP. |
| Interaction modalities | Text, structured output, images as model attachments, and browser screenshots | The compared products document voice/audio/media, richer computer-use or web surfaces, and in some cases video/image generation. Ash does not provide those as a complete user-facing product. |
| Pair-programming maturity | Strong guarded coding primitives and repository map | Aider documents 100+ language support, automatic Git commits, IDE use, images/web pages, voice-to-code, and automatic lint/test loops ([Aider features](https://aider.chat/)). Ash has pieces of this workflow but not the same mature, user-proven ecosystem. |

## Positioning that is safe to claim

The accurate claim is:

> Ash is a strong local-first coding harness with broad safety, persistence,
> extensibility, and integration primitives, but it remains a WIP and is not a
> feature-equivalent replacement for the full Hermes/OpenClaw/OpenCode/Claude
> Code/Codex/Gemini/Aider product families.

The next major parity work should be deliberate architecture work around a
gateway/channel model, provider/auth adapters, remote worker execution, and
client/media surfaces. Those are not safe to claim complete from unit tests or
to implement piecemeal without a product decision.
