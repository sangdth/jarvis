#!/bin/sh
# Builds the Connect app (`Jarvis.app`) and the zip the gateway serves at `/Jarvis.app.zip`.
# Needs only the Command Line Tools (`xcode-select --install`): no SwiftUI macros, no package manager, no dependencies.
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=${JARVIS_CONNECT_OUT:-"$ROOT/target/connect"}
APP="$OUT/Jarvis.app"
ZIP="$OUT/Jarvis.app.zip"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "swiftc is not on PATH: run 'xcode-select --install' first." >&2
  exit 1
fi

BRANCH=$(cd "$ROOT" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo master)
COMMIT=$(cd "$ROOT" && git rev-parse --short=7 HEAD 2>/dev/null || echo dev)
DIRTY=""
if [ -n "$(cd "$ROOT" && git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  DIRTY="+dirty"
fi
JARVIS_VERSION="$BRANCH @ $COMMIT$DIRTY"

# Stage first so a failed compile leaves the previous bundle in place for `/Jarvis.app.zip`.
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/jarvis-connect.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
STAGED_APP="$STAGE/Jarvis.app"
STAGED_ZIP="$STAGE/Jarvis.app.zip"
mkdir -p "$STAGED_APP/Contents/MacOS" "$STAGED_APP/Contents/Resources"

swiftc -O -swift-version 5 -target arm64-apple-macos13.0 \
  -o "$STAGED_APP/Contents/MacOS/Jarvis" "$ROOT"/apps/connect/src/*.swift

cat > "$STAGED_APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleExecutable</key><string>Jarvis</string>
  <key>CFBundleIdentifier</key><string>com.sangdth.jarvis.remote</string>
  <key>CFBundleName</key><string>Jarvis</string>
  <key>CFBundleDisplayName</key><string>Jarvis</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>JarvisVersion</key><string>$JARVIS_VERSION</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>Jarvis uses this Mac's microphone to hear you, and cancels the echo of its own voice.</string>
  <key>NSAppTransportSecurity</key>
  <dict>
    <!-- A Jarvis on the local network or Tailscale answers over plain HTTP; the token is the guard. -->
    <key>NSAllowsArbitraryLoads</key><true/>
  </dict>
</dict>
</plist>
PLIST

# Apple Silicon needs a signature, even an ad-hoc one. With an Apple Development identity in the keychain, macOS
# keeps the Microphone grant across this Mac's updates; ad-hoc is fine too, it just asks again.
IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null |
  sed -n 's/.*"\(Apple Development: [^"]*\)".*/\1/p' | head -n 1)
if [ -z "$IDENTITY" ]; then
  IDENTITY="-"
fi
codesign --force --deep --sign "$IDENTITY" --identifier com.sangdth.jarvis.remote "$STAGED_APP" >/dev/null

ditto -c -k --keepParent "$STAGED_APP" "$STAGED_ZIP"

mkdir -p "$OUT"
rm -rf "$APP" "$ZIP"
mv "$STAGED_APP" "$APP"
mv "$STAGED_ZIP" "$ZIP"

echo "Built $APP ($JARVIS_VERSION, signed with: $IDENTITY)"
echo "      $ZIP"
