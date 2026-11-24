# The mac client

`Jarvis.app` is the Mac in front of you: its mic and speaker, one Connect button, and Settings. Every model, agent,
key and memory stays on the Mac running Jarvis; this app is a mic tap, a resampler, a player and a socket.

```bash
curl -fsSL 'http://<mac-mini>:8643/install-app' | sh
```

That installs `/Applications/Jarvis.app` (or `~/Applications` when `/Applications` is not writable), clears the
download's quarantine flag, writes only the server URL to `~/.config/jarvis-remote/server`, and opens it. A raw
[`/Jarvis.app.zip`](http://localhost:8643/Jarvis.app.zip) download works too; put the URL in Settings by hand.

Without a Jarvis to ask, the newest build is on the rolling [releases page](https://github.com/sangdth/jarvis/releases/tag/connect).
The repo is private, so that page needs a signed-in browser; from a terminal:

```bash
# download only
gh release download connect --repo sangdth/jarvis --pattern '*.dmg' --dir ~/Downloads

# or download, install, and clear the quarantine flag so Gatekeeper does not block the first launch
gh release download connect --repo sangdth/jarvis --pattern 'install-connect.sh' --dir /tmp --clobber
sh /tmp/install-connect.sh http://mac-mini:8643     # the address is optional; Settings takes it too
```

By hand: open `Jarvis.Connect.dmg`, drag `Jarvis.app` to Applications, then allow it once in Privacy & Security
(ad-hoc signature, not notarized).

## What it does

- **Connect.** With no secret yet, the button asks the server for a pairing code
  (`POST /v1/pair/request`, which is open and answers without the code). `jarvis dashboard` shows the digits and keeps them
  on screen until they are used or expire; `jarvis pair` prints them in the terminal instead. Type them in
  (`POST /v1/pair`) and the app keeps its own secret in `~/.config/jarvis-remote/credentials.json` (0600), the same
  file the Python client uses. After that the button just connects, and Disconnect hands the mic back.
- **Talk.** One `AVAudioEngine` with macOS voice processing carries both the tap and the player, so the echo
  canceller sits where the speaker is: barge-in and "shut up" work. Mic audio is converted to 16-bit mono at 16 kHz
  with `AVAudioConverter` (no `soxr`) and sent over `GET /ws`; Jarvis's speech comes back down the same socket.
- **Say what is wrong.** `1013` means another Mac holds the ear; `1008` means this Mac was revoked and needs a new
  code; three seconds with no mic frames means macOS never granted Microphone, and the app says where to grant it.
- **Settings.** The app's version (baked in at build time from `git_version()`) beside the server's (`GET /health`),
  an Update button, the server address (LAN name or Tailscale), and Unpair.

## Update

The gateway serves the bundle built on its own checkout. **Update** downloads `/Jarvis.app.zip`, checks its signature
with `codesign --verify --deep --strict`, swaps the bundle, and relaunches. The server rebuilds it on
`jarvis start` and `just build` rebuild it, so the app is never older than the code running beside it.

Limits: ad-hoc signing (`codesign -s -`) changes the bundle's hash on every build, so macOS may ask for the
Microphone again after an update; an Apple Development identity in the keychain avoids that. In-place updates assume
the bundle is writable — otherwise install again with `/install-app`. An app downloaded from the release (not
installed through `/install-app`, which clears the quarantine flag) is blocked on first launch, because ad-hoc
signatures are not notarized: allow it once in System Settings → Privacy & Security → Open Anyway, or run
`xattr -dr com.apple.quarantine /Applications/Jarvis.app`.

## Build

```bash
sh apps/connect/build.sh      # or: just mac
```

Writes `target/connect/Jarvis.app` and `target/connect/Jarvis.app.zip`, signed ad-hoc (or with an Apple Development
identity when one is in the keychain). `.github/workflows/connect.yml` does the same on every push to master and
keeps the zip as the `connect` workflow artifact. Needs only the Command Line Tools (`xcode-select --install`):
the sources are `src/JarvisApp.swift` (the window), `src/AppModel.swift` (state and the update),
`src/ApiClient.swift` (health, pairing, credentials) and `src/AudioRelay.swift` (the socket and the audio).
