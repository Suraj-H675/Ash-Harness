# Ash Crew: engineering progress and decisions

## Current scope

- Gate R: accepted conditional GO from the prior Ash review. Not repeated in this chat.
- Gate 0: PASS on Linux, 2026-10-26. Phase A Gate 1 identity/config/session seam: IN PROGRESS, not accepted.
- Existing repository: /home/suraj/projects/Ash-Harness, main HEAD df0319432c0914f2df9797794ac5118d953d3153.
- Feature branch and separate worktree: feature/ash-crew at /home/suraj/projects/Ash-Crew, created from the exact accepted main HEAD.
- At startup, local main and the available origin/main tracking ref matched. Remote fetch and hosted CI were not rerun; 16/16 CI is prior review evidence only.
- Original checkout user-owned untracked docs/architecture/ASH_COWORKERS_REA_STUDY_2026_10_26.md and skills/ remain untouched. A byte-identical copy of the research report was placed in this worktree.

## Gate 0: reproducible checks

- Verified pwd, Git root, current HEAD/branch, main/ref equality, Git status, worktree inventory, local instructions, and availability of feature path/branch before mutation.
- Isolated sandbox on development host: /home/suraj/.cache/ash-crew-proof-gate0-20261026 (mode 0700). Its home, config, data, cache, tmp, evidence and workspace directories are independent of the ordinary Ash installation.
- Unit-test interpreter: /home/suraj/projects/Ash-Harness/.venv/bin/python; imported Crew source using PYTHONPATH=/home/suraj/projects/Ash-Crew/src. Tests run with env -i, HOME/XDG_CONFIG_HOME/XDG_DATA_HOME/XDG_CACHE_HOME/TMPDIR set to their respective isolated sandbox subdirectories and PYTHONDONTWRITEBYTECODE=1.
- Baseline modules: tests/unit/test_profiles.py, test_config.py, test_sdk.py, test_automation.py, test_automation_sdk.py, test_session.py and test_policy.py. Result: 422 passed in 37.35 s (pytest -q; no skips or failures).
- Initial isolation error: using a temporary root under the worktree caused two repository-root-discovery tests to find the enclosing worktree instead of the synthetic fixture. That first run had 420 pass and 2 fail. No Ash code was changed; moving TMPDIR outside the repository resolved both failures.
- Tests exercised existing code and mocks only; no live provider, push delivery, OS host sleep, macOS CI, full end-to-end Crew execution, or hosted CI has been verified in this Gate 0 run.

## Decisions and unresolved proof obligations

1. Keep immutable Coworker identity separate from mutable named profiles. Do not equate profile name, environment selection or model output with security authority.
2. Reuse Ash execution, session runtime leases and automation. Do not create a competing tool policy, scheduler or provider/auth implementation.
3. Use separate background run sessions from the canonical visible coworker conversation; publication and policy are not yet implemented.
4. Existing automation webhook support is outbound delivery, not authenticated inbound event admission. HTTP approval futures are in-memory, not a durable Crew approval store.
5. The accepted gates for event deduplication, crash safety, durable approval suspension, provider compatibility and Atlas/Scout vertical slice remain pending.
6. Gate 0 setup: DevSpace opened normally, but its optional review-index action reported disk quota exceeded under /tmp; repository commands and tests worked when TMPDIR was isolated on /home. Do not clear unrelated temporary files.

## Next checkpoint

Finish and independently review Phase A evidence on immutable IDs, explicit owner/profile/effective-config authority, independent sessions and effect interception. Run focused adversarial/restart tests and affected legacy regressions, then update this record with exact files, commands, results, risks, and Crew HEAD. Do not merge unfinished Crew into main.

## Gate 0 seam observations from current code

- AshConfig.load() resolves active_profile_name() from process environment / active-profile marker before reading selected profile TOML and dotenv. It merges explicit defaults with later environment and explicit overrides; a profile-specific database default is not a security ceiling. Concurrent coworker code must not rely on changing this selection globally.
- AshClient.create(config=..., session_id=...) can reuse an already resolved config and explicitly chosen session. Its client locks turns, while the SessionStore maintains a runtime lease; a canonical conversation should not be concurrently mutated by background runs.
- AshLoop tool dispatch first checks permission_policy.evaluate(tool_name, arguments), then optional on_tool_approval/UI approval, records the outcome, and only later marks record.dispatched. This is a candidate interception seam, not yet a durable approval suspension: the callback is not independently authenticated or persisted across restart.
- The existing scheduled automation runner loads a serialized config and creates AshClient without a chosen existing session for each run. It therefore does not provide coworker-owned longitudinal work by itself.
- The candidate seam must be tested across all consequential local, MCP and plugin paths before declaring Gate 4 approval/resume complete.

## Phase A bounded checkpoint (2026-10-27)

- Scope implemented, subject to final review: `src/ash/coworkers.py` stores immutable owner/profile/coworker identity in a versioned local SQLite directory; `src/ash/config.py` adds explicit named-profile effective configuration reads that ignore mutable active-profile selection; `src/ash/core/session.py` adds schema v23 coworker session associations; `src/ash/sdk.py` refuses to start an agent runtime from proof-only coworker configurations; corresponding focused tests in `tests/unit/test_coworkers.py` and an adjusted version-aware legacy session migration assertion.
- Safety decision: current provider/credential and extension readers have not been comprehensively isolated; coworker-bound model runtimes must remain deliberately unavailable. Identity and session persistence are not a claim of safe autonomous execution. The identity/provisioning evidence advances Gate 1 but does not close it.
- Exact regression check (Linux, Python venv from accepted main with Crew PYTHONPATH and sanitized environment): 434 passed in 36.40 s for `test_coworkers.py` plus seven Gate 0 baseline modules, using external `--basetemp` under the isolated Crew cache.
- Additional legacy check: 119 passed in 11.09 s across `test_session_export.py`, `test_session_picker.py`, `test_sessions_cli.py`, `test_automation_cli.py`, `test_recovery.py`, `test_turn_recovery.py`, and `test_agent_approval_channel.py`. Total independent tests run since implementation: 553 passed.
- `ruff check` passed for affected code/tests. `mypy` passed on the four affected source files. `git diff --check` passed. Whole-file `ruff format --check` reported existing formatting differences in multiple large touched files; no broad unrelated formatting changes were performed.
- Independent real-process smoke: process 1 created two coworker records, canonical sessions and per-run sessions in an isolated home; process 2 reopened both after a full interpreter restart, confirmed stable identifiers and run associations despite a changed active-profile marker, and rejected cross-coworker session and wrong-owner access. Both processes passed without a live model or outside accounts. State and the JSON proof are under `/home/suraj/.cache/ash-crew-proof-gate0-20261026/process-proof`.
- Original main still points to `df0319432c0914f2df9797794ac5118d953d3153` and keeps only the original untracked REA report and `skills/`. Hosted CI, macOS, real credential isolation, full repository tests and runtime execution remain unverified.

### Next engineering obligation

Prove the remaining identity authority propagation through every runtime credential, tool and extension initialization path before lifting the explicit runtime block. Then progress to responsibility and authenticated-event admission, conservative recovery and durable approvals in the accepted gate order. Never bypass existing tool permission checks or treat a stored owner ID as independently authenticated user authority.
