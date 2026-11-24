# Closing the self-improvement gap (plan, 2026-11-24)

Status: proposal, nothing implemented. Baseline: Lilian Weng, *Harness Engineering for Self-Improvement*
(2026-07-04, <https://lilianweng.github.io/posts/2026-07-04-harness/>) compared against this checkout at
`6a77d89`. Read `docs/architecture.md` and `apps/brain/AGENTS.md` first; this plan only adds to them.

## 1. What the paper actually claims

Practical near-term recursive self-improvement happens in the **harness** — the code around the model that
runs the loop, manages context, keeps state in files, spawns sub-agents, and evaluates itself — before it
happens in model weights. Optimize the object in this order, hardest last:

| Rung | Object optimized | Work cited |
| :--- | :--- | :--- |
| 1 | prompts → structured context → skills | ACE, MCE, Meta-Harness |
| 2 | workflow graph, synthetic data | ScientistOne, Autodata, ADAS, AFlow |
| 3 | harness **code**: tools, middleware, sub-agents, memory | STOP, Self-Harness, AHE, Lin et al. 2026 |
| 4 | a population of harnesses | AlphaEvolve, ShinkaEvolve, DGM |
| 5 | harness + model weights together | SIA, Continual Harness |

Three patterns carry most of the value, and they are cheap: (a) an explicit plan→act→verify→improve loop;
(b) the filesystem as the durable memory, read on demand by `grep`/`read` instead of being pasted into the
prompt; (c) sub-agents and background jobs whose status and output are files, not chat.

Two results matter more than the rest for us:

- **Self-Harness:** mine failures into *causal* patterns (same verifier exit can have different causes), make
  **bounded** edits, and accept one only when both a held-in split is fixed and a **held-out** split shows no
  regression.
- **AHE:** three observability pillars — every editable component has a file, raw traces are summarised into
  a hierarchy (per-trace report → benchmark overview), and every edit is a falsifiable prediction checked next
  round. Evaluator, tracer and model config are **read-only** to the loop, or the loop reward-hacks them.
- **Lin et al. 2026:** writing harness edits is easy for small models; *benefiting* from them is not, and an
  unused or bad skill makes things worse. So skills must earn their place.

Weng's own open problems: fuzzy evaluators, memory lifecycle, negative results, diversity collapse, reward
hacking, long-term repo health, and human oversight moving up the stack rather than leaving.

## 2. Where Jarvis stands

Position: **~Rung 2.5–3, roughly 55–60% of practical harness-level RSI.** Ahead of most published systems on
safety and deployment (git worktrees, Seatbelt, byte-verified scouting, post-upgrade rollback), behind on
observability and on closing the loop.

| Dimension | Already there | Gap | % |
| :--- | :--- | :--- | :--- |
| Isolation, runtime, jobs | worktrees, `sandbox.py`, `edit_and_run`, timers, live cost/timeout kill | hypothesis children run one at a time; `auto_research_after_strikes=0` | 90 |
| Anti-reward-hacking | `PROTECTED_SELF_PATHS`, blocked test edits, sealed held-out and voice benchmarks, SHA-pinned approvals, 2-failure rollback | `brain/orchestrator.py` is not in `VOICE_ROUTING_PATHS`, so the router has no gate | 85 |
| Context, skills, memory | ACE-style `append` + `expected_sha256` + 2-attempt evidence + auto-rollback; `MEMORY.md` 150 lines; verified *and* negative notes; byte-verified `research_scout` | skills are static markdown with no lift measurement; voice Mem0 and brain `MEMORY.md` are two memories | 70 |
| Observability | `traces/attempt-<id>.json`, `mine_failure_patterns`, `propose_harness_task`, `feedback/intent.jsonl` | interactive T3/`pi`/voice turns write **no** traces; surface attribution is a 15-line `if/elif` on exit codes; predictions never verified; rejected proposals forgotten | 45 |
| Triggering, meta-loop | `run_skill_improver` | runs only in `AUTONOMOUS` while `default_mode=ASSIST`, so **it does not run by default**; no meta-metric for whether it helps | 30 |
| Population search | `run_auto_research` 2–3 children, `METRIC:` comparator | one global `harness-proposal` slot; word-Jaccard novelty; no Pareto (accuracy vs latency vs cost) | 35 |
| Weight updates | implicit corrections logged, `laya`/`jev` dual answers, Whisper LoRA script | entirely manual; the Improver is told to skip speech-check misses | 25 |

The single most uncomfortable line: **most real usage (daytime, `ASSIST`, T3) teaches the board nothing**, and
the reflection pass that would learn from it is gated behind a mode we do not run in.

## 3. Plan

Five phases, in this order, because each phase makes the next one measurable. Do not enable a later phase's
autonomy switch before its gate exists (see §4).

### Phase 1 — Experience and component observability

Status: **built** (2026-11-24). `apps/brain/src/brain/traces.py`, `trace_runs`/`trace_fts`, nightly `ASSIST`
reflection, `jarvis traces`, and cockpit cold start. Deep-dive research: `docs/research/observability-landscape.md`.
Detailed build spec: `docs/observability-plan.md`.

Goal: every run, including daytime and voice, leaves a small file the Improver can read.

1. Write `traces/run-<run_id>.json` from `BrainOrchestrator.route` (`apps/brain/src/brain/orchestrator.py`)
   and from `server.py` run handling: route chosen, runner, latency, cost, outcome, and the implicit friction
   signals that already exist — `replace=True` mid-run, `stop_agent`, a re-asked question, the user opening
   T3 to finish it (DemoEvolve's demonstration signal). Same `strip_secrets` + `0600` treatment as
   `write_attempt_trace`.
2. Replace `_surface_for_trace` (`apps/brain/src/brain/skills.py`) with a declared `HARNESS_SURFACES` map:
   `brain_router` (`orchestrator.py`), `voice_prompts` (`prompts.py`, `SYSTEM.md`), `voice_intent_data`
   (`feedback/intent.jsonl`), `whisper_asr_data` (Whisper vs `[Cleanup]` edit distance), `builder_prompt`
   (`_build_builder_prompt`), `skills`, `memory` (Mem0, `MEMORY.md`).
3. On every failed or interrupted run, store a 5-line causal record in the trace: verifier-level cause,
   causal agent step, target surface. Self-Harness's point is that "timed out" is not a cause.
4. Run reflection in `ASSIST` too: a default 03:00 local timer that calls `run_skill_improver` when
   unreflected runs ≥ 5. Skill and memory edits may apply themselves as they do now; a harness proposal stays
   a `draft` unless `AUTONOMOUS_HARNESS_UPGRADES=true`.

Gate: unit tests for the trace schema, secret stripping, and a nightly reflection that fires with the board
still in `ASSIST`.

### Phase 2 — Evaluator coverage and held-in / held-out splits

Goal: nothing that can change behaviour ships without a number.

1. `scripts/bench_brain_route.py`: `BrainOrchestrator.route` over `delegate_project`, `ask_ephemeral_pi`,
   `queue_board_task`, `list_sessions`, `reply_direct`; ≥95% and a p50 latency budget. Add
   `apps/brain/src/brain/orchestrator.py` to the gated paths in `reconciler.py` and the script to
   `PROTECTED_SELF_PATHS`.
2. `scripts/bench_voice_prompts.py`: `TranscriptCleaner` and `_retell` against invariants — outcome ≤ ~15
   words, technical terms preserved, no tool calls in a retell, option headings kept, at most four phrases.
   Register it for `SYSTEM.md` overrides (`cleanup`, `retell`, `waiting`) in `system_prompts.py`, which today
   can replace prompts with no check at all.
3. Auto-harvest cases (`apps/brain/src/brain/evals.py`): confirmed corrections from `feedback/intent.jsonl`
   and friction records from Phase 1 traces. `sha256(text) % 5 == 0` goes to a sealed
   `JARVIS_HOME/evals/held_out/`, the rest to an editable `held_in/`. `_run_voice_benchmarks` runs both from
   outside the candidate worktree; `sandbox.py` denies workers the held-out folder.
4. Any candidate touching router or voice prompts must fix a held-in case **and** show zero held-out
   regression.

Gate: a deliberately bad router edit fails Phase 2 in CI, and a good one passes.

### Phase 3 — Closed decision loop and one memory

Goal: the Improver remembers its own history, and Voice and Brain stop disagreeing.

1. Per-surface dedupe `harness:<surface>` instead of the single `HARNESS_DEDUPE_KEY`, with the manifesto
   stored in outbox details: `target_surface`, `baseline_failure_rate`, `predicted_fix`,
   `at_risk_regressions`.
2. Feed the last ~10 proposals and their outcomes (`pushed`, `failed_verify`, `rejected`, `rolled_back`) into
   the Improver prompt, so a rejected or failed edit is never re-proposed unchanged.
3. Falsifiability: extend `_check_post_upgrade_health` beyond "two tasks did not crash" — after K=10 runs on
   the target surface, compare its failure count to the recorded baseline and queue a rollback recommendation
   when it rose, quoting the falsified prediction.
4. One memory: inject `memory/MEMORY.md` into the router and the first message of a `T3Runner` thread so
   daytime coding knows what the night learned; add an Improver-only `curate_voice_memory` RPC that reads
   recent Mem0 entries, merges duplicates, drops contradicted facts, and promotes durable engineering facts
   into `MEMORY.md` — the open risk in `docs/DECISIONS.md`.

Gate: integration test that a rejected proposal blocks a duplicate, and that curation respects the Mem0
namespaces (`sang` vs `speaker:*`).

### Phase 4 — Skills that earn their keep, and a Pareto tournament

Goal: measure skills, then choose between candidates on more than accuracy.

1. MCE-lite: `skills/<slug>/` may hold a read-only `context.py` or `check.sh` beside `SKILL.md`, run inside
   `sandbox-exec` with `read,grep,find,ls`. A skill becomes a context function, not a paragraph.
2. Marginal lift: compare pass rate and mean turns/cost with the skill loaded against the repo baseline; after
   10 loads with no lift, retire it. This is the Lin et al. filter, and it fixes "a placebo skill never fails,
   so rollback never fires".
3. `auto_research_after_strikes = 2`; replace word-Jaccard `spec_similarity >= 0.8` with cosine ≥ 0.88 on the
   `bge-m3` embeddings already pinned in RAM by `apps/voice/src/jarvis_voice/memory.py` (no new dependency).
4. Mark `ROUTER_PROMPT` and `SYSTEM.md` sections as bounded evolve blocks, and let the tournament pick on
   `(accuracy, p50_ms, cost_usd)` rather than accuracy alone.

Gate: a candidate that buys 0.5% accuracy for >150 ms of voice latency is rejected by the tournament.

### Phase 5 — Weights, and a metric for the Improver itself

Goal: close the parametric half and stop trusting the loop because it looks busy.

1. Fourth Improver tool `propose_weight_update(target="laya-intent" | "whisper-owner-lora" | "router-distill")`.
   * `laya-intent`: at ≥25 new corrections or high-confidence `laya`≠`jev` disagreements, queue the export,
     retrain, and promote only if `bench_intent.py 3` plus sealed `held_out/intent.jsonl` strictly improve.
   * `whisper-owner-lora`: stage owner clips where `[Cleanup]` rewrote domain nouns for a one-step
     `jarvis voice-review`, train overnight with validation early-stopping (the epoch-memorization problem in
     `docs/voice-finetune-plan.md`), gate on held-out owner WER **and** synthetic general-English WER.
2. Meta-utility in SQLite and `jarvis-top`: weekly first-attempt builder pass rate, interactive friction rate
   per 100 turns, median USD and turns per solved task. Any self-upgrade touching `run_skill_improver` or
   `skills.py` sits on a 7-day probation where these may not degrade.

Gate: a rollback path exists for a promoted checkpoint, and the probation metric is visible in the cockpit.

## 4. Order and risk

| Phase | Effort | Deliverable | Rung |
| :--- | :--- | :--- | :--- |
| 1 | ~2 days | interactive/voice traces, `HARNESS_SURFACES`, nightly reflection in `ASSIST` | solid 3 |
| 2 | ~2 days | `bench_brain_route`, `bench_voice_prompts`, auto held-in/held-out | sealed Self-Harness |
| 3 | ~2–3 days | proposal history, falsifiability check, one memory | full AHE + ACE |
| 4 | ~3–4 days | executable skills, lift retirement, `bge-m3` novelty, Pareto | MCE + AlphaEvolve |
| 5 | ~4–5 days | Laya and Whisper loops, meta-utility in the cockpit | 5 (SIA) |

The one hard rule: **do not set `AUTONOMOUS_HARNESS_UPGRADES=true` before Phase 2 lands.** Today a self-upgrade
can edit `brain/orchestrator.py` and pass every gate we have, while daytime routing quietly degrades, and
nothing in the system would notice for days.

Known traps from the paper, restated for this repo: the evaluator and the tracer must stay outside the edit
surface (`PROTECTED_SELF_PATHS` + sealed held-out is exactly that — keep it); negative results must stay visible
(`progress/<task>.md` already does); and diversity collapses fast once `run_auto_research` always picks the same
shape of hypothesis, which is what the embedding novelty check is for.

## 5. Open questions

- Does the router benchmark pay for itself, or is the local `_heuristic` fallback plus a smoke test enough?
- Is a nightly timer the right trigger in `ASSIST`, or should reflection fire on the first idle 10 minutes
  after a fifth new trace? A timer is simpler and survives restarts; idle-triggering is fresher.
- Phase 5's Laya loop needs the `laya` repo's `distill.py feedback` to run unattended on this Mac. Not checked.
- Whether `bge-m3` cosine ≥ 0.88 actually separates distinct hypotheses; needs a sample of the current
  `spec_similarity` pairs before it replaces anything.
