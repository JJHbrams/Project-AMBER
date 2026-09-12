# AMBER native bubbles

Host-owned comic bubbles, separate from the dashboard and public renderer API.
Input and its sent/waiting queue are two faces of one window. Speech and thought
are separate transparent windows. Character art and event mappings are untouched.

## Build and isolated QA

Requires the existing Engram Python environment, Rust/MSVC and WebView2. Dependencies
are pinned by `Cargo.lock`; the build uses the local Cargo cache (`cargo fetch --locked`
is needed once on a fresh development machine).

```powershell
./installer/build-native-bubble.ps1 -DebugBuild
python scripts/dev/smoke_native_bubble.py --exercise
python scripts/dev/smoke_native_bubble.py --demo
```

The QA harness uses **synthetic messages and a fake provider**, never the user's
conversation/DB. `--exercise` checks the actual WebViews, private host queue,
terminal-gated priority send and owned-child shutdown. It needs `websockets` in the
existing Python environment. Windows sandbox restrictions can prevent WebView2
startup; a successful Rust build alone does not prove a working UI.

`installer/build-overlay.ps1` builds the release shell and includes it in the frozen
package. The frozen entry offers the same isolated test as
`engram-overlay.exe --role bubble-smoke --exercise`. This is not a release-publish
or installed-overlay restart command.

## Private boundary

The host owns providers, approvals, request IDs, queue state and version answers.
Only an inherited stdio channel carries message content. A version/nonce handshake
and all three window-ready signals precede actions. Public external renderer events
remain metadata-only. Protocol stdout is not a diagnostic log.

Full acceptance and outstanding live-provider, IME, DPI and installer checks:
[approved design](../docs/design/0004-bubble-native-renderer.md).
