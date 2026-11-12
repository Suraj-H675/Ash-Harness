# Ash coding evaluation portfolio

**Owner decision (2026-11-12):** Shift from repeatedly optimizing a single
failed benchmark to breadth-first, evidence-driven coding improvement with
selective deep dives. This is a maintained **inventory and decision protocol**,
not a claim that all scenarios have been run or passed.

## First portfolio: existing cases, not invented benchmarks

These eleven fixtures already have runnable code and grading logic. They **do not
constitute eleven independent real repositories**; synthetic and real-repository
lanes must remain separate. Do not pool their scores into a single win rate.

| ID | Existing task | Lane | What it exercises | Latest evidence / next action |
|---|---|---|---|---|
| R1 | `h11-chunk-footer` | Pinned real repo | One-file parser bug; incremental tests | Historical matched Ash/Codex pair passed; retain as control |
| R2 | `urllib3-header-sanitization` | Pinned real repo | One-file security-oriented normalization | Historical paired pilot passed; retain as control |
| R3 | `urllib3-proxy-redirect` | Pinned real repo | Coordinated two-file redirect/security fix | **Scenario 04: PARKED, unresolved**; prior matched pair: Ash fail / Codex pass (one sample) |
| R4 | `urllib3-url-auth-feature` | Pinned real repo | New public API / URL parsing edge cases | **Initial paired pass for both engines**; one sample, no reliability claim |
| R5 | `click-resource-exception` | Pinned real repo | CLI resource lifecycle, exception propagation and suppression | **Oracle-verified**, original fails and upstream fix passes; no agent result yet |
| S1 | `marker` | Synthetic CLI smoke | Exact file creation and protected-file preservation | Exists; record separately as smoke |
| S2 | `build-run` | Synthetic CLI smoke | Create a script, then execute authorized check | Exists; verify tool execution and terminal completion |
| S3 | `repair-test` | Synthetic repair | Single-file bug and independent hidden check | Exists; current strong-model baseline pending |
| S4 | `multi-file` | Synthetic navigation | Inspect multiple modules but edit one | Exists; do not mislabel as coordinated multi-edit |
| S5 | `two-file` | Synthetic repair | Both billing modules must change | Exists; current strong-model baseline pending |
| S6 | `event-log` | Synthetic integration | Three-module parser, aggregation and rendering | Exists; current strong-model baseline pending |

**Current gaps (not runnable tasks):** add approximately three independently
scoped, pinned real-repository cases before interpreting broad coding quality:
one unfamiliar codebase with debugging/test triage; one multi-file refactor
with stable public behavior; one dependency/interface compatibility change;
and one larger repository requiring targeted discovery. Choose repositories,
base/fix pins and independent visible/hidden oracles only after provenance and
red-original/green-reference verification. This would give a **14-case initial
inventory**, though reliability claims will still require repetitions and
diversity beyond shared urllib3 history.

**Evidence checkpoint:** Before this workflow change, the locally retained
paired receipts showed **R1 and R2 passing for both engines** in a concurrent
pilot and **R3 failing for Ash but passing for Codex** in one matched pair.
An initial **sequential** R4 pair subsequently passed both independent
oracles and completed CLI for both engines (Ash **77.14s**, plain Codex
**79.15s**). The paired runner verified the comparison receipt. These are
single observations, not a stable portfolio pass rate or a 2-second speed
advantage. Their task difficulty, order and runtime conditions differ.

**R5 provenance:** The Click task uses original `pallets/click` commit
`16fe802a3f96c4c8fa3cd382f1a7577fda0c5321` and the upstream
`Context.__exit__` fix `555fa9bb37770a6845a98be60b0c84876775552e`
([PR #3058](https://github.com/pallets/click/pull/3058)). Independently
authored, stdlib-only visible and hidden checks both failed on the original
and passed on the fixed commit. Only `src/click/core.py` is editable. This
establishes a **real runnable evaluation**, not a coding-agent success claim.

## Evaluation and prioritization contract

1. **Freeze the task** before a run: objective, task inputs, source revision,
   authorized tool effects, independent visible/hidden oracles, protected files,
   requested model/effort and context/resource limits. Never rewrite a frozen
   task to rescue a failing agent. Keep private raw receipts outside Git.
2. **Separate grades:** code/oracle correctness; visible test actually run;
   full CLI success; safety/audit/scope; wall time; model requests/tokens;
   repeatability. A correct repair that times out at finalization is not a
   full pass. Exclude provider/routing outages from coding win/loss counts.
3. **Compare fairly:** pair the real-repository lane with plain Codex using
   existing `devtools/repository_benchmark.py`,
   `devtools/codex_repository_benchmark.py`, and
   `devtools/parallel_repository_benchmarks.py`. Match user input, pins,
   requested model/effort, deadline and context; disclose tool, sandbox,
   system-prompt and actual provider-routing differences. Use sequential,
   balanced ordering for latency comparison, not concurrent-load rankings.
   Synthetic fixtures currently have an **Ash-only** runner; do not invent
   matched Codex results or equate local-Qwen runs with GPT-6 Luna trials.
4. **Cluster failure modes:** infrastructure/routing; task understanding;
   discovery and redundant inspection; editing; authorization and side-effect
   boundaries; verification; context/recovery; finalization. Prioritize a
   confirmed cross-task weakness by user impact, frequency, severity,
   safety consequences and fix cost, not recency alone.
5. **Stop and reassess:** after **two unsuccessful fix/retest cycles** on one
   scenario, explicitly decide to change the hypothesis, test held-out cases,
   or park it. Critical security/integrity defects can justify a deeper dive
   with documented evidence. Do not spend another provider-intensive run merely
   to obtain a favorable sample.
6. **Risk-proportional implementation:** focused red/green or invariant tests,
   then related regression checks, then broader gates for meaningful changes.
   Validate consequential harness optimizations on a frozen problem and at
   least one independent held-out task; do not overclaim from synthetic tests.
7. **Close each batch** with one choice: accept, revise, revert, or park. Record
   the result and next portfolio slice. Keep scenario diagnostic transcripts
   in the existing **untracked, locally excluded**
   `docs/benchmarks/ASH_SCENARIO_JOURNAL.local.md`.

## Next portfolio slice

**Now:** no new Scenario 04 run and no immediate R4 retry. The initial R4
matched pair is complete. **R5 Click** adds a third upstream repository with
an independent resource-lifecycle bug. Grade it through one frozen sequential
Ash/plain-Codex pair, then rotate to a different case. Use repetitions only
for a predeclared variance question, not to reverse a disappointing result.

**Don't conflate:** passing filesystem/provider regression tests proves those
subsystem contracts; it does not prove that Ash's overall coding success rate
rose. Conversely, failure of one hard coding fixture does not invalidate a
separately verified security or transport fix.
