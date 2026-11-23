#!/bin/sh
# Installs Jarvis from a checkout: curl -fsSL https://sangdth.github.io/jarvis/install.sh | sh
#
# There is no packaged release to wait for. The checkout *is* the runtime, so updating is `git pull` and
# `jarvis start`; editing a file is `jarvis start`. It installs uv (if missing), which brings its own Python,
# so nothing touches the Python you already have.
set -eu

REPO_URL="${JARVIS_REPO:-https://github.com/sangdth/jarvis}"
TARGET="${JARVIS_DIR:-$HOME/Projects/jarvis}"
BIN_DIR="$HOME/.local/bin"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "Jarvis needs a Mac with Apple Silicon (M1 or newer)." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (it brings the Python Jarvis runs on)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# uv's installer puts it here; this shell's PATH does not have it yet.
PATH="$BIN_DIR:$PATH"

if [ -d "$TARGET/.git" ]; then
  echo "Updating the checkout in $TARGET..."
  git -C "$TARGET" pull --ff-only
else
  echo "Cloning Jarvis into $TARGET..."
  mkdir -p "$(dirname "$TARGET")"
  git clone "$REPO_URL" "$TARGET"
fi

echo "Installing its Python dependencies (the first run downloads a few GB and takes a few minutes)..."
(cd "$TARGET" && uv sync)

# One small shim on PATH: it runs this checkout's own `.venv`, so the code on disk is the code that runs.
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/jarvis" <<EOF
#!/bin/sh
# Jarvis lives in $TARGET. This shim runs that checkout; it is not a packaged install.
exec "$TARGET/.venv/bin/jarvis" "\$@"
EOF
chmod +x "$BIN_DIR/jarvis"
"$BIN_DIR/jarvis" --version || true

# The cockpit (`jarvis top`) is the one Rust build. Build it when a toolchain is there; never block setup on it.
if command -v cargo >/dev/null 2>&1; then
  echo "Building the cockpit (jarvis top)..."
  (cd "$TARGET" && cargo build --release --workspace >/dev/null 2>&1) ||
    echo "The cockpit did not build; \`jarvis top\` will print what to run."
else
  echo "No Rust toolchain, so the cockpit is skipped: \`cargo build --release --workspace\` in $TARGET later."
fi

# The mac client app, served at /Jarvis.app.zip so another Mac can install it with one command.
if command -v swiftc >/dev/null 2>&1; then
  echo "Building the mac client (Jarvis.app)..."
  (cd "$TARGET" && sh apps/mac/build.sh >/dev/null 2>&1) ||
    echo "The mac client did not build; \`jarvis start\` tries again."
else
  echo "No swiftc, so the mac client is skipped (xcode-select --install, then \`just mac\`)."
fi

echo
echo "Jarvis is installed in $TARGET. Setting it up now (open a new terminal later to use the jarvis command)."
# stdin is this script when piped from curl, so the questions read from the terminal.
if [ -r /dev/tty ]; then
  "$BIN_DIR/jarvis" init </dev/tty
else
  echo "Run: jarvis init"
fi
