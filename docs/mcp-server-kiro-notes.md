# Building the wayfinder MCP server against Kiro — integration contract

Status: reference, 2026-08-09. Consolidates what the spikes proved
(`docs/spike-present-inject.md`, `docs/spike-user-input.md`), what previously
lived only in probe code, and Kiro-specific hazards from the cyril research
corpus that `design.md` §3/§4 do not cover. Everything marked **VERIFY AT
BUILD** is unproven for our exact configuration.

Reference implementation: `spikes/present-inject/mcp_server.py` — stdlib-only,
spike-proven on kiro-cli 2.16.2 (stdio + streamable-HTTP). The production
`mcp-serve` subcommand should preserve its observable behavior.

## 1. Registration — the wire contract

Declare the server per session in `session/new.mcpServers` (and see §2 for
reload). Shapes proven live by T1:

```jsonc
// stdio (chosen transport)
{ "name": "wayfinder",
  "command": "/path/to/wayfinder", "args": ["mcp-serve", "..."],
  "env": [ {"name": "WAYFINDER_MAP", "value": "<map-id>"},
           {"name": "WAYFINDER_NODE", "value": "<node-id>"},
           {"name": "WAYFINDER_TOKEN", "value": "<capability-token>"} ] }

// http (validated alternative)
{ "name": "wayfinder", "type": "http", "url": "http://127.0.0.1:<port>/mcp",
  "headers": [] }
```

- `env` is a **list of `{name, value}` pairs**, not an object (ACP `McpServer`
  shape — kiro rejects an object).
- kiro-cli spawns the stdio server as its own child, one per session; identity
  and the per-session capability token ride the env/args (design §3).

## 2. `session/load` must re-declare `mcpServers`

The T8 recovery flow (reload a dead session, inject the pending answer) passes
`mcpServers: [<same spec>]` on `session/load`. A reload without the spec gives
the restored session no tool access. The production reconnect path must
persist each session's server spec alongside the session id.

## 3. Server surface kiro requires (proven minimal set)

Newline-delimited JSON-RPC over stdio. Methods the reference server answers —
kiro calls all of these during session setup:

| Method | Reply |
|---|---|
| `initialize` | echo `protocolVersion`, `capabilities: {tools: {}}`, `serverInfo` |
| `tools/list` | `{tools: [...]}` with JSON-Schema `inputSchema` per tool |
| `tools/call` | `{content: [{type: "text", text: ...}]}` |
| `ping` | `{}` |
| `resources/list`, `prompts/list` | empty lists (kiro probes them) |
| notifications (`initialized`, …) | no response |

## 4. Behavior contract (spike-proven, restated from §4/design)

- **The turn-end convention lives in the tool description.** The
  `present_questions` description carries "END YOUR TURN immediately: the
  answers will arrive as a follow-up user message" — T2 (ends ≤4s) and T4
  (responsible speculation when tempted) measured its effect. Treat the
  description text as part of the protocol; change it only with a re-probe.
- **No client-side tool-call timeout ≥10 min** (T10, 605s park) — blocking
  mode is a safe fallback; mirror `PROBE_BLOCK_TIMEOUT` so a forgotten round
  eventually returns `TIMEOUT` to the agent instead of parking forever.
- **Every tool call fires `session/request_permission`** (T1). The permission
  responder in wayfinder-acp must auto-approve calls to wayfinder's own
  server: the request carries `toolCall.title` and `_meta.kiro {toolId,
  consent: {capability, resource}}` to key on. The alternative — a
  trusted-tools `permissions.rules` entry in an agent config — has an
  **unverified capability naming for MCP tools**; VERIFY AT BUILD if used.

## 5. Custom-agent allowlists filter MCP tools out — VERIFY AT BUILD

Both spikes ran sessions on the **default agent** (full toolkit). If wayfinder
sessions use custom agents (grilling roles etc.) with a `tools` allowlist, the
allowlist filters the toolkit — MCP tools included. From the KAS bundle: MCP
tools carry `@<server>/<tool>` tags; `includeMcpJson: true` on the agent
unions all MCP-tagged tools past the allowlist. Before shipping any custom
agent for wayfinder sessions, verify one of: (a) `includeMcpJson: true` pulls
in the session-declared wayfinder server's tools, or (b) the allowlist pattern
that matches them (bare name vs `@wayfinder/...` — untested). The arm-B
lesson applies: an agent whose allowlist misses a tool silently loses it.

## 6. Model caveat: GPT strict schemas degrade optional params

KAS ships optional schema params as `{"not": {}}` under GPT strict-schema
mode (cyril research). `present_questions` has optional fields (`recommended`,
`multi`). If a wayfinder session can run a GPT model, test the tool schema
under it — or make fields required with explicit null-ish sentinels avoided
(prefer required + empty-array/false defaults in the schema).

## 7. Send a real `clientInfo` at `initialize`

The spike probe sent `_meta.kiro.clientName` — **that field does not exist**;
KAS reads the standard ACP `initialize.clientInfo.name` and silently treats
absent/unknown clients as `kiro-ide` (persona affects the system prompt, not
the advertised surface). Production wayfinder-acp should send
`clientInfo: {name: "wayfinder", version: <ver>}` (or `kiro-cli` to match the
CLI persona) deliberately, not inherit the default by accident.

## 8. Lifecycle: assume the child can be orphaned

Who reaps the per-session MCP child when kiro-cli dies is unverified; kiro-cli
is known NOT to reap its KAS engine child (orphans accumulate — observed
repeatedly in cyril research). `mcp-serve` must exit on its own when the IPC
connection to wayfinder-core drops, and the app should kill the kiro process
group on session teardown rather than trusting the tree to unwind.
