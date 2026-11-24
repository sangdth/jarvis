# Observability for a Self-Improving Harness: Research & Landscape Audit (2026-11-24)

Companion to `docs/self-improvement.md` and `docs/architecture.md` (§3E). Evaluates what observability a
self-improving local harness actually needs and audits the 2026 open-source LLM observability landscape against
Jarvis's footprint and license constraints.

---

## 1. Two Consumers, Not One: Human Monitoring vs. Offline Agent Evolution

Most LLM observability platforms are built for **human engineers clicking charts in a browser**. A
self-improving harness has a second, stricter consumer: **an offline coding agent (`pi --offline`) diagnosing
past rollouts inside a filesystem sandbox**.

Five empirical findings from the 2026 harness-evolution papers set our requirements:

1. **Raw filesystem drill-down beats summaries alone by +15.1 pp (*Meta-Harness*, `arXiv:2603.28052v1`
   Table 3).** Under identical proposer budgets, giving the optimizer *scores + LLM summaries* reached **34.9%**
   median accuracy (`38.7%` best), whereas exposing **raw execution traces on disk** via `grep`/`cat` reached
   **50.0%** median (`56.7%` best). The proposer read a median of **82 files across 20+ prior runs** per
   iteration (Appendix A.1) to isolate confounded edits (e.g., separating a good structural fix from a harmful
   prompt edit).
2. **Progressive disclosure keeps token cost bounded (*AHE*, `arXiv:2604.25850v5` §3.2 & `evolve_prompt.md`).**
   Raw rollouts routinely exceed 10M tokens. AHE solves this with a three-tier file hierarchy:
   `analysis/overview.md` (cross-task root-cause summary, read first) → `analysis/detail/<task>.md` (per-case
   diagnosis) → `cleaned.json` (normalized step/tool trace, read on demand). Notably, AHE's own public
   codebase (`china-qijizhifeng/agentic-harness-engineering`, `trace_converter.py`) uses an in-process tracer
   dumped to normalized local JSON files as its source of truth; Langfuse is only an optional passive mirror.
3. **LLM self-attribution is blind to regressions (*AHE* §4.4.2 RQ3b).** Across 9 evolution rounds, GPT-5.4
   predicted which tasks an edit would *fix* at ~5× random (`33.7%` precision, `51.4%` recall), but its
   **regression predictions were nearly random (`11.8%` precision, `11.1%` recall)**. Furthermore, AHE's
   component ablation (Table 3) showed that `system_prompt`-only evolution regressed (`69.7% → 67.4%`) while
   `memory` (`75.3%`), `tools` (`73.0%`), and `middleware` (`71.9%`) carried the transferable gains and cut
   token spend (`Succ/Mtok`, Table 5) by 12–32%. Therefore, observability **must stamp component/commit hashes**
   (`git_sha`, `prompt_sha`, `skills_used`) and token/latency cost on every run so regressions are caught
   statistically across commits rather than by asking the model what might break.
4. **Causal signatures beat exit codes (*Self-Harness*, `arXiv:2606.09498v3` §3.2).** Clustering failures by
   surface outcomes (`timed_out`, `failed_verify`) conflates unrelated bugs. Self-Harness clusters failed traces
   deterministically by a 3-tuple signature $\phi(r) = (c_i, q_i, m_i)$: terminal verifier cause $c_i$,
   causal agent behavior $q_i$, and abstract harness mechanism $m_i$.
5. **`pi --offline` has no network (`apps/brain/src/brain/reconciler.py`).** Jarvis runs the
   `Improver` with `--offline` inside macOS `sandbox-exec` (`apps/brain/src/brain/sandbox.py`). Any backend
   whose primary read interface is HTTP/GraphQL/SQL-over-TCP (ClickHouse, Postgres, Langfuse, Phoenix server)
   cannot be queried by the Improver without punching network holes in Seatbelt.

---

## 2. Open-Source Landscape Audit (2026)

Constraints for Jarvis:
- **License:** Whole repository is **`AGPL-3.0-only`** (`pyproject.toml`, `apps/brain/AGENTS.md`). Embedded libraries
  must be GPLv3/AGPLv3-compatible (MIT, BSD, Apache-2.0); non-OSI source-available licenses (ELv2, SSPL) cannot be
  bundled.
- **Deployment & Footprint:** Single `uv sync` + `cargo build` checkout (`docs/DECISIONS.md`), no Docker daemon
  required, sharing RAM/GPU on a Mac with MLX Whisper, `sherpa-onnx`, Ollama (`bge-m3`), T3 Code, and
  `jarvis-dashboard`.

| Solution            | License       | Footprint              | Offline files | Verdict                      |
| :------------------ | :------------ | :--------------------- | :------------ | :--------------------------- |
| **Langfuse v4**     | MIT (+ EE)    | 6 Docker, 2.5–4.5 GB   | No (HTTP)     | Rejected (OLAP + Docker)     |
| **Comet Opik**      | Apache-2.0    | 8+ Docker, 4–6 GB      | No (HTTP)     | Rejected (heaviest stack)    |
| **OpenLIT**         | Apache-2.0    | 3 Docker, 1.2–2.5 GB   | No (HTTP)     | Rejected (ClickHouse/Docker) |
| **Arize Phoenix**   | ELv2 (server) | 45+ pkgs, 250–450 MB   | No (GraphQL)  | Optional external viewer     |
| **MLflow Tracing**  | Apache-2.0    | 20+ pkgs, 300–500 MB   | Partial       | Adopt OTel `gen_ai.*` only   |
| **VictoriaTraces**  | Apache-2.0    | 1 Go bin, 30–60 MB     | HTTP/CLI      | Optional external sidecar    |
| **Native (chosen)** | AGPL-3.0-only | 0 daemons, `< 5 MB`    | 100% native   | Selected (`traces.py`)       |

Notes:
- **Arize Phoenix:** `phoenix-otel` / `openinference` are Apache-2.0, but the server is Elastic-2.0 (non-OSI) and
  pulls ~400 MB into `.venv`. Works on demand as an external viewer (`uvx arize-phoenix serve`) via
  `OTEL_EXPORTER_OTLP_ENDPOINT`.
- **VictoriaTraces / VictoriaLogs:** Best optional external sidecar if multi-machine aggregation is ever needed;
  unnecessary extra daemon for a single Mac today.
- **Pipecat `TurnTraceObserver` gotcha:** `pipecat-ai` ships `TurnTraceObserver`, which hooks Pipecat's
  `TurnTrackingObserver` and conflicts with `JarvisIntentGate`'s `ExternalUserTurnStrategies` (pre-LLM interruptions
  wipe the turn cycle; see `apps/voice/src/jarvis_voice/latency.py`). Routing spans through Jarvis's own
  `TelemetryHub` (`apps/brain/src/brain/traces.py`) avoids that bug completely.

---

## References

1. Lin et al., *Agentic Harness Engineering: Observability-Driven Automatic Evolution of Coding-Agent Harnesses*,
   `arXiv:2604.25850v5` (Nov 2026) & `https://github.com/china-qijizhifeng/agentic-harness-engineering`.
2. Zhang et al., *Self-Harness: Harnesses That Improve Themselves*, `arXiv:2606.09498v3` (Aug 2026).
3. Lee et al., *Meta-Harness: End-to-End Optimization of Model Harnesses*, `arXiv:2603.28052v1` (Mar 2026).
4. Weng, Lilian, *Harness Engineering for Self-Improvement*, `https://lilianweng.github.io/posts/2026-07-04-harness/` (Jul 2026).
5. OpenTelemetry Generative AI Agent Semantic Conventions, `https://github.com/open-telemetry/semantic-conventions-genai`.
6. VictoriaTraces & VictoriaLogs Architecture, `https://docs.victoriametrics.com/victoriatraces/`.
