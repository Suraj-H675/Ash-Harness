# Real-repository coding evaluation

For the current cross-scenario inventory, stopping rules, and evaluation
priorities, see [Coding evaluation portfolio](../benchmarks/CODING_PORTFOLIO.md).

This is a **curated historical-regression replay** on publicly available,
pre-existing source repositories, not a synthetic project and not yet a
SWE-bench or Codex/Claude Code head-to-head comparison. It exercises Ash's
actual `ash -p --mode auto_edit` CLI against a disposable Git checkout and
tests both visible and hidden behavior outside the model's workspace.

The operator-run entry point is `devtools/repository_benchmark.py`. It
requires Linux, Bubblewrap, Git, the checked-out Ash development environment,
and an existing, authorized ChatGPT-plan registration with enough access-token
lifetime for the chosen trial. Model IDs are confirmed in the account catalog.

## Task provenance

| Task | Public repository | Pinned original | Original upstream fix | Authorized source |
|---|---|---|---|---|
| `h11-chunk-footer` | [python-hyper/h11](https://github.com/python-hyper/h11) | `31e626c64e1e28db3cd73a6aa0ac057f1b915c18` | `dff7cc397a26ed4acdedd92d1bda6c8f18a6ed9f` | `h11/_readers.py` |
| `urllib3-header-sanitization` | [urllib3/urllib3](https://github.com/urllib3/urllib3) | `8883fd900057ee65f553586389ecd70269f05e04` | `96449eb1ac8cfca8fb9605b8bbff7a5fe0081dd2` | `src/urllib3/connection.py` |
| `urllib3-proxy-redirect` | [urllib3/urllib3](https://github.com/urllib3/urllib3) | `e349d97f2bf234b95d5dd9e689d056a521dfc3b0` | `01cd60b539f808a65e9c5aa93db1c48292432176` | `src/urllib3/connectionpool.py` and `src/urllib3/poolmanager.py` |
| `urllib3-url-auth-feature` | [urllib3/urllib3](https://github.com/urllib3/urllib3) | `5454903f272718f19d68d5f78789251bd5c5b443` | `5cf0247049a4baa02e830666de53431e5b575694` | `src/urllib3/util/url.py` |

Each original repository is fetched at the **exact pinned Git commit**, with
no agent access to the later reference fix. Before inference, the benchmark
independently verifies that the visible and hidden oracles fail on the original
revision and pass on the reference fix. If that check fails, **no model
evaluation is scored**. These tasks ask the agent to reproduce behavior, not
to copy a reference patch; alternative correct implementations can pass.

The h11 test covers valid and invalid chunk footers, including fragmented
input. The first urllib3 test covers obsolete header folding, bare CR/LF, NUL
and whitespace. The second urllib3 test is a **two-production-module** proxy
redirect regression: it checks same-origin Authorization/Cookie preservation,
cross-origin sensitive-header removal, destination Host regeneration, and
default versus nondefault port handling with deterministic recording proxy
pools rather than live network connections. Because urllib3 generates its
`_version` module at installation, the evaluation scripts supply a
noncredentialed runtime stub without changing the upstream checkout.
The fixed oracles check behavior, not source text.

The fourth task is an actual **feature implementation**: adding
`Url.auth_decoded` and `Url.auth_decoded_joined` convenience properties to
the existing urllib3 URL parser. Tests verify UTF-8 percent-decoding,
missing versus empty passwords, first-colon parsing, preservation of the
percent-encoded raw `auth` field, and Basic-auth-ready trailing-colon
semantics. Only `src/urllib3/util/url.py` may change. The upstream feature
commit also contained ancillary socks and documentation updates, which are
not part of this deliberately scoped task.

## Example usage

```sh
PYTHONPATH=src .venv/bin/python devtools/repository_benchmark.py \
  --task h11-chunk-footer --chatgpt-model gpt-6.1-sol \
  --context-tokens 32768 --turn-token-budget 120000 --timeout 240 \
  --evidence-dir /var/tmp/ash-h11-replay-001
```

For model/effort-controlled comparisons, add `--reasoning-effort xhigh`
or another effort supported by the chosen model. This passes a one-shot,
model-validated `ash --reasoning-effort` option; the benchmark records the
requested effort and never changes the persistent Ash profile. Earlier
trials that omit the option retain the model's runtime default, which must
not be presented as the same effort setting.

Select **one task per execution** and a new evidence directory each time.
The documented example model must still be in the signed-in account's current
catalog. A maximum of 24 model iterations and a task timeout bound the run.
The ChatGPT-plan Responses route currently does not enforce a request-level
completion-token cap. The trial sends only public repository source and
benchmark instructions to the selected model.

Each trial gets an independent Git workspace, Ash session DB, trust decision,
and an **exact grant** for `python3 -B qa_visible.py`; other command lines
are not approved. The native sandbox is mandatory. A private temporary Ash
home in Linux tmpfs holds a copy of the *access token only*, not the refresh
or identity token, and is removed after the trial. The normal Ash profile and
project checkout are not used as workspaces. Source snapshots refuse changed
tests, other tracked files, unexpected new files, and symlinks.

`result.json` records the model, task and original/ref commit hashes,
preflight red/green outcomes, exact CLI outcome, allowed tool activity, runtime
timings/usage, visible and hidden oracle scores, audit integrity, changed file
paths, and cleanup state. Raw CLI stdout/stderr are preserved privately for
diagnosis. **A passing model message is never sufficient for acceptance.**
The local benchmark additionally records **content-free** tool-result
character counts and, for `read_file`, the numeric requested range and
maximum-size settings (if supplied). These size fields never include the
file path, source content, or credential text. The operational timeline
also includes numeric `context.usage` slices, making inflated repeated file
reads distinguishable from static system/tool-schema overhead.
The grader requires a successful one-shot CLI completion, required source
edits, an executed authorized test, both external oracles passing, protected
files unchanged, and verified session audit.

Ash's `replace_file_content` supports both strictly bounded replacements
and exact, unique whole-line block matching without line bounds. The
recommended mode for a block uniquely identifiable by its old lines,
especially after earlier edits have shifted line positions, is the
unbounded **unique-block** mode. This still rejects duplicates and stale
SHA-256, does not ignore indentation, and uses the same atomic write
guard. A bounded mismatch can offer a **non-mutating** hint for a unique
exact block at a different location; it never silently relocates or
fuzzily applies the edit. Use explicit current bounds when the old block
is duplicated or location is important.

Interpret each trial narrowly. This replay suite contains two real-world
**single-module regression repairs**, one **two-module regression repair**,
and one **single-module feature addition**. It does not yet cover more
substantial feature work, language/build ecosystem diversity,
interruption/resume, repeated reliability, or a fair same-model competitor
baseline. Those remain open acceptance work.

## Repeatable plain-Codex comparator

The optional devtools/codex_repository_benchmark.py uses these same
predeclared tasks, pinned historical source commits, preflight red/green
oracles and protected-file snapshots. It runs against a fresh disposable
checkout and grades after the model exits. Unlike earlier exploratory
Codex + Poteto runs, the current comparator does not invoke Poteto:
the user task is supplied verbatim from the same model-neutral prompt
builder used by Ash when `--fair-prompt` is selected. Codex runs with
`--ignore-user-config` and `--ignore-rules`, and requests disabling host
skill discovery. The runner bypasses account-overlay launchers by resolving
a native Codex executable (`codex-real` when available, otherwise native
`codex`), rejecting script wrappers. It creates a private memory-backed
`CODEX_HOME`, copies only the existing user-owned `auth.json` into it,
and sets separate temporary HOME/XDG runtime and configuration paths.
Installed personal skills, plugins, MCP configurations, Codex rules and
other user profile files are not copied. The authentication copy is
removed when the subprocess terminates; the original file is only read,
not changed. These isolated credentials are deliberately never logged or
stored in the Git repository.
A preflight-only exercise requires no Codex inference:

~~~sh
PYTHONPATH=src .venv/bin/python devtools/codex_repository_benchmark.py \
  --task h11-chunk-footer --model gpt-6-luna --reasoning-effort xhigh \
  --preflight-only --evidence-dir /var/tmp/ash-codex-h11-preflight
~~~

For an actual operator-approved, bounded model run, omit --preflight-only,
choose a new evidence directory, and optionally set --timeout (30–300
seconds). A zero exit means both independent oracles passed, the authorized
source was changed, protected files stayed unchanged, the visible QA command
was observed succeeding, and the Codex CLI emitted a completed turn. A
timeout cannot count as a finished run even if the resulting code passes.
Evidence directories cannot be overwritten, and private Codex JSONL/stderr
logs are retained outside Git. Scratch source checkouts are removed after
grading. The runner copies a temporary authenticated profile into RAM,
but never inherits ambient API-token environment variables and never
mutates the existing personal Codex installation.

For the paired Ash run, use the same task, model, effort, time limit
and a fresh evidence directory, passing `--fair-prompt` to
`devtools/repository_benchmark.py`. The two runners record the actual
user-prompt SHA-256; `devtools/coding_comparison.py` rejects different
user prompts or historical task-objective hashes when both are present.
New receipts also include content-free SHA-256 fingerprints of both
visible and hidden oracle *source*; a mismatch is rejected when both
runners provide these fields. This proves the declared grader text
matches, not identical grader environments or oracle inaccessibility.
The old Poteto trials have no matched actual-user-prompt receipt and
cannot be treated as plain-Codex comparisons.

For new controlled runs, both runners **remove the preflight-only
historical reference checkout before any model call**. Codex also
accepts `--context-tokens 32768`, which requests
`model_context_window=32768` from its native CLI; use the corresponding
`--context-tokens 32768` for Ash. The comparison receipt verifies
that both requested the same window. This does not prove that the
providers account for system overhead identically, and Codex has no
enforced counterpart to Ash's `--turn-token-budget`.

**Comparison limitations:** The shell sandbox and command permissions are
not identical to Ash's exact tool grants. Network blocking and host skill
discovery disabling are requested through Codex configuration, not
independently proven in all cases; access to every host-side file has not
been audited. Only the agent's personal profile is isolated, not every
possible host read path. Both receive identical user-input bytes, but their
product/system instructions, native tools, provider internals and resource
accounting differ. The prompt fingerprint checks user intent equality,
not system-prompt equality. Do not claim a fully controlled head-to-head or
general competitive win from one paired trial.
