# Wayfinder — design

Status: design, pre-implementation. 2026-08-08.

A desktop app that runs the Matt Pocock wayfinding flow as a first-class GUI:
chart a map of decision tickets out of the fog, work each node in its own agent
session, then run `to-spec` per node and `to-tickets` per spec. Standalone
Tauri application — not a Kiro Crew built-in — assembled from three donor
projects: **rivets** (storage), **cyril** (agent protocol lane),
**kiro-control-center** (Tauri design framework).

## 1. The flow

Map lifecycle:

```
charting ──► working ──► clear ──► speccing ──► ticketed ──► handed-off
```

Node lifecycle:

```
fog ──► drafted ──► blocked ──► frontier ──► claimed ──► resolved
                          └────────► out-of-scope
```

- Node types: `research` (AFK, background turn), `prototype` (HITL),
  `grilling` (HITL, the default), `task` (either). One ticket per session,
  except research.
- **Destination shape** is a map-level setting fixed during charting:
  `single-spec` (canonical wayfinder: the map collapses into one spec) or
  `spec-per-node` (each resolved node that yields a deliverable gets its own
  spec). Nodes are marked `yields: spec | decision-only`; speccing fans out
  over the `spec` ones only.
- Fog graduation: resolving a node may clear fog patches, which graduate into
  new drafted nodes (create-then-wire, edges in a second pass). A resolution
  may also rule existing nodes out of scope or invalidate them.

## 2. Architecture

```
crates/
  wayfinder-core/   # domain: map, node, fog, intents, mutation pipeline,
                    #   driver lease, journal, tracker adapters, MCP server
  wayfinder-acp/    # agent connectivity, carved from cyril-core's KAS lane:
                    #   sessions, notification routing, approvals;
                    #   executor behind a trait (the workflow-engine seam)
  wayfinder-app/    # Tauri 2 shell in the kiro-control-center pattern:
                    #   thin src-tauri/commands over wayfinder-core,
                    #   Svelte 5 + Tailwind 4 frontend, tauri-specta bindings
```

Donor contributions:

| Donor | What it gives |
|---|---|
| rivets (`dwalleck/rivets`) | `rivets-jsonl` crate linked directly (atomic JSONL writes, same `IssueStorage` path `rivets-mcp` wraps). No CLI spawning for the rivets adapter. |
| cyril (`cyril-core`) | The KAS ACP lane: `protocol/kas/` (auth, callbacks, discovery, hooks, settings, host_io), transport, session state, approval prompts. No `_kiro/workflow/*` consumer exists yet — that is new code guided by `cyril/docs/kiro-2.16.0-wire-audit.md`. **Measured embedding surface + carve-later decision: [`docs/wayfinder-acp-carve-map.md`](wayfinder-acp-carve-map.md)** — milestone 1 depends on cyril-core directly (5-call surface); the carve is a post-tracer-bullet refactor. |
| kiro-control-center | The design framework: Rust core crate + thin `src-tauri/commands/*.rs` + SvelteKit-static Svelte 5 app + Tailwind 4 + tauri-specta generated `bindings.ts` + vitest + Playwright. |

## 3. The agent interface: one app-owned MCP server

**Transport: stdio, decided by spike** (T1 A/B, kiro-cli 2.16.2 — both
transports worked end to end; stdio wins on spawn simplicity). kiro-cli spawns
the MCP server as a child process per session (an `mcp-serve` subcommand of
the app binary) that bridges back to the running wayfinder-core over local
IPC. Identity arrives as launch args: map id, node id, and a per-session
**capability token** minted by the core when the session is created. The HTTP
variant (in-process server, per-session URL token) is equally functional per
the spike and remains an option if the IPC bridge ever hurts.

Tool surface — deliberately the ONLY tracker access the agent gets:

| Tool | Kind | Purpose |
|---|---|---|
| `get_map` | read | Map body: destination, notes, decisions-so-far, fog |
| `get_node` / `list_frontier` | read | Node detail / open-unblocked-unclaimed set |
| `present_questions` | interaction | Render a round of questions as UI cards; answers return to the agent (see §4) |
| `submit_resolution_intent` | write (mediated) | The agent's ONLY write path. Validates the intent schema and submits it to the mutation pipeline (§5) |

**Raw `rivets-mcp` is NOT registered for wayfinder sessions.** Its tool
surface includes `create`, `update`, `close`, `dep` — direct writes that would
bypass the driver lease and break the single-driver invariant. If rivets-mcp
is ever used, it must be tool-filtered to read-only verbs; the preferred path
is that wayfinder-core's own server implements the reads (it links the same
storage crate anyway).

This single component resolves two design problems at once: it is the choke
point that makes the concurrency contract enforceable, and it is the channel
that gives Kiro a structured question-asking capability (§4).

Build contract: [`docs/mcp-server-kiro-notes.md`](mcp-server-kiro-notes.md) —
the wire registration shapes, required method surface, permission/auto-approve
mechanics, and the verify-at-build hazards (custom-agent allowlists × MCP
tools, GPT strict schemas, child lifecycle).

## 4. Asking questions without an AskUserQuestion tool

Kiro's KAS lane ships a native structured-question callback — `_kiro/userInput`
(options with descriptions, `recommended`, nested `subOptions`) — but it is
**unreachable in a plain main session**: the `user_input` tool is never in a
non-delegated session's candidate toolkit, even for a client-injected custom
agent that declares it (vendor-pinned; validated by
`docs/spike-user-input.md`, kiro-cli 2.16.1). Only spec-flow agents and
*delegated* children (subagent stages, workflow steps) get it — see §8 for the
delegated upside. So for v1's plain per-node sessions there is effectively no
native `AskUserQuestion`. Prior art:
Kiro Crew ships an `ask_question` MCP tool that works with kiro-cli today —
the gateway holds a pending question keyed by `ask_id`, the dashboard renders
a card, and the answer routes back (`docs/architecture/mcp.md` in KiroCrew).
Two viable mechanics for wayfinder:

1. **Blocking tool call.** `present_questions` parks on a channel until the
   human answers; the answers are the tool result. Simple, but hostage to
   kiro-cli's MCP tool-call timeout (unverified) and to AFK users.
2. **Present-and-inject (CHOSEN — validated by spike, kiro-cli 2.16.2; see
   `docs/spike-present-inject.md` Results).**
   `present_questions(questions: [{id, title, options[], recommended,
   multi?}])` validates and returns immediately: "presented; answers will
   arrive as a follow-up message." The app renders the round as decision
   cards (recommended option pre-selected, one-click accept, custom-answer
   field). On submit, the app injects the answers into the session as a
   fenced-JSON envelope via `session/prompt`.

   Binding operational rules, each earned by a spike test:

   - **Inject after turn end by default.** Mid-turn `session/prompt`
     preempts: the active turn's response arrives with
     `stopReason:"cancelled"` and the injected turn runs normally (T6). Use
     mid-turn injection only deliberately, as the cancel-and-redirect
     mechanism — the cancellation is signaled, never silent.
   - **"End your turn" is a convention, not a guarantee.** Untempted, the
     agent ends its turn within seconds of presenting (T2); invited to keep
     working, it continued ~30s and drafted speculatively — responsibly
     (recommendations marked "pending confirmation"), not fabricating (T4).
     The app tolerates pending questions across continued work.
   - **Rounds queue, never supersede** — separate ids, separate answer turns
     (T5). Ten-minute-delayed answers land fine (T3).
   - **Envelope: fenced JSON block** — all tested formats parsed perfectly at
     N=3; JSON is the most robust for multi-select/custom answers (T9).
   - **`_kiro/session/notify` is not an injection route** (T7).
   - **Fallback:** a blocking `present_questions` call survived a 605-second
     park with no MCP timeout (T10), if present/inject ever regresses.
   - **Session liveness**: process death with a pending question is
     detectable, and `session/load` after a hard kill reloads the session —
     a delayed answer injected into the reloaded session is processed (T8).
     So pending cards survive restarts: attempt reload first; the
     dead-session card state is the fallback only when reload fails.
   - **Every MCP tool call triggers a permission prompt** — the app must
     auto-approve its own server's tools (client-side auto-accept or a
     trusted-tools agent-config entry) or every round stalls on an unseen
     card (T1).
   - **Serve fresh auth tokens per `_kiro/auth/getAccessToken` callback** —
     a cached token fails inside the host's 180s refresh buffer on any
     session idle past token expiry (T3's first run failed exactly this
     way).

Grilling maps onto this directly: the grilling skill asks the whole frontier
in one round — so one `present_questions` call = one round = one batch of
cards = one batched answer message. `wait-what`/clarification flows are the
same tool with one question.

Rejected alternative: MCP elicitation (server-initiated input requests).
Elicitation is answered by the MCP *client* — kiro-cli — which has no UI
surface for it that we control. The whole point is rendering in OUR UI.

Rejected alternative: native `_kiro/userInput` as the v1 mechanism. Spiked
(`docs/spike-user-input.md`): a plain main session cannot obtain the
`user_input` tool no matter what the injected agent's `tools` declares — the
exclusion is feature-test-pinned in the KAS bundle. It works fully for
delegated children (live round-trip verified), so it returns as an option only
with the §8 executor seam.

HITL vs AFK is expressed per call: grilling sessions block indefinitely for
the human; sessions whose skills permit AFK progress get a timeout answer of
`"deferred"` and proceed per their rules.

## 5. Concurrency contract

> **Single-driver invariant (v1):** exactly one app instance drives structural
> mutations for a project at a time. Claims may race (readback arbitrates);
> structural writes may never race (the driver lease prevents it; the
> revision check exists to detect violation, not to arbitrate).

- **Lease.** Tauri single-instance + a project-scoped lock file
  (`.rivets/wayfinder.drv`, `.scratch/<effort>/.drv`, or a recorded driver id
  on GitHub). Holding the lease is what permits the mutation pipeline to run.
  A second instance opens read-only with a "driven elsewhere" banner.
- **External writers** (human runs the rivets CLI, `gh`, edits files) cannot
  be prevented, so they are **detected, never silently merged**: poll the
  revision token; on mismatch, re-read, mark externally-changed nodes, show a
  reconcile banner, and reject in-flight intents back to the UI with the
  fresh head.
- **Claims** (has a winner): write assignee/status, then readback-confirm;
  the loser reads someone else's claim and gets `already_claimed`.
- **Structural writes** (no winner — map body, create-then-wire): only the
  lease-holder performs them, through the mutation pipeline:
  1. Validate the intent (agent-authored = hostile: schema, bounds, types).
  2. Optimistic revision check against the intent's `base_revision`; stale →
     semantic rebase (apply only if touched sets don't intersect mutations
     since the base), else reject with fresh state.
  3. Idempotent apply: per-map `applied-mutations.jsonl` journal keyed by
     `mutation_id`; replay returns the original result. Node creation
     dedupes on the fog-patch id it graduates from and on client slug
     (upsert-by-slug within the map). Create-then-wire pass 2 takes ids from
     the journal, so retries wire edges to the SAME nodes — no duplicates.
  4. Write-through to the tracker, then frontier recompute + UI update.
- **Compensation is self-scoped.** Rollback may only touch nodes this driver
  created AND that are unchanged since creation (journal hash match).
  Anything externally touched is left in place and logged. Compensation never
  deletes another writer's work.
- **Mid-flight invalidation:** closing an unclaimed node is immediate; a
  claimed node is marked `obsolete`, its live session is halted, unassigned,
  then closed with a pointer note. Work in flight is never silently deleted.

Per-adapter mechanics:

| Concern | Rivets (linked crate) | Local markdown | GitHub |
|---|---|---|---|
| Revision token | JSONL mtime + content hash | SHA-256 of `map.md` + issues-dir listing | issue `updated_at` |
| Lease artifact | `.rivets/wayfinder.drv` | `.scratch/<effort>/.drv` | driver id recorded on the map issue |
| Claim | status + assignee write, readback | `Status:` under file lock | `--add-assignee`, readback |
| Multi-op compensation | delete own orphaned nodes, remove own edges | remove own files | close own orphans + `wayfinder:aborted` label |
| External-write detection | hash mismatch → reconcile state | hash mismatch → reconcile | `updated_at` mismatch → reconcile |

A true multi-driver lease/rebase protocol is out of scope for v1; revisit only
if two drivers must ever work one map simultaneously.

## 6. Tracker adapters

One interface, three backends. The interface IS the concurrency contract
surface: `revision()`, `read_map()`, `read_frontier()`, `claim(node)`,
`apply_intent(intent)`, `acquire_lease()` / `release_lease()`.

Wayfinding-op mapping:

| Op | Rivets | Local markdown | GitHub |
|---|---|---|---|
| Map | `--kind epic`, label `wayfinder:map` | `.scratch/<effort>/map.md` | issue, label `wayfinder:map` |
| Child ticket | `parent-child` dep + `wayfinder:<type>` label | `issues/NN-<slug>.md`, `Type:` line | sub-issue + `wayfinder:<type>` label |
| Blocking edge | `blocks` dep | `Blocked by: NN` line | native `blocked_by` dependency |
| Frontier | `ready` + label filter | scan + filter | children minus blocked/assigned |
| Resolve | note append + close | `## Answer` + `Status: resolved` | comment + close |
| Decisions-so-far | epic body update | `map.md` append | map body edit |

v1 ships rivets + local-markdown; GitHub is additive behind the same seam.

## 7. UI

Framework: kcc pattern (Svelte 5, SvelteKit static adapter, Tailwind 4,
specta bindings, vitest + Playwright).

- **Map canvas** — the differentiating view. SVG layered-DAG layout
  (longest-path layering; dozens of nodes, no graph library). Node state by
  color/shape; fog patches as dimmed dashed blobs; blocking edges as arrows;
  resolved nodes show their gist on hover.
- **Decision cards** — the answer UI from §4. Numbered cards per round,
  recommended option pre-selected, accept-all, custom answers. Reused by
  grilling sessions, charting, and the seams checkpoint in to-spec.
- **Draft-approve-publish** — charting and to-tickets draft into a preview
  canvas (editable: rewire edges, retype/delete nodes); Publish hands the
  approved structure to the pipeline. The quiz-the-user gate becomes visual.
- **Chat pane** — embedded ACP session stream for HITL tickets.
- Layout: left rail = maps; center = canvas; right = context panel (node
  detail / live chat / rendered spec). Phase strip on top:
  Chart → Work → Spec → Tickets → Handoff.

## 8. Executor seam and the KAS workflow engine

v1: per-node sessions are plain ACP sessions through wayfinder-acp. Research
tickets are one-shot background turns.

The kiro-cli 2.16.0 `_kiro/workflow/*` engine (KAS-lane only; the v2 Rust
backend Kiro Crew uses has no workflow surface) maps cleanly onto this domain:
map → run, frontier → `parallel` scheduler, fog graduation → `update` /
`steps_queued`, "work until clear" → `repeat` with `onMaxIterations: pause`,
external triggers → `watch`. It stays behind the executor trait until there
is a reason to adopt it. Adoption carries a discovered upside
(`docs/spike-user-input.md`): step/subagent sessions are *delegated*, so they
get the `user_input` tool by bypass and route questions to the client as
native `_kiro/userInput` cards — no MCP machinery; a parked question holds
the turn open ≥10 min with no timeout; pending questions replay as data on
`session/load` (the request does not re-fire — the answer returns via a fresh
prompt, i.e. Kiro's own crash recovery is present-and-inject-shaped). The
spike's "Turn-end mechanics" consumer contract covers cancel/late-answer/
reload handling. Keep the decision-card UI source-agnostic; the
`present_questions` schema and `UserInputOption` are near-identical. When that day comes, the wire audit's consumer
hazards are the implementation contract: `run_complete` can mean *paused*;
`node_start` double-emits; `steps_queued` is overloaded; `node_complete` is
not a liveness signal; `paused` and `node_paused` are independent;
`replace_remaining` alone does not resume an exhausted loop.

## 9. Downstream: spec and tickets

- **Speccing** (per `yields: spec` node): synthesis-only session (to-spec
  forbids interviewing), one seams checkpoint rendered as a decision-card
  round. Spec markdown rendered in the right panel, stored per tracker.
- **Ticketing** (per spec): draft breakdown → quiz gate on the preview canvas
  → publish as tracer-bullet tickets with blocking edges, labelled
  `ready-for-agent`.
- **Handoff:** execution happens elsewhere in v1 — tickets are agent-grabbable
  by construction. In-app execution loops are a later decision.

## 10. Risks

1. **The cyril carve is re-shaping, not disentangling** (downgraded
   2026-08-09 after measuring the donor). The KAS lane + protocol plumbing is
   ~17.4k lines and completely UI-free (cyril's core-never-imports-UI rule
   held), and commands/notifications are already session-id-targeted
   (`BridgeCommand::SendPrompt {session_id}`, `RoutedNotification`). What
   remains is generalizing the few main-session assumptions (global
   `CancelRequest`, global busy-guard, main-vs-subagent routing living in
   cyril's binary) to N peer sessions. Structured work, not extraction
   archaeology — but still not a clean dep.
2. ~~MCP tool latency/timeout behavior~~ **RESOLVED by the spike**
   (`docs/spike-present-inject.md` Results): present/inject validated,
   blocking fallback viable ≥10 min, permission auto-approval and fresh-token
   serving are the two operational requirements it surfaced.
3. **The domain is new code regardless of host.** Canvas, pipeline, intents,
   MCP server — the pivot removes platform friction, not domain work.
4. **Single-user desktop assumption** is baked in by the lease model.
5. Two advisory rounds hardened the concurrency contract; a third class
   remains untested until built: crash mid-pipeline (journal replay must be
   verified against each adapter, not just designed).
6. ~~N concurrent node sessions with parallel turns on ONE KAS connection~~
   **RESOLVED by spike** (`docs/spike-parallel-turns.md`, 2026-08-09): three
   sessions ran turns simultaneously on one stdio connection — 126/178 chunks
   interleaved across concurrently-flowing streams, flat first-token latency,
   clean per-session routing, and (bonus) per-session error isolation
   observed live. Residual: unmeasured beyond N=3; re-measure before assuming
   linear scaling past ~4–6 concurrent AFK nodes.
7. **Vendor drift.** KAS ships roughly biweekly and the `_kiro/*` wire moves
   (the spikes ran on 2.16.1/2.16.2; the audits anchor at 2.16.0). Mitigation
   is inherited, not built: cyril's release-diff discipline and the
   bundle-as-oracle method. Wayfinder should pin the kiro-cli version it was
   validated against and re-run the two spike harnesses on upgrade.
8. **Credit economics.** Every node session burns real KAS turns; AFK
   research fan-out multiplies it. Not a blocker — but the app needs a
   visible per-map spend model from day one, and the sidecar metering files
   (`~/.kiro/sessions/cli/*.json`, per cyril research) are the only on-disk
   source to build it from.
9. Two **verify-at-build** items from the MCP contract
   (`docs/mcp-server-kiro-notes.md`): custom-agent `tools` allowlists filter
   MCP tools out unless `includeMcpJson`/pattern-matched, and the
   `permissions.rules` capability naming for MCP tools is unverified. Each is
   a one-turn check; run them before the first custom agent ships.

## 11. Build order

### Milestone 1 — tracer bullet (the first deliverable)

One thin vertical slice of the real app, keeper code, cutting across steps
1–4 below: **Tauri window opens → cyril-core bridge spawns one KAS session
(wayfinder's MCP server declared at `session/new`) → the agent calls
`present_questions` → real decision cards render in the window → the answer
injects as a fenced-JSON prompt → the agent's confirmation streams back into
the chat pane.** Every wire behavior in that loop is capture-backed
(`docs/spike-present-inject.md`, `docs/spike-user-input.md`,
`docs/spike-parallel-turns.md`); the app loop is sketched end-to-end in
`docs/wayfinder-acp-carve-map.md`.

In scope (the only new code):

1. cyril-core deltas (carve map §"known deltas"): `mcpServers` + optional
   `_meta.kiro` on `NewSession`/`LoadSession`; fresh-token auth handling.
   Depend on cyril-core by path — **no carve** (carve map headline).
2. Production `mcp-serve` subcommand: port
   `spikes/present-inject/mcp_server.py` per
   `docs/mcp-server-kiro-notes.md` (present_questions + the two read stubs).
3. Minimal Tauri shell (kcc pattern): card panel + chat stream. No canvas.
4. Wire-through: permission auto-approve for own tools, round journal
   (present/resolve by round id), answer injection after turn end.

Out of scope: map canvas, lease, tracker adapters, fog, spec/ticket phases,
multiple sessions, custom agents. Done when: the loop above runs on a real
`kiro-cli` twice in a row, including once surviving an app restart with a
round pending (journal → reload → inject, the T8 path).

Design sessions scoping this milestone should treat `design.md`, the spike
docs, the MCP notes, and the carve map as **settled input** — interview only
on what blocks this slice; park anything finer as an issue.

### Full sequence

0. ~~N-session parallel-turn probe~~ **DONE — PARALLEL**
   (`docs/spike-parallel-turns.md`): the per-node session architecture is
   licensed by observation, not inference.
1. `wayfinder-core`: rivets-via-crate adapter + lease + journal + intent
   pipeline. Headless, unit-tested: two drivers racing, external-write
   detection, self-scoped compensation, crash-replay.
2. MCP server + present/inject spike against a real kiro-cli session.
   Spec: `docs/spike-present-inject.md` — this spike decides whether §4's
   mechanism is chosen or falls back, so it can run before or in parallel
   with step 1 (they share no code).
3. Tauri shell + map canvas (read-only rendering of a live rivets store).
4. Chart session end-to-end (grilling cards → draft → approve → publish).
5. Work loop: claim, ticket sessions, resolution intents, fog graduation.
6. Spec/tickets phases. 7. GitHub adapter. 8. Executor seam consumers.

## Open questions

- Does `present_questions` need a "user edited the question" escape hatch
  (human reframes rather than answers)?
- Where do prototypes (the `prototype` ticket type) live — branches in the
  target repo, per the Pocock skill, or app-managed worktrees?
- Does the app ever manage multiple projects at once, or one window per
  project (lease model assumes the latter)?
