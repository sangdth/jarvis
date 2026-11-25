# Self-Improvement Architecture and Backlog

How Jarvis improves its own harness — prompts, router, skills, memory, and local model checkpoints — without reward
hacking. Framed around Lilian Weng, *Harness Engineering for Self-Improvement*
(<https://lilianweng.github.io/posts/2026-07-04-harness/>), plus *Self-Harness*, *AHE*, *Meta-Harness*, and Lin et al.
2026 (citations in `docs/research/observability-landscape.md`).

## 1. Optimization Rungs and Current Status

| Rung | Object optimized                | Status in Jarvis                                        |
| :--- | :------------------------------ | :------------------------------------------------------ |
| 1    | Prompts, context, skills        | Built: `MEMORY.md`, `SYSTEM.md` guards, lift retirement |
| 2    | Traces, harvested eval splits   | Built: `traces.py`, `evals.py` (`held_in` / `held_out`) |
| 3    | Harness code & closed loop      | Built: `harness_proposals`, K=10 check, voice curation  |
| 4    | Multi-objective candidate search| Built: `pareto.py`, `bge-m3` novelty, `EVOLVE-BLOCK`    |
| 5    | Model weights + meta-probation  | Partial: `weights.py` & 7-day probation built; see §3   |

## 2. Non-Negotiable Guardrails

- **No new daemons or runtime dependencies.** Runs inside the single `jarvis` process using SQLite (`state.db`),
  local files (`traces/`, `evals/`, `models/`), Ollama `bge-m3`, and PEP 723 training scripts (`scripts/voice/`).
- **Evaluators, tracer, and held-out splits stay outside worker reach.** `traces.py`, `evals.py`, `board.ts`,
  `sandbox.py`, and all `scripts/bench_*.py` live in `PROTECTED_SELF_PATHS`. Benchmarks run from the checkout
  (`repo_dir`) against candidate code on `PYTHONPATH`, and `sandbox.py` denies `JARVIS_HOME/evals/held_out/` to every
  worker role (including `Improver`).
- **One Qdrant lock.** `curate_voice_memory` (`voice_memory.py`) reuses the in-process Mem0 handle attached by
  `supervisor.py` and enforces namespace isolation in code (`sang` vs `speaker:*`).
- **Human gate by default.** Harness proposals and weight updates land as `draft` tasks unless
  `AUTONOMOUS_HARNESS_UPGRADES=true`; edits to `skills.py` or `reconciler.py` enter a 7-day meta-utility probation.

## 3. Remaining Work

Items not yet built (in priority order):

1. **Whisper validation early-stopping (`scripts/voice/train.py`, `scripts/voice/evaluate.py`):** Carve a deterministic
   10% `val` split from `train` (keeping the 1-in-7 `test` split untouched), keep the best `val` epoch instead of
   running 5 fixed epochs (which memorized at `loss ~ 0` in Round 1; see `docs/research/whisper-finetune.md`), and emit
   JSON scores for automated promotion gating.
2. **Whisper clip staging and review (`voice_data/staged.jsonl`, `jarvis voice-review`):** When `SAVE_SEGMENTS` is on
   and an owner turn logs `friction_kind='asr_rewrite'`, stage `{wav, raw_stt, cleaned}` into
   `JARVIS_HOME/voice_data/staged.jsonl`; add `jarvis voice-review` (`[y]es / [e]dit / [n]o`) so only human-checked
   clips enter `voice_data/manifest.jsonl`.
3. **Checkpoint CLI (`jarvis models [status|rollback <target>]`) and `laya` runner check:** Wire `weights.status()` and
   `weights.rollback()` plus `POST /v1/restart` into `src/jarvis/cli.py`, and verify `~/Projects/laya/data/distill.py
   feedback` runs unattended on this Mac for `laya-intent` retrains.
4. **Dashboard meta-utility and probation strip (`apps/dashboard/`):** Read `system_state` probation columns and
   rolling 7-day `compute_meta_utility()` metrics read-only in `apps/dashboard/src/db.rs` and show them in the header.
5. **Executable skill sidecars (`context.py` / `check.sh`, deferred):** Optional read-only, network-denied scripts
   beside `SKILL.md`. Deferred because 10-load marginal-lift retirement already removes placebo skills and the payoff
   of sidecar prompt injection is unproven.
