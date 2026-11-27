# Real-repository coding evaluation

For the current cross-scenario inventory, stopping rules, and evaluation
priorities, see [Coding evaluation portfolio](../benchmarks/CODING_PORTFOLIO.md).

This is a **curated historical-regression replay** on publicly available,
pre-existing source repositories, not a synthetic project or SWE-bench.
The optional plain-Codex comparator is a **qualified operational comparison**,
not a fully controlled benchmark of the underlying coding models. It exercises
Ash's actual `ash -p --mode auto_edit` CLI against a disposable Git checkout
and grades visible and hidden behavior outside the model's workspace.

The operator-run entry point is `devtools/repository_benchmark.py`. It
requires Linux, Bubblewrap, Git, the checked-out Ash development environment,
and an existing, authorized ChatGPT-plan registration with enough access-token
lifetime for the chosen trial. Model IDs are confirmed in the account catalog.
The runner uses the interpreter with which it was launched, rather than
requiring a second `.venv` in every isolated worktree; the evaluated checkout's
`src` remains selected through the explicit `PYTHONPATH=src` source overlay.

## Task provenance

The table below shows the initial four tasks and the R17 pytest fixture.
It is **not** the complete portfolio: the authoritative list of currently
runnable task IDs and pinned revisions is `TASKS` in
`devtools/repository_benchmark.py`, with status tracked in the portfolio.

| Task | Public repository | Pinned original | Original upstream fix | Authorized source |
|---|---|---|---|---|
| `h11-chunk-footer` | [python-hyper/h11](https://github.com/python-hyper/h11) | `31e626c64e1e28db3cd73a6aa0ac057f1b915c18` | `dff7cc397a26ed4acdedd92d1bda6c8f18a6ed9f` | `h11/_readers.py` |
| `urllib3-header-sanitization` | [urllib3/urllib3](https://github.com/urllib3/urllib3) | `8883fd900057ee65f553586389ecd70269f05e04` | `96449eb1ac8cfca8fb9605b8bbff7a5fe0081dd2` | `src/urllib3/connection.py` |
| `urllib3-proxy-redirect` | [urllib3/urllib3](https://github.com/urllib3/urllib3) | `e349d97f2bf234b95d5dd9e689d056a521dfc3b0` | `01cd60b539f808a65e9c5aa93db1c48292432176` | `src/urllib3/connectionpool.py` and `src/urllib3/poolmanager.py` |
| `urllib3-url-auth-feature` | [urllib3/urllib3](https://github.com/urllib3/urllib3) | `5454903f272718f19d68d5f78789251bd5c5b443` | `5cf0247049a4baa02e830666de53431e5b575694` | `src/urllib3/util/url.py` |
| `pytest-mixed-indirect-fixture-precedence` (R17) | [pytest-dev/pytest](https://github.com/pytest-dev/pytest) | `fd47511afeb024d2fbad9257d1b44310672c6a0f` | `6b6502e94b78b41951b70265a84187a419e8ce8c` ([PR #13976](https://github.com/pytest-dev/pytest/pull/13976)) | `src/_pytest/fixtures.py` and/or `src/_pytest/python.py` |

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

**R17 (pytest)** specifically tests mixed direct/indirect parametrization
when a direct argument shares a name with a parametrized fixture. It preserves
`indirect=True`, `indirect=False`, missing-indirect-fixture diagnostics and
ordinary parametrization. Unlike earlier tasks, a one-file repair in either
allowed production module can pass. Upstream test files, the visible test and
the fixture runtime overlay remain protected. R17's historical baseline must
fail both visible and hidden grading and the pinned reference must pass both;
R1–R16 retain their existing rule that at least one original oracle must fail.

The R17 task is **Linux-only** for its model-driven replay. Its system
`python3` command must resolve to `/usr/bin/python3`, meet the upstream
Python **3.10+** requirement, and match the actual grader interpreter; the
preflight records the exact system interpreter version. The source checkout
requires generated ignored version metadata plus a bounded source-only
runtime overlay. The overlay consists of 380 whitelisted pure-Python files
sourced from the active locked development venv, checked against installed
`RECORD` and separately frozen source-byte fingerprints. These fingerprints
are chief-observed installed source digests, **not** an independent
cryptographic verification of PyPI wheel artifacts; changes require
preflight reproving and updated provenance. No untrusted installer or network
fetch is used to build the overlay.

R17's candidate-executed hidden **probe** receives only the candidate-visible
cases and emits observations; the expected observations live in the trusted
parent grader, outside candidate-visible process arguments and files. Its
hidden test is authored for this task, **not** a previously unseen independent
generalization holdout. Independent chief-owned holdout checks are maintained
separately and never sent to the implementing agent. A malicious candidate
may still spoof or infer probe observations; this fixture does not claim to
prove adversarial resistance in every case. The authenticated task inputs,
source and oracle fingerprints, visible grant and frozen checker all belong
to the comparative test contract. No paid R17 model-driven comparison had
run when this documentation entry was written.

## Example usage

```sh
PYTHONPATH=src .venv/bin/python devtools/repository_benchmark.py \
  --task h11-chunk-footer --chatgpt-model gpt-6-luna \
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
files unchanged, and verified session audit. The shared Ash/plain-Codex
source-tree snapshot includes regular-file permission bits as well as content
hashes; changed protected executable/read/write modes, symlinks, special file
entries (including named pipes), and unreadable files invalidate the
protected-file score rather than being ignored. These checks do not measure
the behavior or security of the model itself.
Required source changes still demand altered source **content**: a chmod-only
change to an allowed source does not satisfy the required-edit gate or the
reference-fix preflight. Historical source hashes and source-path receipts
are not interpreted as evidence of changed code merely from permission bits.

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

Interpret each trial narrowly. The *original four* replay tasks consisted
of two single-module repairs, one two-module repair and one feature addition.
The portfolio now contains substantially more pinned tasks, including R17;
the real-task inventory and evidence status are maintained in
`docs/benchmarks/CODING_PORTFOLIO.md`. Requested model/effort/context,
source pins and user prompt may match between engines while effective system
instructions, native tools, command permissions and other limits do not.
Paired reports therefore distinguish `comparison_verified` (the checked
receipt invariants) from `fully_controlled: false` and explicitly list the
operational parity caveats. Do not infer repeat-run reliability or a product
coding win from preflight validity, source-based tests or one paired sample.

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

For an actual **separately owner-approved** bounded model run, omit
`--preflight-only`, add the required `--authorize-model-trial` opt-in, choose a
new evidence directory, and optionally set `--timeout` (30–300 seconds). The
model-using plain-Codex runner also requires its independently attested Linux
x86_64 sealed managed sandbox profile; unsupported platforms or changed local
policy/native binary fail closed before accessing model credentials. The
one-time attestation and flag are not general permission to run further paid
trials. A zero exit means both independent oracles passed, the authorized
source was changed, protected files stayed unchanged, the visible QA command
was observed succeeding, and the Codex CLI emitted a completed turn. A
timeout cannot count as a finished run even if the resulting code passes.
Evidence directories cannot be overwritten, and private Codex JSONL/stderr
logs are retained outside Git. Scratch source checkouts are removed after
grading. The runner copies a temporary authenticated profile into RAM,
but never inherits ambient API-token environment variables and never
mutates the existing personal Codex installation.

The paired coordinator (`devtools/parallel_repository_benchmarks.py`) also
requires `--authorize-model-trial` **before** it refreshes credentials, creates
batch evidence, or dispatches either engine. After explicit owner approval for
the named task(s), number of repetitions, and per-engine budget, an example
single-pair invocation is:

~~~sh
PYTHONPATH=src .venv/bin/python devtools/parallel_repository_benchmarks.py \
  --task h11-chunk-footer --parallel 1 --repetitions 1 \
  --timeout 170 --context-tokens 32768 --authorize-model-trial \
  --evidence-root /var/tmp/ash-approved-paired-new-evidence
~~~

Each pair's `summary.json` (also included as a result in `batch.json`)
preserves existing
`ash_pass` and `codex_pass` as **full-task** grades. The additive
`engine_outcomes` section separately records each candidate's independently
graded source correctness, whether the exact required visible test ran,
whether the CLI turn completed, and whether a timeout was actually recorded.
A correct repair with a green oracle but an unfinished/timed-out turn remains
**a failed full task**, not a win. A missing or malformed timeout field is
`null`, not invented as `false`; unverified/incomplete receipts do not claim
engine outcomes. `comparison_verified` verifies comparability of the recorded
inputs and provenance, not that either coding task succeeded.

New Ash receipts (`completion_evidence_version: 1`) additionally require a
recorded successful terminal sequence. Ordinary completion, explicitly
requested command checkpoints, and bounded context recovery each have distinct
accepted terminal sequences; an absent event, incomplete stop, or inconsistent
restart count cannot prove completion. The comparator checks this evidence
rather than trusting the claimed grade. Older receipts retain their original
rubric and report unknown completion verification.

New receipts (`verification_order_version: 1`) disqualify a full workflow pass
when `observed_edit_after_passing_test` is `true`. The separate semantic grade
still tests the final source. The order field means a successful observed edit
follows the last passing exact QA command (`true`), none was observed (`false`),
or no passing test or usable trace exists (`null`). Recorded patch dry runs
do not count as edits. `verification_freshness` is
`observed_stale` for the first case and `unknown` otherwise: neither Ash edit
receipts nor native Codex file-change events attest tested source hashes. Shell
commands can mutate source without an explicit edit event. A later successful
exact QA command resets the observation; a failed retest does not. This is a
symmetric observed-order requirement, not proof of final-source verification.
The comparator recomputes order evidence and rejects mixed or inconsistent
rubrics; historical receipts retain their original grades.

The flag authorizes **every Ash and Codex model run in the selected batch**
(up to six pairs), not merely one child. Never reuse it without a fresh
correspondingly scoped owner approval. Missing authorization fails without
starting either engine; `--authorize-model-trial` is forwarded to the Codex
child, not passed as an unsupported Ash option. A valid batch still cannot
claim matched tools, system prompts, or permission semantics solely because
model, effort, source, user prompt and context settings were matched.

For the paired Ash run, use the same task, model, effort, time limit
and a fresh evidence directory, passing `--fair-prompt` to
`devtools/repository_benchmark.py`. The two runners record the actual
user-prompt SHA-256; `devtools/coding_comparison.py` rejects different
user prompts or historical task-objective hashes when both are present.
New receipts include content-free SHA-256 fingerprints of the visible
and hidden oracle definitions; a mismatch is rejected when both runners
provide them. R17 additionally fingerprints its candidate-visible probe and
a combined probe/parent-expectations payload, without putting the latter in
the candidate checkout. This proves declared grader material matches, not
identical grader environments or immunity to crafted output.
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
not identical to Ash's exact tool grants. The bounded Linux x86_64 comparator
now checks a managed-profile and native-binary attestation and runs a model-free
sandbox probe for workspace access, outside-file denial and socket denial
before model dispatch. This is a verified *bounded* isolation preflight, not
proof that all host read paths or effective execution grants match Ash's. The
plain comparator also requests suppression of user-owned skills and rules;
there is no established exact parity of system instructions, native tools,
provider internals, permissions or resource accounting. macOS model-driven
plain-Codex trials fail closed rather than claiming equivalent confinement.
The prompt fingerprint checks user intent equality, not system-prompt equality.
Do not claim a fully controlled head-to-head or general competitive win from
one paired trial.
