# Spike: parallel turns across N sessions on one KAS connection

Status: executed 2026-08-09. kiro-cli 2.16.1, KAS lane, stdio, Linux.
Harness: `spikes/parallel-turns/probe.py`; capture
`spikes/parallel-turns/captures/parallel-stdio.jsonl` (final run).
Resolves `design.md` risk 6 / build-order step 0 — the last un-probed
load-bearing wire assumption under "work each node in its own session".

## Verdict: **PARALLEL** — concurrent turns are real, streams multiplex, routing and errors are per-session clean

Three runs, converging:

| Run | Setup | Result |
|---|---|---|
| 1 | 2 sessions, short generations, B fired while A mid-turn | B accepted + `turn_start` 1.84s before A's `turn_end`; both `end_turn`; first-token latency identical (~2.0s) for both → requests concurrent. Streams barely interleaved (turns shorter than first-token latency) — ambiguous between parallel and a stream gate. |
| 2 | 3 sessions, medium generations, staggered | All accepted; windows overlap pairwise; latencies flat (1.77–1.94s); but streams strictly back-to-back (each next stream began ≤0.5s after the previous ended) — still consistent with BOTH hypotheses. |
| 3 | 3 sessions, long generations (the discriminator: if a stream gate exists, B's first token must wait ~20s for A) | **All 3 turns simultaneously open; 126 of 178 chunks interleaved** (B streamed 8.3–14.0s, C 10.0–13.2s, concurrently); B/C first tokens arrived ~1.8–1.9s after their own `turn_start`, mid-other-stream. Serialization falsified. |

No `activePrompts`-style rejection ever fired for cross-session concurrency
(that guard is per-session, as the bundle indicated). Content routing was
clean in all runs — every chunk carried its `sessionId`, sentinels never
crossed sessions, prompt responses resolved per-request-id.

## Bonus finding: per-session error isolation, live-demonstrated

Run 3's deliberately degenerate prompts ("write four hundred numbers as
words") tripped the backend content filter on sessions A and C:
`-32000 "Model output was blocked by content filtering policy"`, each arriving
as that session's own prompt-response error plus `turn_end
{stopReason:"error"}` — while session B, running concurrently on the same
connection, streamed to a normal `end_turn` undisturbed. One node session's
turn failure does not perturb its siblings. (Also a caveat for prompt design:
highly repetitive bulk-generation prompts can trigger the content filter —
run 2's shorter variants of the same prompts all passed.)

## What this licenses in `design.md`

- The per-node-session architecture (§1/§8) stands on observed behavior:
  one kiro-cli process, N sessions, turns in flight simultaneously, ~flat
  first-token latency at N=3.
- The wayfinder-acp carve needs a per-session busy model (cyril's global
  busy-guard is the donor's main-session assumption to generalize), and the
  turn-mechanics rules from the other spikes apply per session.
- Unmeasured beyond N=3: throughput at larger N and any backend-side
  concurrency ceiling. If maps routinely run >4–6 concurrent AFK nodes,
  re-measure before assuming linear scaling.
