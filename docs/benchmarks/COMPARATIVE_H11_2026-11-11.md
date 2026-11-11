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
