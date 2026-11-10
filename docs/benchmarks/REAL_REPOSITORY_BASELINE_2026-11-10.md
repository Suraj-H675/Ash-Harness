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
