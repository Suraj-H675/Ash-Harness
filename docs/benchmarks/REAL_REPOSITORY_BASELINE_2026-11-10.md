# Historical upstream coding regression baseline — 2026-11-10

**Status: early existing-repository evidence, not competitive daily-driver
acceptance.** The two tasks were declared and preflighted against exact public
upstream revisions before each model run. Unlike the earlier synthetic coding
fixtures, each trial operated on an actual public upstream Git checkout.

## Method

- Operator tool: `devtools/repository_benchmark.py`; instructions and
  reproducible pinned source commits are recorded in
  [REAL_REPOSITORY_CODING_EVALUATION.md](../guides/REAL_REPOSITORY_CODING_EVALUATION.md).
- Ash checkout at both trials: `615ab914a1c96b5405889031c51317a4317256c2`.
  Provider: `openai/gpt-6.1-sol` through existing ChatGPT-plan authentication.
- One fresh isolated Git workspace, Ash home, session database, trust decision
  and exact test-command grant per task; Bubblewrap native execution. A
  private access-only account record in Linux tmpfs was deleted afterward.
- 32,768-token request context, 120,000-token turn budget, 24 iteration limit;
  no comparable per-request output-token cap is enforced on this plan route.
- The benchmark fetched two *separate* pinned revisions before inference:
  original, where both oracles must fail, and historical upstream fix, where
  both oracles must pass. It copied **only the original revision** into the
  model's sandbox. The later fixed source and hidden oracle were never
  exposed to the model.
- Acceptance required completed CLI exit 0, permitted source changed,
  unmodified protected files, the exact visible test actually run, both
  independent oracles passing, and session audit verified.

## Results (one trial per repository)

| Public project / task | Original / upstream fix | Visible and hidden oracle | Ash CLI / total time | Model requests | Token usage (prompt / completion) | Result |
|---|---|---|---:|---:|---:|---|
| [python-hyper/h11](https://github.com/python-hyper/h11) chunk footer validation | `31e626c` / `dff7cc3` | Both pass | 33.49s / 38.00s | 6 | 31,910 / 609 | **Pass** |
| [urllib3/urllib3](https://github.com/urllib3/urllib3) header sanitization | `8883fd9` / `96449eb` | Both pass | 31.85s / 39.32s | 5 | 52,532 / 506 | **Pass** |

Both historic **original** checkouts failed their visible and hidden
regressions, and both upstream reference fixes passed them.

In h11, Ash made two successful `replace_file_content` edits to
`h11/_readers.py`, read existing source, and ran the only permitted
`python3 -B qa_visible.py` test. All five tool calls succeeded, there were
no denied/out-of-scope commands, protected files were identical to baseline,
and the audit passed.

In urllib3, Ash made one successful `replace_file_content` edit to
`src/urllib3/connection.py` and ran the same exact permitted visible-test
command after reading source. All four tool calls succeeded; protected files,
audit, and source scope passed. The upstream project ordinarily generates a
version module at build time, so this standalone evaluation supplies a
harmless `urllib3._version` stub inside the QA scripts to test the actual
source module without running a dependency-install step.

Raw evidence on the development host (private, untracked):
`/var/tmp/ash-h11-realrepo-sol-20261110/` and
`/var/tmp/ash-urllib3-realrepo-sol-20261110/`. Each has `result.json`,
`ash.stdout`, and `ash.stderr`. The source checkouts and temporary account
records were destroyed after each run; no credential file entered the
evidence trees.

## Limits and next gate

This is **2/2 single, curated historical regression repairs**, not a robust
completion-rate estimate. The tasks use standard-library-only evaluation
scripts, and their scope is one production module each. Neither result
proves feature implementation, complex multi-file changes in a real project,
long-term context/navigation reliability, interruption/resume, non-Python
build systems, or everyday terminal UX. The baseline has no corresponding
Codex/Claude Code/OpenCode run under matched model, permission, context and
evaluation constraints; it must not be represented as a competitive win.

Next evaluation work should add predeclared multi-module and feature tasks on
existing repositories, repeat enough trials to measure variability, and
run a leading coding CLI comparator under genuinely comparable constraints.

## First two-module historical regression: proxy redirect origin (2026-11-10)

The third curated upstream task, `urllib3-proxy-redirect`, uses the
real `urllib3/urllib3` Git revision
`e349d97f2bf234b95d5dd9e689d056a521dfc3b0` immediately preceding
the fix `01cd60b539f808a65e9c5aa93db1c48292432176`. The original fails
both independently written redirection oracles; the upstream reference fix
passes both. Both `src/urllib3/connectionpool.py` **and**
`src/urllib3/poolmanager.py` are required to change. The visible and hidden
oracles simulate proxy pools locally, without network connections, and check
same-origin Authorization/Cookie preservation, cross-origin removal, Host
regeneration, and default versus nondefault ports.

Three **separate, non-equivalent budget probes** used GPT-6.1 Sol and the
same protected source and permissions:

| Per-request context / turn budget | Result | Model requests | Usage (prompt / completion tokens) | Total time |
|---|---|---:|---:|---:|
| 32,768 / 120,000 | **Fail**: context gate blocked further requests before any edit | 3 | 25,218 / 146 | 20.15s |
| 65,536 / 120,000 | **Fail**: stopped at `token_budget` after partial edits in `connectionpool.py` only | 7 | 97,032 / 809 | 50.33s |
| 65,536 / 250,000 | **Pass**: both production modules changed, exact authorized visible test executed, visible and hidden oracles passed, audit verified | 9 | 179,569 / 1,566 | 77.15s |

The passing trial had 71.00s of CLI execution, no model transport errors,
nine successful tool calls including four file edits and the exact authorized
`python3 -B qa_visible.py` command, no out-of-scope commands, unchanged
protected files, and a completed terminal event with exit status 0.
The two failing trials correctly returned exit 1 rather than falsely
signaling completion. All three kept the audit intact, honored file
boundaries, and removed the temporary workspace and access-only credentials.

Private individual receipts:
`/var/tmp/ash-urllib3-proxy-redirect-sol-20261110/`,
`/var/tmp/ash-urllib3-proxy-redirect-sol-64k-20261110/`, and
`/var/tmp/ash-urllib3-proxy-redirect-sol-250k-20261110/`.
No retained credential files or active model processes remained.

**Interpretation:** This is the first real two-production-module Ash coding
repair independently verified to completion. It is **not** evidence of
reliable completion at a 32K context or 120K total budget. A 250K limit was
needed for this trial, exposing substantial cumulative prompt/context
consumption. Do not fold the changed-budget retries into the earlier 2/2
single-module score or claim that increased budgets improved the harness
itself. Compare with a leading CLI under matched model, budgets, tools, and
permissions before drawing conclusions about operational efficiency.

## First existing-repository feature task: URL auth decoding (2026-11-10)

The fourth curated task is **feature implementation**, not a regression fix:
add `Url.auth_decoded` and `Url.auth_decoded_joined` convenience properties
to an existing urllib3 URL parser. The pinned initial source is
`5454903f272718f19d68d5f78789251bd5c5b443`; the independent historical
reference feature is
`5cf0247049a4baa02e830666de53431e5b575694`. The original failed the
visible and hidden oracles; the reference passed both. The trial required
only `src/urllib3/util/url.py` to change, and prevented edits to the test,
packaging metadata, or any other file.

GPT-6.1 Sol ran with a **32,768-token request context** and a **120,000-token
turn budget**. Ash successfully read the URL parser and visible test, made
two authorized source-edit calls, executed the exact
`python3 -B qa_visible.py` command, and completed the CLI with exit 0.
Both external oracles passed; the session audit verified and protected files
were unchanged.

| Measure | Observed |
|---|---:|
| Independently scored result | **Pass, 1/1** |
| Full trial / CLI duration | 38.34s / 32.94s |
| Model requests | 6 (all completed) |
| Prompt / completion tokens | 41,211 / 601 |
| Authorized tool calls | 5 / 5 succeeded |
| Files changed | `src/urllib3/util/url.py` only |
| Scratch and temporary access-only credential home | Deleted |

Private evidence:
`/var/tmp/ash-urllib3-auth-feature-sol-20261110/`.

This demonstrates a correct, independently graded **single-production-module
feature** through Ash's real CLI. It does not prove performance on open-ended
feature design, multi-module feature changes, new dependencies, or large
existing-project navigation. The earlier large two-module repair's budget
pressure remains unresolved. Competitor-matched tests and repeated trials
remain outstanding.
