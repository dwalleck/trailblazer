# wayfinder-acp carve map — measured, not speculative

Status: reference, 2026-08-09. Read from cyril-core at `~/repos/cyril` (main).
Purpose: answer the "what does wayfinder-acp expose / how does the carve work"
question with facts, so design interviews don't speculate about code. This was
the predicted grill-stall topic — treat everything here as settled input.

## Headline: don't carve for milestone 1

cyril-core is already a UI-free library crate (zero ratatui/crossterm imports
across the protocol layer, ~17.4k lines). The **entire surface the cyril
binary uses to drive a KAS agent is five calls**:

```rust
// crates/cyril-core/src/protocol/bridge.rs
let bridge: BridgeHandle = cyril_core::protocol::bridge::spawn_bridge(
    AgentCommand::try_from_argv(vec!["kiro-cli".into(), "acp".into()])?,
    SpawnConfig { engine: AgentEngine::Kas, ..Default::default() },
    cwd,
)?;

bridge.sender().send(BridgeCommand::SendPrompt { session_id, content_blocks }).await?;
let note: Option<RoutedNotification> = bridge.recv_notification().await;  // session-id routed
let perm: Option<PermissionRequest>  = bridge.recv_permission().await;    // approval flow
// BridgeHandle::split() exists for select! loops; BridgeHandle::for_tests() for unit tests.
```

The bridge runs the `!Send` ACP machinery on its own thread; the app side is
plain `Send` channels. Fail-stop paths emit `Notification::BridgeDisconnected`
— no silent channel closes. `SpawnConfig` already carries the KAS knobs
wayfinder needs (`engine`, `kas_spawn`, `shell`, `present_as` = the
`clientInfo` identity, `kas_hooks`).

**Milestone-1 decision: depend on `cyril-core` as a path/git dependency and
wrap `BridgeHandle` thinly.** Extract a real `wayfinder-acp` crate only after
the tracer bullet shows which surface is actually used — carving before usage
data just guesses the seam. (This retires design.md risk 1's residual by
deferring it to an informed refactor.)

## Session-targeting: already multi-session shaped

- `BridgeCommand::SendPrompt { session_id, .. }` — per-session.
- Every notification arrives as `RoutedNotification { session_id, .. }` —
  the app demuxes by id (cyril's main-vs-subagent split lives in cyril's
  binary, not the library).
- Parallel turns across sessions on one connection: live-proven
  (`docs/spike-parallel-turns.md`).

## The known deltas (the actual milestone-1 work in cyril-core)

1. **`BridgeCommand::NewSession { cwd }` carries no `mcpServers` and no
   `_meta`.** Wayfinder must declare its MCP server (and, later, custom
   agents/`modeId`) at `session/new`. This is the one required cyril-core
   change: extend `NewSession` (and `LoadSession` — reloads must re-declare
   `mcpServers`, see `docs/mcp-server-kiro-notes.md` §2) with the ACP
   `McpServer` list + optional `_meta.kiro` payload. Small, mechanical, and
   upstreamable — cyril will want MCP config eventually anyway.
2. **KAS auth responder freshness.** cyril's `kas::auth` reads
   `~/.aws/sso/cache/kiro-auth-token.json`, which `kiro-cli login` does NOT
   refresh (known cyril gap "cyril-taba"). The spikes instead served fresh
   tokens from `data.sqlite3` per `_kiro/auth/getAccessToken` callback —
   long-lived wayfinder sessions need that behavior. Options: fix the
   responder to source sqlite (upstreamable), or synthesize the SSO file from
   sqlite at app launch (documented workaround in cyril's research). Budget
   this; do not discover it in production as a `-32000` after 10 idle
   minutes.
3. **`CancelRequest` is global** (main-session assumption). Irrelevant at one
   session; needs a `session_id` before concurrent node sessions cancel
   independently. Same class: the busy-guard, if reused, is per-connection in
   cyril today.

## What the app loop looks like (tracer bullet)

```
spawn_bridge(Kas) → NewSession{cwd, mcpServers:[wayfinder mcp-serve spec]}
  → recv_notification loop:
      SessionCreated        → hold session_id
      AgentMessage chunks   → stream into chat pane
      TurnCompleted         → idle
  → recv_permission loop:
      own-server tool call  → auto-approve (mcp notes §4)
  → present_questions round lands via the MCP server (app-side queue)
  → answers injected as SendPrompt{session_id, fenced-JSON envelope}
```

Every wire behavior in that loop is capture-backed by the three spikes; the
only new code is the Tauri shell and the two cyril-core deltas above.
