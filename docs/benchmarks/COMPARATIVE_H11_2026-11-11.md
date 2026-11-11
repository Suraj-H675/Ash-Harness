# Exploratory h11 harness comparison — 2026-11-11

**Status: first matched requested model/effort, not a controlled harness
ranking.** Both agents worked on the real
[python-hyper/h11](https://github.com/python-hyper/h11) source pinned to
`31e626c64e1e28db3cd73a6aa0ac057f1b915c18`, immediately before
the historical fix `dff7cc397a26ed4acdedd92d1bda6c8f18a6ed9f`.
The original failed both independent behavioral oracles; the upstream fix
passed both. Neither agent received the fixed checkout or hidden tests.

| Measure | Ash CLI | Codex CLI + Poteto |
|---|---|---|
| Requested model | GPT-6 Luna | GPT-6 Luna |
| Requested reasoning effort | xhigh (new one-shot `--reasoning-effort`) | xhigh (`model_reasoning_effort`) |
| Task | Historical h11 chunk footer validation | Same pinned bug and visible test |
| Run deadline | 170 seconds | 170 seconds |
| Final source scope | Only `h11/_readers.py` | Only `h11/_readers.py` |
| Visible regression | Pass | Pass |
| Hidden regression | Pass | Pass |
| CLI terminal success | **Exit 0; completed** | **Timed out**; no terminal event |
| Total wall-clock time | **83.90 seconds** | **170.00 seconds** |
| Usage reporting | 94,278 prompt / 2,654 completion tokens; 10 model requests | Not available; no completion receipt |

Ash executed its exact authorized `python3 -B qa_visible.py` command;
one file-edit attempt failed and was recovered. The session audit verified,
all protected files remained unchanged, and isolated tmpfs access-only
credentials and scratch workspaces were deleted.

Codex was launched with `codex exec --ephemeral`, workspace-write
sandboxing, a requested network-off setting and no automatic approvals. The
workspace was prepared separately from the same pinned upstream source;
Codex followed the locally installed **Poteto mode** workflow and executed
read-only inspection commands in addition to the visible test. It produced
correct source and passed both oracles **despite being terminated at the
deadline**. Its lack of a completed turn must not be misrepresented as a
coding-quality failure, and there is no trustworthy comparable prompt-token
total for Codex in this receipt.

The setup is **not controlled for** system prompts, installed skills,
multi-agent routing, tool schema/command affordances, full execution
containment, or request-context/turn-token limits. Both runs requested the
same model/effort, but provider inference internals and user-session
conditions were not independently verified to be identical. Only one
matched-requested-model trial per harness is available. This is useful
end-to-end evidence, **not** proof that Ash is faster or more capable in
general.

Evidence, retained privately and outside Git:
`/var/tmp/ash-h11-luna-xhigh-comparator-20261111/result.json` and
`/var/tmp/ash-codex-h11-comparator-20261111/grade.json` plus the separately
stored Codex command event log. The latter is not a full Ash-equivalent audit.

**Next gate:** For a stronger head-to-head, standardize tools and execution
policy as far as each CLI permits, control skill/plugin activation and
subagents explicitly, use identical prompt wording, and run multiple fixed
tasks with repeated trials. Keep scored coding correctness separate from
terminal completion and provider usage.

## Reusable comparison check

Run the local read-only receipt comparison without starting either model:

```sh
PYTHONPATH=src .venv/bin/python devtools/coding_comparison.py \
  --ash-evidence /var/tmp/ash-h11-luna-xhigh-comparator-20261111 \
  --codex-evidence /var/tmp/ash-codex-h11-comparator-20261111
```

The checker refuses mismatched task identifiers or upstream revision pins,
separates source correctness from CLI completion and requested model settings,
and makes unresolved parity conditions explicit. It prints only numeric and
boolean metadata plus a hash of the Codex prompt, not agent messages or
source content. This is **not a Codex runner** and cannot retroactively
verify the Codex sandbox, prompt equivalence, sealed-oracle access or actual
inference route. Those require a controlled future execution setup.

After this original hand-assembled experiment, the dedicated
devtools/codex_repository_benchmark.py runner was added to make future
Codex evaluations reproducible, including a preflight-only mode that
does not contact a model. It now defaults to plain Codex.
This does **not** retroactively upgrade the original Codex receipt's
trustworthiness or erase the differences in tool and sandbox policy.

**Updated comparison protocol:** Following the owner's explicit fairness
requirement, new trials use plain Codex without Poteto, ignore Codex
user configuration/rules, request suppression of host skill discovery,
and send the exact same user task bytes as an Ash run with `--fair-prompt`.
Both retain their native runtime/system instructions and tool safety
boundaries; the comparison receipt verifies actual user-prompt fingerprints.
All earlier Poteto results remain historical exploratory evidence only.

**First plain-Codex h11 trial (2026-11-11):** The pinned h11 fixture
passed red-original/green-reference preflight and was repaired by
plain Codex CLI using requested GPT-6 Luna xhigh, a 170-second deadline,
ignored user config and exec rules, and requested host-skill discovery
suppression. The full user-input SHA-256 was
`5567c1b06b83aa604c1b38bbaa6e60d32c1f0f05c8b3bc27e7b6554c92bf592e`.
It exited **0 in 56.61 seconds**, passed both external oracles, changed
only `h11/_readers.py`, executed the visible test successfully, and
cleaned the temporary source checkout. The JSONL included four completed
shell calls, two of which were not the exact test command; no Poteto
commands were observed. At that checkpoint a matching Ash run with
`--fair-prompt` had not yet been performed, so the 56.61-second
outcome must not be
compared as a controlled speed result against the older Ash 83.90-second
trial using different user input. Private evidence:
`/var/tmp/ash-plain-codex-h11-luna-xhigh-20261111/grade.json`.

**First paired identical-user-input run (2026-11-11):** A stricter
plain-Codex evaluation used the **native binary directly** and an
isolated, disposable HOME/XDG/CODEX_HOME with only an authentication
copy staged on Linux tmpfs. The installed account overlay, personal
plugins, skills, MCP configuration, rules and user-home instructions
were excluded. The source authentication file was not changed, and
the temporary private credential directory was deleted. Ash was run
in its existing isolated benchmark home with the exact same user
input (SHA-256
`5567c1b06b83aa604c1b38bbaa6e60d32c1f0f05c8b3bc27e7b6554c92bf592e`),
the same pinned h11 revision, GPT-6 Luna xhigh requested on both,
and a **170-second deadline**.

| Scored outcome | Ash | Isolated plain Codex |
|---|---|---|
| Completed CLI and both independent oracles | PASS | PASS |
| Protected files unchanged | PASS | PASS |
| Required source changed | `h11/_readers.py` | `h11/_readers.py` |
| Total wall-clock elapsed | **56.57 seconds** | **46.52 seconds** |
| CLI timeout | No | No |

This single pair shows **Codex about 10 seconds faster** on this
specific task, with both solutions correct. It does **not** establish
an overall speed or reliability advantage. Ash's exact-command
authorization, tool interfaces, model context budget and token
accounting are not mirrored by Codex's native shell tools; both
products retain their own system prompts. The request-context and
turn-token limits are not independently matched.

Private evidence:
`/var/tmp/ash-fair-h11-luna-xhigh-20261111/result.json` and
`/var/tmp/ash-isolated-codex-h11-luna-xhigh-20261111/grade.json`.
