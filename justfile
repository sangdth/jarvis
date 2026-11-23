# One command per job, whichever language it is in. `just` (https://github.com/casey/just) runs it.
set shell := ["bash", "-c"]

# The jobs below; `just` alone lists them.
default:
    @just --list

# Python and Rust dependencies, one lockfile each. The only "build" there is: the checkout is the runtime.
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

# The cockpit and the Connect app (the one compiled artifact per language; Python is live as written).
build:
    cargo build --release --workspace
    sh apps/connect/build.sh

# The Connect app alone: Jarvis.app, served at /Jarvis.app.zip.
mac:
    sh apps/connect/build.sh

# Listen in this terminal with this Mac's mic and speaker.
run:
    uv run jarvis

# The 24/7 service on this checkout: sync, build the cockpit and the mac app, relaunch, open the cockpit.
start:
    uv run jarvis start

status:
    uv run jarvis status

top:
    uv run jarvis top

# Set up the state home: .env, agents.json, SOUL.md, the brain folder and its token.
init:
    uv run jarvis init

# The speech check and the routing benchmarks (paid OpenRouter calls).
bench-intent:
    uv run python scripts/bench_intent.py

bench-routing:
    uv run python scripts/bench_routing.py
