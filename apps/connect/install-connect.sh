#!/bin/sh
# Installs the Connect app from the rolling GitHub release, and clears the quarantine flag so Gatekeeper does not
# block the first launch (the app is ad-hoc signed, not notarized).
#   curl -fsSL https://github.com/sangdth/jarvis/releases/download/connect/install-connect.sh | sh
#   sh install-connect.sh http://mac-mini:8643      # also points the app at that server
set -eu

DMG_URL=${JARVIS_CONNECT_DMG:-https://github.com/sangdth/jarvis/releases/download/connect/Jarvis%20Connect.dmg}
APP_NAME="Jarvis.app"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "The Jarvis Connect app runs on a Mac." >&2
  exit 1
fi

TARGET="$HOME/Applications"
if [ -d /Applications ] && [ -w /Applications ]; then TARGET=/Applications; fi

TMP=$(mktemp -d)
MNT="$TMP/mnt"
mkdir -p "$MNT"
cleanup() { hdiutil detach "$MNT" -quiet 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

echo "Downloading $APP_NAME..."
curl -fsSL "$DMG_URL" -o "$TMP/app.dmg"
hdiutil attach "$TMP/app.dmg" -nobrowse -quiet -mountpoint "$MNT"
codesign --verify --deep --strict "$MNT/$APP_NAME"

mkdir -p "$TARGET"
rm -rf "$TARGET/$APP_NAME"
ditto "$MNT/$APP_NAME" "$TARGET/$APP_NAME"
xattr -dr com.apple.quarantine "$TARGET/$APP_NAME"

if [ $# -ge 1 ]; then
  mkdir -p "$HOME/.config/jarvis-remote"
  chmod 700 "$HOME/.config/jarvis-remote"
  printf '%s\n' "$1" > "$HOME/.config/jarvis-remote/server"
  chmod 600 "$HOME/.config/jarvis-remote/server"
fi

open "$TARGET/$APP_NAME"
echo "Installed $TARGET/$APP_NAME."
