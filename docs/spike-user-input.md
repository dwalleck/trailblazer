# Spike: native `_kiro/userInput` question cards via client-injected agents

Status: executed 2026-08-09 (reachability arms A–D + turn-end arms E–G).
kiro-cli **2.16.1**, KAS lane, Linux (CachyOS).
Harness: `spikes/user-input/probe.py`; captures in `spikes/user-input/captures/`
(auth redacted at capture time, both directions). Follow-up to the cyril
research review of `design.md` §4, which flagged that "Kiro CLI has no native
AskUserQuestion tool" is wrong at the wire level: KAS ships `_kiro/userInput`
(cyril `docs/kiro-2.7.1-wire-audit.md`), but the `user_input` tool is absent
from the default vibe toolkit.

## Question

Wayfinder controls its agent config. Does a client-injected custom agent
declaring `user_input` in `tools` (`session/new._meta.kiro.customAgents`),
selected as the session's main agent (`_meta.kiro.modeId`), plus the
`initialize.clientCapabilities._meta.kiro.userInput: true` gate, make KAS route
structured questions to the client natively — no MCP server needed?

## Method

Bundle-oracle reading of the installed 2.16.1 `acp-server.js` (per cyril's
"the shipped bundle IS the reference impl" methodology) to locate the exact
filter logic, then live arms — one fresh HOME-isolated kiro-cli spawn and one
real turn each. Fresh sqlite tokens served per `_kiro/auth/getAccessToken`
callback (present-inject spike lesson).

## Results

| Arm | Setup | Verdict |
|---|---|---|
| B | gate ON + injected `wayfinder-interviewer` (`tools:["user_input","fs_read"]`) as MAIN agent via `modeId` | **FAIL — vendor-pinned.** The mode mechanism itself worked (`session/new` returned `_meta.agentMode: "wayfinder-interviewer"`; the injected agent appears in the mode select), but the toolkit never contained `user_input`: the model reported its only tool was `disclose_context` and asked in chat instead. `B-stdio.jsonl` |
| D | gate ON + same agent dispatched as a **delegated** subagent stage (`subagentOrchestration` enabled at initialize; main session stays vibe) | **PASS — full round-trip.** `orchestrate_subagent` ran the stage as `wayfinder-interviewer`; the child called `user_input`; the client received `_kiro/userInput {sessionId (parent's), toolCallId, question, options:[{title, description, recommended}]}` — the model authored option descriptions and marked sqlite `recommended` unprompted. Scripted reply `{action:"answered", answer:"sqlite"}` resolved the tool call; the parent restated the choice. `D-stdio.jsonl` |
| A (control: vibe + gate + same prompt) | not run live | Covered twice over: arm B produced exactly the ask-in-chat fallback the control predicts, and cyril live-captured the same on 2.7.1. |
| C (agent + no gate) | not run live | Statically pinned in the bundle: without the `userInput` capability, `handleUserInputAction` falls back to a legacy `session/request_permission` bridge carrying flat option labels. Wayfinder always advertises the gate, so this path is moot. |

## Why arm B fails — the bundle's own words

`WorkspaceConnectionImpl.filterTools` (2.16.1) keeps a
`DELEGATED_ALLOWED_TOOLS = {report_progress, subagent_response, user_input}`
bypass that re-adds these tools **only** when `isDelegatedExecution` (subagent
stages, workflow step sessions). The doc comment states the main-session case
explicitly: `user_input` "never reaches a non-delegated mode session — neither
a restrictive allowlist nor a plain `'*'` policy — because it is never in that
session's candidate bundle for this bypass to re-add", and "the profile's
allowlist stands (pinned by the exact tool-set scenarios in
custom-agent.feature and plan-mode.feature). Spec agents still receive
`user_input` directly via getSpecTools / specOrchestratorTools." This is
deliberate, feature-test-pinned vendor behavior — not a gap a different agent
config can route around.

## Turn-end mechanics (arms E–G — the present-inject counterpart)

`user_input` inverts present-and-inject: the turn stays **open** on an awaited
client request (bundle: `await execute()` with **no timeout**), where
present-and-inject ends the turn and injects answers as new turns. Measured:

| Arm | Setup | Verdict |
|---|---|---|
| E | delegated question; answer held 600s | **PASS.** Turn stayed open the full park (no `turn_end`, zero traffic — not even keepalives), then the late answer resolved the tool, the crew completed, `turn_end {stopReason:"end_turn"}`. Native blocking survives ≥10 min — symmetric with present-inject T10 (605s MCP park). `E-stdio.jsonl` |
| F | delegated question; `session/cancel` 15s in | **Clean signaled teardown.** Stage tool_calls → `failed`, `turn_completion`, `turn_end {stopReason:"cancelled"}`, prompt response `"cancelled"` (same contract as present-inject T6). The pending `_kiro/userInput` request is left **outstanding** — and a reply sent 7s *after* cancellation is still accepted: `interaction_resolved {outcome:"answered"}` + tool_call `completed` land in the transcript, but no turn runs on it. Late answers are record-only. `F-stdio.jsonl` |
| G | delegated question; SIGKILL the process group; respawn; `session/load` | **Recovery is present-and-inject-shaped.** Load replays the transcript (9 `_meta.kiro.replay:true` frames) including the persisted `pending_interaction` **with full question+options** and a synthesized `turn_end {stopReason:"cancelled", replay:true}` for the interrupted turn. The live `_kiro/userInput` request does **not** re-fire — the pending card is rebuilt from replay data (a `pending_interaction` with no paired `interaction_resolved` by `toolCallId`), and the answer returns via a **fresh `session/prompt`**, which worked ("The project will use **sqlite**…"). `G-stdio.jsonl` |

Consumer contract for a future native-card client (§8 executor seam):

1. Render pending cards from `session_info_update kind:pending_interaction`
   (it carries the full question + options) or from the `_kiro/userInput`
   request itself; resolve on `interaction_resolved` (pair by `toolCallId`).
2. On `turn_end {stopReason:"cancelled"}`, invalidate pending cards. A late
   answer is accepted into the record but acted on by nobody — re-driving
   requires a new prompt.
3. After `session/load`, expect no re-fired request: rebuild unresolved cards
   from replayed `pending_interaction` frames and deliver answers as a new
   prompt. (The ws-mux `resendPendingUserInputs` path applies only to a
   still-live server with a reconnecting/observer client, not to process
   death.)
4. The `pending_interaction` ledger is general: delegated-stage tool approvals
   ride the same kinds (`interactionType: "tool_approval"`), so one feed can
   drive both question cards and approval cards.

**Design consequence:** Kiro's own native mechanism degrades to
*present-and-inject* at the moment durability matters (arm G) — recovery is
"replay pending question as data, answer via new prompt". Re-creating
userInput's blocking flavor app-side would buy UX symmetry, not robustness;
`design.md` §4's chosen mechanism is validated rather than challenged.

## Incidental findings

- **`session/new._meta.kiro.modeId` works** — the session is created directly
  in the requested mode, including a client-injected agent id. Injected agents
  are listed in the `mode` config option (source displayed as "bundled" —
  `modeGroupLabel` maps `client` → `bundled`).
- **Native pending questions are reload-safe as data, not as live requests.**
  The bundle persists userInput interaction state "so pending inputs survive
  disconnects and session reloads" — live-verified by arm G: the pending
  question replays on `session/load`; the request itself does not re-fire (see
  Turn-end mechanics). The ws mux additionally re-sends pending requests to
  clients joining a *still-live* server (`resendPendingUserInputs`). This is
  the vendor-side twin of present-inject T8.
- The question also surfaces as a `tool_call` update in the session stream
  (title = the question text) — a second render hook alongside the callback.
- kiro-cli does not reap its KAS child (known cyril finding) — the probe kills
  its process group; 12 pre-existing orphaned `acp-server.js` processes from
  earlier research were swept during this spike.

## Decision

- **§4 stands for v1.** Present-and-inject via the app-owned MCP server remains
  the mechanism for plain per-node sessions — native cards are structurally
  unreachable there.
- **§4's premise corrected.** "No native AskUserQuestion" → "native
  `_kiro/userInput` exists but is unreachable in plain main sessions;
  spec agents and delegated children get it."
- **§8 gains an adoption upside.** If node sessions ever run as workflow steps
  or subagent stages (the executor seam), they are delegated — native
  `_kiro/userInput` cards arrive with zero MCP machinery and reload-safe
  pending state. The decision-card UI should stay source-agnostic; the
  `present_questions` card schema and `UserInputOption` are near-identical
  (title/description/recommended/subOptions).
