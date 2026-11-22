#!/bin/sh
# Installs the `jarvis` command: curl -fsSL https://sangdth.github.io/jarvis/install.sh | sh
# It installs uv (if missing), which brings its own Python, so nothing touches the Python you already have.
set -eu

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "Jarvis needs a Mac with Apple Silicon (M1 or newer)." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (the tool that installs Jarvis)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# uv's installer puts it here; this shell's PATH does not have it yet.
PATH="$HOME/.local/bin:$PATH"

echo "Installing Jarvis. The first install downloads a few GB and takes a few minutes..."
uv tool install --force --python 3.12 "git+https://github.com/sangdth/jarvis@master"
uv tool update-shell >/dev/null 2>&1 || true

# The cockpit (`jarvis top`) is Rust. Build it when a toolchain is there; never block the voice setup on it.
if command -v cargo >/dev/null 2>&1; then
  echo "Building the cockpit (jarvis top)..."
  cargo install --git https://github.com/sangdth/jarvis --locked --bin jarvis-top --force >/dev/null 2>&1 ||
    echo "The cockpit did not build; \`jarvis top\` will print what to run."
else
  echo "No Rust toolchain, so the cockpit is skipped: \`jarvis top\` needs \`cargo install --git https://github.com/sangdth/jarvis --bin jarvis-top\`."
fi

echo
echo "Jarvis is installed. Setting it up now (open a new terminal later to use the jarvis command)."
# stdin is this script when piped from curl, so the questions read from the terminal.
if [ -r /dev/tty ]; then
  "$(uv tool dir --bin)/jarvis" init </dev/tty
else
  echo "Run: jarvis init"
fi
