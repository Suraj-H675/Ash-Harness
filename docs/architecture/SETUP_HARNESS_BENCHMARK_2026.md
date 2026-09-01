# Terminal AI Coding Harness Setup Benchmark

Date checked: 2026-08-27 (Asia/Kolkata)

Scope: first-run setup, provider/model selection, API-key onboarding, local or custom OpenAI-compatible endpoints, profiles/configuration, MCP, and extensions for six terminal-oriented AI coding harnesses. Sources are official product documentation or official repositories only.

## Executive comparison

| Harness | First-run UX | Provider/model and local endpoint handling | Profiles/configuration | MCP/extensions |
| --- | --- | --- | --- | --- |
| Claude Code | `claude` opens browser login; `ANTHROPIC_API_KEY` skips login but asks for approval. | `/model` and `--model`; provider/cloud choices include Anthropic, Bedrock, Vertex, Foundry, and gateways. `ANTHROPIC_BASE_URL` supports a custom gateway/base URL. | User/project/local/managed settings; `CLAUDE_CONFIG_DIR` relocates state. No separate named-profile UX was evident in the reviewed docs. | Native MCP CLI plus `/mcp`; plugins can bundle skills, agents, hooks, and MCP servers. |
| OpenAI Codex CLI | First `codex` offers ChatGPT or another available sign-in method; API-key login is available. | `/model`; user-defined `model_providers` with `base_url`, env key, headers, and `openai_base_url`; local `lmstudio`/`ollama` modes. | Explicit named profiles via `--profile`; user config is authoritative for provider/auth/profile settings. | `codex mcp add/list/login`; stdio and Streamable HTTP. |
| Aider | Configuration-driven: with no explicit model it checks supplied keys and selects a model; without keys it offers OpenRouter setup. | `OPENAI_API_BASE` plus `--model openai/<name>` for OpenAI-compatible APIs; documented Ollama and LM Studio adapters. | Layered CLI, env, `.env`, and `.aider.conf.yml`; model metadata/settings files. No named profiles or native MCP workflow surfaced in the reviewed official docs. | Editor/watch-file integrations are documented; the official MCP feature request remains open. |
| Goose | Desktop welcome choices include API-key quick setup, ChatGPT subscription, OpenRouter, and other providers; `goose configure` is an interactive CLI wizard. | Provider picker, API-key/host prompts, searchable model list; custom OpenAI/Anthropic/Ollama-compatible providers, optional key, headers, streaming, and local runtimes. | YAML config plus environment overrides; no separate named-profile UX was evident in the reviewed docs. | Extensions are MCP-based; built-in, stdio, and remote Streamable HTTP extensions are configured interactively. |
| Gemini CLI | `gemini` can open Google sign-in; `GEMINI_API_KEY` starts API-key onboarding, and Vertex uses ADC/service-account/cloud credentials. | `/model` offers Auto or Manual model selection; `GEMINI_MODEL` is supported. `GOOGLE_GEMINI_BASE_URL` overrides the Gemini API base, not a generic OpenAI-compatible provider. | User and workspace `.gemini/settings.json` scopes; no named-profile command surfaced. | MCP is configured in settings; extensions install from local/GitHub sources and may prompt for secrets. |
| Cline CLI | `cline auth` is an interactive provider/model flow supporting Cline OAuth, ClinePass, BYOK, and local runtimes. | `-P/--provider`, `-m/--model`; official CLI source documents an OpenAI-native provider with API key, model ID, and base URL; Ollama/LM Studio can require no key. | Global/project `.cline` data plus `--config`/`--data-dir`; no named-profile command surfaced. | `cline mcp` is a transport/config wizard; plugins install from file, Git/npm, or local sources. |

## The two closest blueprints: OpenClaw and Hermes

### OpenClaw

OpenClaw treats onboarding as configuration of an entire agent system, not just selection of an inference API. Its [wizard](https://docs.openclaw.ai/start/wizard) has a guided path that detects existing model credentials, local AI CLIs, and reachable Ollama/LM Studio servers; tests detected candidates with a real completion; and saves only a verified route. If detection finds nothing, it offers OpenAI, Anthropic, xAI, Google, OpenRouter, or a broader provider menu. Its classic flow also distinguishes QuickStart, Manual, and Import from another agent, including Claude, Codex, and Hermes.

The rest of the OpenClaw setup surface is similarly staged: workspace, model/auth, gateway, channels, web search, skills/dependencies, daemon, and health check. It supports isolated named profiles via `--profile`, and separate agent workspaces/sessions/auth profiles via `openclaw agents add`. This is a strong blueprint for Ash's setup sections and profile model.

OpenClaw's [provider directory](https://docs.openclaw.ai/providers) is deliberately broad: hosted APIs, gateways/routers, cloud platforms, local runtimes, and third-party integrations coexist as provider entries. Its [configuration UI](https://docs.openclaw.ai/gateway/configuration) renders a form from the live schema, keeps advanced fields collapsed but searchable, validates strictly, and provides a raw JSON escape hatch. Its [tools model](https://docs.openclaw.ai/tools) separates tools, skills, and plugins; plugins can contribute tools, skills, channels, model providers, speech, media generation, web search, and hooks.

**Ash lesson:** use OpenClaw's staged onboarding, detection, verification, profiles, schema-driven settings, and capability/plugin boundary. Do not copy its full channel/gateway scope into Ash's first milestone; make those optional setup packs.

### Hermes Agent

Hermes makes the first-run path unusually compact while still exposing a wide system. `hermes setup --portal` is a one-command setup that opens browser OAuth, stores a refresh token, selects a Nous model, configures the provider, enables the Tool Gateway, and returns the user to a ready `hermes chat` session. Existing providers remain available and can be switched with `/model`. The official [configuration reference](https://nousresearch-hermes-agent.mintlify.app/reference/configuration-options) lists provider routes including OpenRouter, Nous Portal, OpenAI Codex, Copilot/ACP, Anthropic, Z.AI, Kimi Coding, MiniMax, KiloCode, OpenCode, AI Gateway, Alibaba, and custom endpoints.

Hermes then exposes a broad [tool and toolset registry](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/): web search, browser, terminal, files, vision, image generation, TTS, memory, sessions, cron, delegation, code execution, messaging, Home Assistant, and MCP. It supports multiple terminal backends, including local, Docker, SSH, Singularity, Modal, Daytona, and Vercel Sandbox. Its [persistent memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) is bounded, profile-aware, and explicit about what is retained.

**Ash lesson:** offer opinionated “connection packs” that configure provider plus optional hosted capabilities in one path, while preserving granular commands for advanced users. Model selection should be only one part of an agent profile: tools, memory, delegation, sandbox, and delivery behavior belong beside it.

## Ash current-state audit

> Implementation update: the provider catalog, fallback manager, setup status
> screen, and `ash providers list/test` landed after this baseline audit. The
> original rows below describe the pre-implementation gaps; use the target
> blueprint and the live commands to assess the remaining work.

Ash is not limited to four providers today; the setup catalogue has six entries: Anthropic, OpenAI, DeepSeek, Groq, Ollama, and one generic OpenAI-compatible route ([setup.py](../../src/ash/commands/setup.py#L55)). The problem is that they appear as six flat choices and all but Ollama follow almost the same API-key/base-URL/model-probe flow. That makes the product feel smaller than its runtime already is.

| Area | Current Ash behavior | Product gap exposed by the benchmark |
| --- | --- | --- |
| Provider menu | Six hard-coded, numeric-only choices. | No categories, search, favorites, existing-login routes, cloud/local distinction, or capability labels. |
| Auth | API key or no-key Ollama. | No OAuth, subscription/CLI/ACP, AWS profile, GCP ADC, gateway login, keychain, logout, or explicit auth-route choice. |
| Endpoint | Provider-specific base URL override and one OpenAI-compatible custom endpoint. | No protocol selector, custom headers/query parameters, streaming toggle, TLS/proxy settings, or Anthropic/Ollama-compatible custom routes. |
| Model choice | Live `/models`/Ollama discovery with manual fallback. | No intent or capability filters, aliases, context/pricing display, fallback chain, model roles, or cached catalog. |
| Profiles | Named isolated config/credential directories with persistent active selection and one-shot `--profile` override. | Profile-owned tool/policy/memory/agent roles and credential rotation remain. |
| Setup scope | Model, optional web search, and browser. | No guided setup for tools, MCP, memory, agents, permissions, sandbox, channels, gateway, or scheduled work. |
| Visual UX | Plain ASCII headers and numeric prompts. | No progress steps, status cards, arrow-key navigation, search, verification summary, or accessible rich/non-color modes. |
| CLI surface | Parser accepts `ash setup providers`, but the wizard has no providers branch; that command currently falls through to “Setup complete!” without configuring anything. | Setup sections need real behavior and matching tests. |
| Quick mode | `setup_model_provider(..., quick=...)` accepts `quick` but does not change provider setup behavior. | QuickStart needs explicit defaults and a documented scope. |

## What the benchmark consistently does well

Across the harnesses, the winning pattern is not “list every model.” It is a small number of composable decisions with safe defaults:

1. Detect what is already available: environment keys, saved credentials, local servers, installed CLIs, and existing config. Do not download models or silently import tokens.
2. Separate connection route from provider from endpoint from model. “OpenAI” may mean API key, ChatGPT/Codex login, Azure, Bedrock-style gateway, or a compatible proxy.
3. Verify the selected route with a real request before making it the default. Preserve a failed existing default until the user explicitly changes it.
4. Treat local runtimes and routers as first-class choices. Ollama, LM Studio, vLLM, llama.cpp, Docker Model Runner, OpenRouter, LiteLLM, and hosted gateways are different setup experiences even when they share a wire protocol.
5. Store credentials separately from ordinary project configuration and expose status/logout/reset commands.
6. Provide user/project/profile scope. A coding project may need a different model, policy, or tool set from a personal assistant session.
7. Continue onboarding after the provider works: permissions, tools, MCP, memory, subagents, browser, scheduling, channels, and health checks.
8. Keep a complete escape hatch: every interactive choice must map to an inspectable config/env/CLI equivalent.

## Ash target blueprint

### 1. A provider descriptor/catalog instead of a hard-coded menu

Build a registry of declarative provider descriptors. A descriptor should contain:

- identity: `id`, display name, vendor, category, documentation URL;
- wire protocol: OpenAI Chat/Responses, Anthropic Messages, Ollama, Gemini, or another adapter;
- auth routes: API key, OAuth, subscription/CLI/ACP, AWS profile/default chain, GCP ADC, environment-only, exec command, or none;
- endpoint data: default base URL, environment key, custom headers/query parameters, region/project, proxy/TLS options;
- discovery: endpoint probe, static catalog, manual entry, or local-runtime scan;
- capabilities: streaming, tools, vision, reasoning, image/audio, embeddings;
- model metadata: aliases, roles, context window, pricing when known, deprecation, and fallback candidates;
- optional dependency/install hint, without making the base package depend on every vendor SDK.

The initial catalog should expose more choices without creating a bespoke SDK for every vendor:

| Group | Initial entries |
| --- | --- |
| Native/API | Anthropic, OpenAI, Google Gemini, DeepSeek, Groq |
| Routers/gateways | OpenRouter, LiteLLM, Vercel AI Gateway, Cloudflare AI Gateway, OpenCode/AI Gateway-style custom routes |
| Cloud | Vertex AI, Amazon Bedrock, Azure OpenAI/Azure AI Foundry |
| Hosted vendors via generic adapter | Mistral, xAI, Together AI, Fireworks, Cerebras, Cohere, Hugging Face, MiniMax, Qwen/Alibaba, NVIDIA, Perplexity, Z.AI |
| Local runtimes | Ollama, LM Studio, vLLM, llama.cpp, SGLang, Docker Model Runner |
| Custom protocols | Generic OpenAI Chat/Responses, Anthropic-compatible, Ollama-compatible |
| Existing agent routes | Codex/ACP, Claude/ACP, Copilot/ACP, installed CLI/exec adapter, with explicit user consent |

This list is a product catalog, not a promise that every entry gets a native integration in v1. Native adapters should be reserved for protocols/auth flows that genuinely differ; compatible vendors should use descriptors and the generic adapter.

### 2. A staged, resumable setup flow

Recommended interactive flow:

```text
Welcome and current status
  -> Detect keys, saved auth, CLIs, local runtimes, and existing profiles
  -> Choose setup path: existing login | API key | local | gateway | custom
  -> Choose provider category and provider
  -> Choose auth route and endpoint details
  -> Test connection and discover models
  -> Choose model by intent/capability
  -> Configure model fallbacks and agent roles
  -> Choose optional packs: tools | MCP | memory | browser | sandbox | channels
  -> Review exactly what will be saved
  -> Save profile and run doctor/health check
```

The flow should be resumable after a failed network request, and should offer QuickStart, Manual, Repair, Import, and Skip/Configure later. A non-interactive mode should accept the same decisions through flags/config/env for CI and headless machines.

### 3. Profiles and provider commands

Add an explicit profile layer:

```text
ash profile list
ash profile add work
ash profile use work
ash profile show work
ash profile remove work
ash providers list
ash providers add
ash providers test <id>
ash providers login <id>
ash providers logout <id>
ash model
ash models
ash doctor --connect
```

Each profile should own or reference a model route, fallback models, auth source, endpoint settings, default tools, permission policy, memory scope, and optional agent/subagent roles. `--profile` should work on commands and sessions. Adding a provider must not erase an existing provider or silently change the active one.

### 4. A richer custom endpoint experience

Replace the current single custom OpenAI-compatible path with a “Custom endpoint” form:

```text
Protocol: OpenAI Chat | OpenAI Responses | Anthropic Messages | Ollama
Endpoint: https://...
Authentication: API key | environment variable | OAuth/exec | none
Headers/query parameters: optional named values, secrets by reference
Model discovery: endpoint | manual model IDs | both
Streaming: auto | on | off
Capabilities: tools | vision | reasoning | image/audio
Test: auth -> model list -> minimal completion
```

This is directly informed by Codex's custom provider fields and Goose's custom OpenAI/Anthropic/Ollama-compatible flow. It also gives Ash a clean route for providers such as Mistral, xAI, Together, LM Studio, and self-hosted gateways without expanding the dependency graph.

### 5. Model catalog and role-based selection

Keep live discovery, but add a normalized catalog cache and curated fallback metadata. Let users choose:

- Best coding model;
- Fast/low-cost model;
- Reasoning model;
- Local/private model;
- Vision-capable model;
- Model for subagents or summarization.

Show provider, model ID, context size when known, tool/vision/reasoning support, verification status, and pricing only when sourced. Support aliases such as `main`, `fast`, `reasoning`, and `subagent`, plus a fallback chain and an exact-provider allowlist.

### 6. Capability packs after model setup

Provider setup should unlock a second layer rather than ending the wizard. Proposed optional packs:

| Pack | Setup questions |
| --- | --- |
| Tools | Enable terminal/files/web/browser/media; show privilege and confirmation behavior. |
| MCP | Add stdio or remote HTTP servers; show scope, OAuth/headers, trust, and reload status. |
| Memory | Enable bounded persistent memory; choose profile/project scope and retention controls. |
| Agents | Add named agents/subagents; choose model, workspace, tools, and delegation policy. |
| Sandbox | Local, Docker, SSH, or hosted execution; show filesystem/network limits. |
| Channels | Optional messaging/webhook delivery, with per-channel allowlists and pairing. |
| Automation | Cron/scheduled work, leases, logs, and notification destination. |
| Browser | Detect/install browser runtime only after explicit opt-in. |

Hermes and OpenClaw show why these capabilities should be visible in the product model, while their optionality keeps Ash's core setup approachable.

## Prioritized implementation plan

### P0 — make the existing setup feel complete

- Replace the flat numeric menu with a Rich/prompt-toolkit TUI: progress indicator, cards, keyboard navigation, search, back, cancel, `--no-color`, and a final summary.
- Fix `ash setup providers` so it actually manages providers; add command-level tests.
- Make QuickStart semantics real: detect configured routes, choose a verified default, skip optional packs, and say exactly what was skipped.
- Detect environment keys and reachable Ollama/LM Studio-style endpoints before asking the user to type them.
- Add auth-route choices and provider categories while preserving the current six providers.
- Add profiles, status/test/logout, model fallback, and a richer custom endpoint form.

### P1 — broaden the provider surface safely

- Move provider definitions into the registry/descriptor model.
- Add Google Gemini, OpenRouter, Mistral, xAI, Together, Fireworks, Cerebras, Azure, Bedrock, LM Studio, vLLM, llama.cpp, and Docker Model Runner descriptors.
- Add generic OpenAI Responses, Anthropic-compatible, and Ollama-compatible adapters.
- Add normalized model metadata, aliases, capability filters, and per-provider catalog cache.
- Add official OAuth/ACP/CLI adapters only where the provider supports them; never scrape or copy subscription credentials.

### P2 — configure the harness around the model

- Add capability-pack setup for MCP, memory, agents, sandbox, browser, channels, and automation.
- Add import flows for compatible existing configs with a preview and explicit consent.
- Add policy/status screens showing tool privileges, active profile, credential source, and effective model route.

### P3 — schema-driven administration

- Generate an interactive settings UI from Ash's config schema, with common fields first and advanced search.
- Let plugins contribute provider descriptors, toolsets, channels, storage, and auth routes.
- Add health/doctor output in both human and JSON formats, including remediation commands.

## Safety constraints for the redesign

- Never place raw API keys, OAuth refresh tokens, or bearer tokens in project config, session transcripts, model prompts, or diagnostic output.
- Do not infer that a subscription login can be reused through an API endpoint. Integrate official OAuth/ACP/CLI paths only, with explicit consent.
- Make every provider test a bounded, redacted operation and distinguish “credentials detected,” “endpoint reachable,” “model discovered,” and “completion verified.”
- Treat tool, browser, sandbox, channel, and automation setup as privileged actions with visible scope and confirmation.
- Keep user/project/profile scopes explicit and preserve the previous working route until a new route passes verification.

## Observed findings

### Claude Code

- **Setup and credentials.** The [quickstart](https://code.claude.com/docs/en/quickstart) says the first `claude` invocation prompts for browser login. Setting `ANTHROPIC_API_KEY` skips login and prompts for approval; `/login` changes or re-authenticates the account. The [authentication guide](https://code.claude.com/docs/en/authentication) documents Claude.ai, API, Bedrock, Vertex, Foundry, workload identity, and gateway routes, plus `apiKeyHelper` for a shell-provided key.
- **Models and endpoints.** The [model configuration guide](https://code.claude.com/docs/en/model-config) documents aliases, full model/provider IDs, `--model`, and `/model`. `ANTHROPIC_BASE_URL` changes the request destination; a gateway can pass provider-specific model strings through.
- **Configuration and integrations.** The [settings guide](https://code.claude.com/docs/en/settings) defines user, shared-project, local-project, and managed settings. The [MCP guide](https://code.claude.com/docs/en/mcp) documents local stdio and remote HTTP/SSE/WebSocket servers, `/mcp`, project trust, OAuth, and scoped config. The [plugins guide](https://code.claude.com/docs/en/plugins) documents bundled skills, agents, hooks, and MCP servers.

### OpenAI Codex CLI

- **Setup and credentials.** The [CLI quickstart](https://developers.openai.com/codex/cli/) starts with `codex`, sign-in selection, and a first task. The official [authentication documentation](https://learn.chatgpt.com/docs/auth) documents browser login, `codex login --with-api-key`, `codex login status`, logout, and cached credentials in `~/.codex/auth.json` or the OS credential store.
- **Models, endpoints, and profiles.** The [configuration reference](https://developers.openai.com/codex/config-reference/) documents `~/.codex/config.toml`, trusted project config, `model_provider`, `model_providers.<id>`, `base_url`, `env_key`, headers, `openai_base_url`, and local `oss_provider` values for LM Studio or Ollama. It also documents named profiles selected with `--profile`; project-local config cannot override provider/auth/profile settings.
- **MCP.** The official [MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) documents `codex mcp add`, `list`, and OAuth login, with stdio and Streamable HTTP transports. The [official repository](https://github.com/openai/codex) also exposes the CLI and MCP commands.

### Aider

- **Setup and credentials.** The [models and keys guide](https://aider.chat/docs/troubleshooting/models-and-keys.html) describes automatic selection from supplied keys and OpenRouter onboarding when no key is available. The [API-key guide](https://aider.chat/docs/config/api-keys.html) supports CLI flags, environment variables, `.env`, `.aider.conf.yml`, and provider-specific or generic `--api-key` values.
- **Models and endpoints.** The [OpenAI-compatible guide](https://aider.chat/docs/llms/openai-compat.html) uses `OPENAI_API_BASE`, `OPENAI_API_KEY`, and `aider --model openai/<model-name>`. Separate official guides cover [Ollama](https://aider.chat/docs/llms/ollama.html) and [LM Studio](https://aider.chat/docs/llms/lm-studio.html). The [advanced model settings guide](https://aider.chat/docs/config/adv-model-settings.html) documents model aliases and metadata/settings files.
- **Configuration and extensions.** The [configuration guide](https://aider.chat/docs/config.html) documents CLI, env, `.env`, and layered `.aider.conf.yml` files. The official [repository](https://github.com/Aider-AI/aider) documents terminal pair programming and local/cloud models. The reviewed official docs and README do not document a first-party MCP client; the official [MCP request](https://github.com/Aider-AI/aider/issues/3314) is still open.

### Goose

- **Setup and credentials.** The [provider setup guide](https://goose-docs.ai/docs/getting-started/providers/) documents desktop quick setup (including API-key auto-detection) and the CLI flow: `goose configure` → Configure Providers → provider selection → key/host prompts → searchable model selection → “Configuration saved successfully.” The [quickstart](https://goose-docs.ai/docs/quickstart/) describes the same first-use flow.
- **Models and endpoints.** The provider guide documents custom OpenAI-, Anthropic-, and Ollama-compatible providers with display name, URL, optional API key, model list, and streaming support. It also documents LM Studio and other local providers. The CLI can search/select listed models; the guide notes that arbitrary custom model names require Desktop or editing `GOOSE_MODEL` in `config.yaml`.
- **Configuration and extensions.** The [configuration-files guide](https://goose-docs.ai/docs/guides/config-files/) documents YAML configuration, environment-over-config precedence, and keyring/`secrets.yaml` handling; it explicitly advises against putting API keys in `config.yaml`. The [extensions guide](https://goose-docs.ai/docs/getting-started/using-extensions/) treats extensions as MCP servers and supports built-in, command-line/stdio, and remote Streamable HTTP extensions.

### Gemini CLI

- **Setup and credentials.** The current [authentication guide](https://geminicli.com/docs/get-started/authentication/) recommends Google sign-in, documents `GEMINI_API_KEY` with an interactive “Use Gemini API key” choice, and documents Vertex AI ADC/service-account/cloud-key paths. It also documents headless use through cached auth or environment variables.
- **Models and endpoints.** The [model-selection guide](https://geminicli.com/docs/cli/model/) documents `/model`, Auto Gemini modes, Manual model selection, and `--model`. The official [configuration reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md) documents `GEMINI_MODEL` and `GOOGLE_GEMINI_BASE_URL`; the latter is a Gemini API request-base override, not documented as a generic OpenAI-compatible adapter.
- **Configuration and integrations.** The [settings guide](https://geminicli.com/docs/cli/settings/) documents user `~/.gemini/settings.json` and workspace `.gemini/settings.json`, with workspace precedence. The [MCP tutorial](https://geminicli.com/docs/cli/tutorials/mcp-setup/) configures `mcpServers` in those files and uses `/mcp list`/`/mcp reload`. The [extensions reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/extensions/reference.md) documents local/GitHub installation, install-time prompts, keychain handling for sensitive settings, and restart requirements.

### Cline CLI

- **Setup and credentials.** The [authorization guide](https://docs.cline.bot/getting-started/authorizing-with-cline) documents Cline OAuth, ClinePass, and BYOK paths; it states that the CLI uses the same provider flow as the IDE. It also documents no-key local Ollama/LM Studio setups. The [CLI overview](https://docs.cline.bot/usage/cli-overview) documents `cline auth`, `cline config`, `cline mcp`, `cline doctor`, `--provider`, `--model`, `--config`, and `--data-dir`.
- **Models and endpoints.** The official [CLI README](https://github.com/cline/cline/blob/main/apps/cli/README.md) documents provider/model flags and an OpenAI-native setup with `--apikey`, `--modelid`, and `--baseurl`, providing direct evidence for custom OpenAI-compatible onboarding.
- **Configuration and integrations.** The [configuration guide](https://docs.cline.bot/getting-started/config) documents global and project `.cline` data, provider settings, project rules/skills/hooks/agents/plugins, and custom data directories. The [MCP overview](https://docs.cline.bot/mcp/mcp-overview) documents an interactive `cline mcp` wizard for stdio and remote Streamable HTTP/SSE. The [plugins guide](https://docs.cline.bot/customization/plugins) documents project/global installation from files, Git/npm, or local paths.

## Recommendations for Ash setup UX

These are recommendations derived from the observed patterns above, not claims about any harness’s implementation.

1. **Make first run a resumable, explicit wizard.** Offer separate choices for subscription/OAuth, API key, environment-provided credentials, and local runtime. End with a connection test, selected provider/model, and a clear “saved” confirmation; expose a non-interactive equivalent for CI/headless use.
2. **Treat provider, endpoint, and model as separate decisions.** Support a catalog picker plus manual model ID entry. For custom OpenAI-compatible endpoints, collect base URL, optional key, headers, streaming capability, and model ID instead of assuming the public OpenAI endpoint.
3. **Keep secrets out of ordinary project config.** Prefer OS keychain/credential storage or environment indirection, show where non-secret configuration is saved, and provide status/logout/reset commands. Goose, Codex, Claude Code, and Cline all make credential persistence or separation a visible concern.
4. **Provide named profiles and clear scope rules.** Codex demonstrates explicit profiles; Claude Code, Gemini CLI, Goose, and Cline demonstrate useful user/project/config-directory layers. A profile switch should show its provider, model, endpoint, and credential source without copying secrets into project files.
5. **Make MCP/extensions an optional post-provider step.** Offer add/list/status/remove, stdio and remote HTTP transports, OAuth/header/environment inputs, trust/approval information, and a reload/restart hint. Keep project-scoped integrations visibly distinct from user-scoped ones.
6. **Preserve escape hatches.** Every wizard choice should map to documented flags/config/env values so advanced users can repair or reproduce setup without the interactive UI.
