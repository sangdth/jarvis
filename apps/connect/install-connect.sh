#!/bin/sh
# Installs the Connect app from the rolling GitHub release, and clears the quarantine flag so Gatekeeper does not
# block the first launch (the app is ad-hoc signed, not notarized). The repo is private, so the download goes through
# `gh` when it is signed in, and falls back to the plain release URL.
#   gh release download connect --repo sangdth/jarvis --pattern 'install-connect.sh' --dir /tmp --clobber
#   sh /tmp/install-connect.sh http://mac-mini:8643      # the address is optional: Settings takes it too
set -eu

DMG_URL=${JARVIS_CONNECT_DMG:-https://github.com/sangdth/jarvis/releases/download/connect/Jarvis.Connect.dmg}
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
# The repo is private today, so the plain release URL answers 404 without a token: `gh`, when signed in, does.
if [ -z "${JARVIS_CONNECT_DMG:-}" ] && command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  gh release download connect --repo sangdth/jarvis --pattern "$(basename "$DMG_URL")" --dir "$TMP" --clobber
  mv "$TMP/$(basename "$DMG_URL")" "$TMP/app.dmg"
else
  curl -fsSL "$DMG_URL" -o "$TMP/app.dmg"
fi
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
