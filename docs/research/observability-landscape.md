# Observability for a Self-Improving Harness: Research & Landscape Audit (2026-11-24)

Companion to `docs/self-improvement-plan.md` (Phase 1) and `docs/observability-plan.md`. Evaluates what
observability a self-improving local harness actually needs, audits the 2026 open-source LLM observability
landscape against Jarvis's footprint and license constraints, and inspects the live telemetry wiring in this
checkout (`6a77d89`).

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
5. **`pi --offline` has no network (`apps/brain/src/brain/reconciler.py:1543-1559`).** Jarvis runs the
   `Improver` with `--offline` inside macOS `sandbox-exec` (`apps/brain/src/brain/sandbox.py`). Any backend
   whose primary read interface is HTTP/GraphQL/SQL-over-TCP (ClickHouse, Postgres, Langfuse, Phoenix server)
   cannot be queried by the Improver without punching network holes in Seatbelt.

---

## 2. Open-Source Landscape Audit (2026)

Constraints for Jarvis:
- **License:** Whole repository is **`AGPL-3.0-only`** (`pyproject.toml:6`, `apps/brain/AGENTS.md:32`). Embedded
  libraries must be GPLv3/AGPLv3-compatible (MIT, BSD, Apache-2.0); non-OSI source-available licenses (ELv2,
  SSPL) cannot be bundled.
- **Deployment & Footprint:** Single `uv sync` + `cargo build` checkout (`docs/DECISIONS.md:90`), no Docker
  daemon required, sharing RAM/GPU on a Mac with MLX Whisper, `sherpa-onnx`, Ollama (`bge-m3`), T3 Code, and
  `jarvis-top`.

| Solution | License | Required Stack (Verified from Manifests) | Local RAM & `.venv`/Disk Cost | `pi --offline` File Access? | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Langfuse v4** (`langfuse/langfuse`) | MIT core (+ commercial EE) | Docker Compose: **6 containers** (`Postgres`, `ClickHouse`, `Redis/Valkey`, `MinIO/S3`, `Web`, `Worker`) | **2.5–4.5 GB RAM**, multi-GB images | No (HTTP API) | **Rejected** — heavy OLAP server stack; requires Docker. |
| **Comet Opik** (`comet-ml/opik`) | Apache-2.0 | Docker Compose: **8+ containers** (`MySQL 8.4`, `ClickHouse`, `ZooKeeper`, `Redis`, `MinIO`, Java Dropwizard + Python backends, `Nginx`) | **4.0–6.0 GB RAM** (JVM + ClickHouse + MySQL) | No (HTTP API) | **Rejected** — heaviest local footprint in the field. |
| **OpenLIT** (`openlit/openlit`) | Apache-2.0 | Docker Compose: **3 containers** (`ClickHouse 24.4`, `NATS JetStream`, Node UI/OTLP receiver) | **1.2–2.5 GB RAM** | No (ClickHouse HTTP) | **Rejected** — ClickHouse + Docker breaks single-checkout runtime. |
| **Arize Phoenix** (`arize-phoenix`) | **Server: Elastic-2.0 (ELv2, non-OSI)**; `phoenix-otel` / `openinference`: Apache-2.0 | 1 Python process + SQLite (`~/.phoenix`), *but* `pyproject.toml` pulls **45+ packages** (`pandas`, `pyarrow`, `fastapi`, `uvicorn`, `grpcio`, `sqlalchemy`, `strawberry-graphql`, `pydantic-ai`, `anthropic`) | **250–450 MB RAM**, ~400 MB `.venv` | No (GraphQL / internal ORM schema) | **Rejected as core dep** (ELv2 license clash + `.venv` bloat). **Supported as optional external viewer** via `uvx arize-phoenix serve`. |
| **MLflow Tracing** (`mlflow-tracing` + `mlflow`) | Apache-2.0 | `mlflow-tracing` SDK is ~5 MB, *but* UI/search server requires full `mlflow` (~1 GB, 20+ deps) + separate daemon | SDK: ~15 MB; Server: **300–500 MB RAM** | Partial (not `grep`-friendly) | **Rejected as server; adopt its wire standard** (OpenTelemetry `gen_ai.*` conventions). |
| **VictoriaTraces / VictoriaLogs** (`VictoriaMetrics`) | Apache-2.0 | **1 zero-config Go binary** (~25 MB), daily `YYYYMMDD` disk partitions, native OTLP ingest, Jaeger/LogsQL UI | **30–60 MB RAM**, ~10× compression, auto-retention | HTTP/CLI (`LogsQL`), not plain files | **Best optional external sidecar** if multi-machine trace aggregation is ever needed; unnecessary extra daemon today. |
| **Native OTel-JSONL + SQLite WAL/FTS5 + `jarvis-top`** *(Selected)* | **`AGPL-3.0-only`** + Python stdlib `sqlite3` + Rust `rusqlite` | **0 extra daemons, 0 Docker, 0 new ports, 0 required new deps** | **< 5 MB RAM**, ~3–8 MB/week disk, **0 ms** hot-path block | **100% Native** (`.md` + `.jsonl` + read-only `state.db`) | **Selected** — matches AHE + Meta-Harness + Jarvis's architecture, with optional OTLP/HTTP export. |

---

## 3. Audit of What Jarvis Already Ships (and Three Live Wiring Gaps)

Inspecting the running checkout and `.venv` (`Python 3.12.16`, `Pipecat 1.15.0`, `SQLite 3.53.1`, `rusqlite 0.43`)
shows that **80% of the collection machinery and UI already exist**:

1. **Installed primitives at zero new dependency cost:**
   - Python's stdlib `sqlite3` is **SQLite 3.53.1** compiled with **`ENABLE_FTS5`** and JSON functions.
   - `apps/tui` (`Cargo.toml:14`) already bundles `rusqlite 0.43` opening `state.db` with `SQLITE_OPEN_READ_ONLY`
     (`apps/tui/src/db.rs:16-23`), plus `apps/tui/src/ui/xray.rs` rendering a live stage-by-stage Turn X-Ray in
     `<10 MB` RAM.
   - `pipecat-ai==1.15.0` already installs `opentelemetry-api` and `pipecat.utils.tracing.TurnTraceObserver`.
     However, `TurnTraceObserver` hooks Pipecat's standard `TurnTrackingObserver`, which conflicts with
     `JarvisIntentGate`'s `ExternalUserTurnStrategies` for the exact reason documented in
     `apps/voice/src/jarvis_voice/latency.py:13-25` (pre-LLM interruptions wipe the turn cycle). Routing spans
     through Jarvis's own `TelemetryHub` avoids that bug completely.

2. **Three concrete telemetry wiring gaps found in this checkout:**
   - **Gap A — Ephemeral RAM ring buffer only:** `TelemetryHub` (`apps/brain/src/brain/telemetry.py:74`) is an
     in-memory `deque(maxlen=2000)`. Every `jarvis start` or self-upgrade wipes all history, and `jarvis-top`
     opens to `"say something: each stage lands here live"` (`apps/tui/src/ui/xray.rs:75`) even right after a
     failed voice turn.
   - **Gap B — Broken `turn_id` link between Voice and `brain.route`:** `apps/tui/src/app.rs:852-873` expects
     `brain.route` events to carry `turn_id` so the `ROUTE` stage renders inside the Turn X-Ray (`apps/tui/src/main.rs:323`
     mocks `"turn_id": "t42"`). In live code, `BrainClientAdapter.send_task` (`apps/voice/src/jarvis_voice/brain_client.py:204-218`)
     never sends `turn_id` in `POST /v1/runs`, so `BrainOrchestrator._telemetry` (`apps/brain/src/brain/orchestrator.py:206-234`)
     emits `brain.route` without `turn_id` and the stage is orphaned from the turn.
   - **Gap C — Unforwarded `replace=True` friction signal:** `server.py:784` checks `if bool(body.get("replace")):`
     to cancel and supersede a running task, but `agent_tools.py:244-245` calls `await agent.interrupt()` locally
     and never forwards `replace=True` through `BrainClientAdapter.send_task`, losing a clean server-side
     record that the user had to correct a misunderstood task mid-flight.

---

## 4. Recommended Architecture Summary

Build a **zero-daemon, 4-layer embedded observability pipeline** inside `apps/brain` + `apps/voice` + `apps/tui`,
using **OpenTelemetry GenAI semantic conventions (`gen_ai.*`)** as the field vocabulary:

1. **Layer 0 — End-to-End `trace_id` & Safe Async Sink (`0 ms` audio impact):** Stamp both `turn_id` (for live
   `jarvis-top` X-Ray compatibility) and a globally unique 16-byte hex `trace_id` across Voice → HTTP
   `POST /v1/runs` (`turn_id`, `trace_id`, `replace`) → `BrainOrchestrator` → Runners. Drain `TelemetryHub` on
   an async background queue that strips secrets, drops `voice.level` / `heartbeat`, and **redacts
   `pair.requested` 6-digit pairing codes** before anything touches disk.
2. **Layer 1 — Daily Partitioned JSONL (`JARVIS_HOME/brain/traces/YYYY-MM-DD/`):** Borrow VictoriaTraces's
   immutable per-day folders (`spans.jsonl` in OTel `gen_ai.*` shape + one `trace-<trace_id>.json` per finished
   turn/run). Pruning old history (`30d` or `500 MB`) is an instant `shutil.rmtree` on old date directories.
3. **Layer 2 — SQLite `trace_runs` + `trace_fts` (FTS5) + 8-Signal Implicit Friction Detector (`state.db`):**
   ~350 bytes/row indexing every Voice turn, interactive `T3`/`pi` run, timer check, and board attempt, stamped
   with `git_sha` and `prompt_sha`, plus online behavioral friction detection (`replaced`, `stopped`,
   `router_override`, `not_you`, `repeated`, `asr_rewrite`, `latency_sla`, `verify_failed`).
4. **Layer 3 — Progressive-Disclosure Digest (`traces/analysis/`) + `jarvis traces` CLI:** Deterministic +
   causal $\phi(r)$ aggregation into `traces/analysis/overview.md` and `surfaces/<surface>.md` for `pi --offline`
   `Improver`, paired with a `jarvis traces` CLI (*Meta-Harness* Appendix D) and `jarvis-top` cold-start
   X-Ray hydration.
5. **Optional Escape Hatch (`OTEL_EXPORTER_OTLP_ENDPOINT`):** When set in `.env`, asynchronously POST the same
   OTel `gen_ai.*` spans over `httpx` (already installed) to an on-demand `victoria-traces` binary or
   `uvx arize-phoenix serve`—zero permanent dependencies or RAM when unset.

---

## References

1. Lin et al., *Agentic Harness Engineering: Observability-Driven Automatic Evolution of Coding-Agent Harnesses*,
   `arXiv:2604.25850v5` (Nov 2026) & `https://github.com/china-qijizhifeng/agentic-harness-engineering`.
2. Zhang et al., *Self-Harness: Harnesses That Improve Themselves*, `arXiv:2606.09498v3` (Aug 2026).
3. Lee et al., *Meta-Harness: End-to-End Optimization of Model Harnesses*, `arXiv:2603.28052v1` (Mar 2026).
4. Weng, Lilian, *Harness Engineering for Self-Improvement*, `https://lilianweng.github.io/posts/2026-07-04-harness/` (Jul 2026).
5. OpenTelemetry Generative AI Agent Semantic Conventions, `https://github.com/open-telemetry/semantic-conventions-genai`.
6. VictoriaTraces & VictoriaLogs Architecture, `https://docs.victoriametrics.com/victoriatraces/`.
