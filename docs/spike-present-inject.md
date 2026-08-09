# Spike: present-and-inject question channel

Status: spec, ready to execute. 2026-08-08.
Gates: `design.md` §4 (question mechanism) and §11 build-order step 2. The
decision-card UI, grilling rounds, seams checkpoints, and quiz gates all rest
on this spike's outcome.

## 1. Purpose

Determine empirically whether an app-owned MCP server can present structured
questions to a kiro-cli session and receive answers injected as a later user
turn — and if not, which fallback holds. Also pick the MCP transport
(stdio shim vs in-process HTTP).

**Non-goals:** no Tauri shell, no real UI, no wayfinder-core, no rivets, no
concurrency pipeline. The "UI" in this spike is a scripted answerer.

## 2. Hypotheses under test

| # | Hypothesis | Pass condition |
|---|---|---|
| H1 | kiro-cli connects to and calls tools on our MCP server via stdio AND via HTTP | tool call observed on the wire for both transports |
| H2 | After `present_questions` returns, the agent ends its turn | `turn_end` observed within one continuation; no unbounded monologue |
| H3 | A user-turn injection after `turn_end` is accepted and processed | `session/prompt` accepted; agent references the answers in its next turn |
| H4 | Mid-turn injection is safe (queued or cleanly rejected) | deterministic, documented behavior — not corruption or silent loss |
| H5 | The `_kiro/session/notify` steering buffer is a viable injection route | message lands in the target session and is acted on |
| H6 | A machine-readable answer envelope is reliably parsed | agent acts on all N answers correctly in its response, N=3, across formats |
| H7 | Timeouts are survivable | a 10-minute-delayed answer still lands; no client-side MCP timeout kills the session (relevant to the blocking fallback) |

## 3. Harness

Three processes, scripted end to end; no human in the loop.

1. **Probe client** — standalone Python ACP driver in the style of cyril's
   `experiments/conductor-spike/probe-kas-*.py` (HOME-isolated, JSONL wire
   capture of every frame). Responsibilities: spawn kiro-cli (KAS lane),
   `initialize`, `session/new` with a probe agent config, send prompts,
   observe notifications, perform injections. Reuse cyril probe helpers
   verbatim where they exist (host-init, session setup, capture).
2. **Probe MCP server** — a ~100-line MCP server (Python, `mcp` SDK)
   exposing exactly:
   - `present_questions(questions: [{id, title, options[], recommended,
     multi?}])` → immediately returns `"presented; answers will arrive as a
     follow-up message"` and POSTs the round to the answerer queue.
   - Two trivial read tools (`get_map`, `list_frontier` returning canned
     JSON) so tool-call behavior isn't measured on a single tool alone.
   - Parameterizable: serve over stdio or HTTP (the transport A/B).
   - Optional blocking mode flag for H7 (park until answer arrives).
3. **Scripted answerer** — stands in for the UI. Reads presented rounds from
   a queue file/socket, waits a scripted delay, then performs the injection
   under test (`session/prompt`, or `_kiro/session/notify` via the probe
   client) with the answer envelope. Delays and answer contents are per-test
   parameters.

Agent config: dedicated probe agent, workflows gate ON (needed later for
`_kiro/session/notify`; harmless here), model configurable with `"auto"` as
the default — no hardcoded model id. Probe dir HOME-isolated per cyril probe
methodology; every run captures full JSONL wire traffic.

**Cost note:** real KAS turns cost credits (the 2.16.0 audit flags this per
probe). Minimize turns: batch multiple observations per session where the
test allows, prefer one long session with many rounds over many sessions.

## 4. Probe matrix

| Test | Setup | Observe | H |
|---|---|---|---|
| T1 | Same `present_questions` call over stdio server, then over HTTP server | tool registered, called, result returned on both transports | H1 |
| T2 | Prompt: "Use present_questions to ask me 3 things, then wait." Answerer injects after 2 s | does the turn end? time-to-`turn_end`; agent text after the tool result | H2 |
| T3 | T2, but injection 10 min later | injection accepted; session alive; no timeout artifacts | H3, H7 |
| T4 | Prompt engineered to tempt continuation ("…also start drafting the plan while you wait") | does the agent act on un-answered questions? does it fabricate answers? (informs seed-prompt wording, not just mechanism) | H2 |
| T5 | Agent calls `present_questions` twice in one turn before any answer | both rounds reach the answerer; define queue-vs-supersede behavior | — |
| T6 | Inject while the agent's turn is still running (trigger a slow tool, inject during it) | `session/prompt` mid-turn: queued? rejected? error shape? | H4 |
| T7 | Inject via `_kiro/session/notify` instead of `session/prompt` | steering-buffer delivery; does the agent act on it next turn? | H5 |
| T8 | Kill the session process with a question pending; restart probe with a fresh session | answerer/card resolves to dead-session state; nothing hangs | — |
| T9 | Three envelope formats across three rounds: (a) fenced JSON block, (b) markdown numbered list mirroring the questions, (c) plain `id: answer` lines | which format the agent parses and acts on most reliably (score: all answers referenced correctly, no hallucinated options) | H6 |
| T10 | Blocking-mode `present_questions`, answer after 10 min | does the tool call survive? MCP timeout observed? | H7 (fallback viability) |

## 5. Instrumentation

Per run, one JSONL capture containing, in order: every ACP frame (both
directions), every MCP frame (probe server side), answerer actions with
timestamps, and process lifecycle events. Analysis is a script that reduces a
capture to an event timeline (`tool_call → tool_result → turn_end →
injection → next turn_start`) plus per-test verdicts. Keep captures — they
are the evidence for the design.md update, same discipline as cyril's wire
audits.

## 6. Decision table

| Outcome | Consequence for design.md §4 |
|---|---|
| H2 + H3 pass | Present-and-inject is **chosen**. Card UI proceeds. |
| H2 passes, H3 fails, H5 passes | Inject via steering buffer instead; document as the route. |
| H2 fails (agent won't end turn reliably) | Seed-prompt contract changes: questions presented only at natural turn end, or fall back to blocking (H7) if timeouts allow. |
| H3 + H5 + H7 all fail | Kiro Crew directive pattern: tool result carries a marker, answers return through a channel outside MCP. Largest design change; revisit §4 wholesale. |
| H6 shows one format dominant | That format becomes the envelope schema in §4 and the seed-prompt contract. |
| H1 fails on one transport | The surviving transport is the §3 decision. |

## 7. Execution notes

- Runs live in `../cyril/experiments/` (alongside the existing conductor-spike
  probes, same conventions) or a `spikes/` dir in this repo — pick at
  execution time; the capture files are the deliverable either way.
- Every degenerate-case verdict gets written back into `design.md` §3/§4 in
  the same change that flips the mechanism from *hypothesis* to *chosen* (or
  to a fallback).
- If the spike reveals kiro-cli version sensitivity, record the exact binary
  version per capture, per cyril audit methodology.

## Results (2026-08-08 — kiro-cli 2.16.2, KAS lane, Windows 11)

Harness: `spikes/present-inject/probe.py` + `mcp_server.py`; captures in
`spikes/present-inject/captures/*.jsonl` (auth material redacted at capture
time; both directions recorded).

| Test | Verdict | Evidence |
|---|---|---|
| T1 stdio | **PASS** — tool registered, called, completed; `stopReason=end_turn` | T1-stdio.jsonl |
| T1 http | **PASS** — same over minimal streamable-HTTP | T1-http.jsonl |
| T2 | **PASS** — turn ended ≤4s after the present tool result (unprompted); injection accepted; 3/3 answers referenced | T2-stdio.jsonl |
| T3 | **PASS** — turn 1 ended ~4s after present; answer held a REAL 600s; injection accepted; 3/3 | T3-stdio.jsonl |
| T4 | **MIXED** — when invited to "keep working", the agent continued ~30s and wrote a draft plan. It did NOT fabricate answers: it used the recommended options marked "(pending confirmation)" with alternatives listed | T4-stdio.jsonl |
| T5 | **PASS** — two `present_questions` calls in one turn produced two queued rounds; both answered via separate injected turns | T5-stdio.jsonl |
| T6 | **PASS (deterministic preemption)** — mid-turn `session/prompt` CANCELS the in-flight turn: its prompt response arrives with `stopReason:"cancelled"`, then the injected turn runs to `end_turn` and processes the answers (3/3). (First analysis wrongly reported the first turn's response as never arriving — the probe's pump discarded the unmatched frame; capture inspection confirms the explicit cancellation.) | T6-stdio.jsonl |
| T7 | **FAIL** — client→agent `_kiro/session/notify` is dead: agent received nothing ("NONE"). The steering buffer is only reachable via the agent-facing `send_message` tool, as the 2.16.0 audit documented | T7-stdio.jsonl |
| T8 | **PASS (better than assumed)** — killed kiro-cli with a round pending, restarted, `session/load` succeeded (transcript replay observed), answer injection into the reloaded session referenced 3/3. Pending cards CAN survive process death | T8-stdio.jsonl |
| T9 | **PASS** — all three envelope formats parsed 3/3 (exact id+choice pairs) | T9-stdio.jsonl |
| T10 | **PASS** — a blocking MCP tool call parked 605s, completed on answer, `end_turn`, no timeout. Blocking fallback viable to ≥10 min | T10-stdio.jsonl |

Incidental findings:

- **Every MCP tool call triggers a `session/request_permission` prompt**
  (`allow_once`). The app must auto-approve its own server's tools (client-side
  auto-accept, or a trusted-tools entry in the agent config) or every question
  round dies on a permission card the user never sees.
- **Token staleness on idle sessions**: the probe initially cached the auth
  token; a 600s-idle session then failed its next prompt with `-32000` (token
  inside the host's 180s refresh buffer). Re-reading the token from
  `data.sqlite3` on every `_kiro/auth/getAccessToken` callback fixes it. Any
  long-lived app session must serve FRESH tokens per callback.

### Decision (per §6 decision table)

H1 (both transports), H2, H3, H6, H7 pass; H5 fails. **Present-and-inject is
CHOSEN**, with these binding operational rules discovered by the spike:

1. **Mid-turn injection preempts with an explicit cancellation.** The
   in-flight turn's `session/prompt` response arrives with
   `stopReason:"cancelled"` and the injected turn runs normally (T6). So the
   rule is not "never inject mid-turn" but "inject mid-turn only
   deliberately": it discards the active turn's remaining work, but the
   cancellation is signaled, never silent. Default to injecting after turn
   end; use mid-turn injection as the cancel-and-redirect mechanism.
2. **"End your turn" is not guaranteed.** If the agent has other work it may
   continue for tens of seconds (T4). The app must tolerate pending questions
   across continued work; T4 shows the agent speculates responsibly (marks
   recommendations "pending confirmation") rather than fabricating.
3. **Rounds queue, never supersede** — each round has its own id and its own
   answer turn (T5).
4. **Envelope: fenced JSON block** is canonical (all formats worked at N=3;
   JSON is the most robust for multi-select/custom answers) (T9).
5. **`_kiro/session/notify` is not an injection route** (T7).
6. **Blocking tool calls survive ≥10 minutes** — a viable fallback, though
   present-and-inject remains primary (T10).
7. **Transport: stdio** (simplest spawn story; HTTP equally functional if an
   in-process server is ever preferred) (T1).

`design.md` §3/§4 updated to match.
