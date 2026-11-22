# One command per job, whichever language it is in. `just` (https://github.com/casey/just) runs it.
set shell := ["bash", "-c"]

# The jobs below; `just` alone lists them.
default:
    @just --list

# Python and Rust dependencies, one lockfile each.
sync:
    uv sync
    cargo fetch

# Every suite: the CLI, the voice harness, the brain, and the cockpit.
test:
    uv run pytest -q
    cargo test --workspace

lint:
    uv run ruff check src apps packages tests scripts
    uv run mypy
    cargo clippy --workspace --all-targets -- -D warnings

fmt:
    uv run ruff check --fix src apps packages tests scripts
    cargo fmt --all

# The cockpit, ready for `jarvis top`.
build:
    cargo build --release --workspace

top:
    uv run jarvis top

# The brain on its own, as the 24/7 LaunchAgent starts it.
serve:
    uv run jarvis serve

status:
    uv run jarvis status

# Set up a home: .env, agents.json, SOUL.md, the brain folder and its token.
init:
    uv run jarvis init

# The speech check and the routing benchmarks (paid OpenRouter calls).
bench-intent:
    uv run python scripts/bench_intent.py

bench-routing:
    uv run python scripts/bench_routing.py
