#!/usr/bin/env python3
"""Probe MCP server for the present/inject spike (wayfinder/docs/spike-present-inject.md).

Stdlib-only MCP server: newline-delimited JSON-RPC over stdio (default) or a
minimal streamable-HTTP endpoint (--http PORT). Exposes exactly the three
tools the spike needs:

  present_questions  — writes round-<id>.json into the queue dir; returns
                       immediately (default) or blocks until answer-<id>.json
                       appears (PROBE_BLOCKING=1, for T10).
  get_map            — canned map JSON (tool-call baseline).
  list_frontier      — canned frontier JSON (tool-call baseline).

Config via env (the probe passes these through session/new mcpServers env):
  PROBE_QUEUE_DIR      — required; where rounds/answers land.
  PROBE_BLOCKING       — "1" = present_questions parks until answered.
  PROBE_BLOCK_TIMEOUT  — seconds before a blocking call returns TIMEOUT (900).
"""
import json
import os
import sys
import time
import uuid

QUEUE = os.environ.get("PROBE_QUEUE_DIR", "")
BLOCKING = os.environ.get("PROBE_BLOCKING") == "1"
BLOCK_TIMEOUT = float(os.environ.get("PROBE_BLOCK_TIMEOUT", "900"))
HTTP_PORT = 0
for i, a in enumerate(sys.argv):
    if a == "--queue" and i + 1 < len(sys.argv):
        QUEUE = sys.argv[i + 1]
    if a == "--http" and i + 1 < len(sys.argv):
        HTTP_PORT = int(sys.argv[i + 1])

if not QUEUE:
    print("PROBE_QUEUE_DIR or --queue required", file=sys.stderr)
    sys.exit(2)
os.makedirs(QUEUE, exist_ok=True)


def log(*a):
    print("[probe-mcp]", *a, file=sys.stderr, flush=True)


CANNED_MAP = {
    "destination": "Probe map: decide the deployment story for a demo service.",
    "decisions_so_far": [],
    "fog": ["observability story not yet specifiable"],
}
CANNED_FRONTIER = [
    {"node": "auth-transport", "type": "grilling", "question": "How do clients authenticate?"},
    {"node": "retry-policy", "type": "grilling", "question": "What retry policy do we use?"},
]

PRESENT_DESCRIPTION = (
    "Present a round of structured questions to the human operator as UI cards. "
    "Call this INSTEAD of asking questions in plain text whenever you need the "
    "operator to make choices. Each question has an id, a title, 2-5 mutually "
    "exclusive options, and your recommended option. After calling this tool, "
    "END YOUR TURN immediately: the answers will arrive as a follow-up user "
    "message. Do not speculate about the answers and do not continue working "
    "on anything that depends on them."
)

TOOLS = [
    {
        "name": "present_questions",
        "description": PRESENT_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "options": {"type": "array", "items": {"type": "string"}},
                            "recommended": {"type": "string"},
                            "multi": {"type": "boolean"},
                        },
                        "required": ["id", "title", "options"],
                    },
                }
            },
            "required": ["questions"],
        },
    },
    {
        "name": "get_map",
        "description": "Return the current wayfinding map (destination, decisions so far, fog).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_frontier",
        "description": "Return the open, unblocked, unclaimed decision nodes.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def present(args):
    round_id = uuid.uuid4().hex[:8]
    rec = {
        "round_id": round_id,
        "presented_at": time.time(),
        "questions": args.get("questions", []),
    }
    path = os.path.join(QUEUE, f"round-{round_id}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)
    os.replace(tmp, path)
    n = len(rec["questions"])
    log(f"presented round {round_id} ({n} questions), blocking={BLOCKING}")
    if not BLOCKING:
        return (
            f"Presented round {round_id} with {n} question(s) to the operator. "
            "Answers will arrive as a follow-up user message. End your turn now and wait."
        )
    deadline = time.time() + BLOCK_TIMEOUT
    apath = os.path.join(QUEUE, f"answer-{round_id}.json")
    while time.time() < deadline:
        if os.path.exists(apath):
            with open(apath, encoding="utf-8") as f:
                return f.read()
        time.sleep(0.5)
    return json.dumps({"round_id": round_id, "answers": None, "status": "TIMEOUT"})


def call_tool(name, args):
    if name == "present_questions":
        return present(args or {})
    if name == "get_map":
        return json.dumps(CANNED_MAP, indent=2)
    if name == "list_frontier":
        return json.dumps(CANNED_FRONTIER, indent=2)
    raise KeyError(name)


def handle(msg):
    """One JSON-RPC message -> response dict, or None for notifications."""
    m = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if m == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "wayfinder-probe", "version": "0.1.0"},
            },
        }
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if m == "tools/call":
        try:
            text = call_tool(params.get("name"), params.get("arguments"))
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        except KeyError:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"},
            }
    if m == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if m in ("resources/list", "prompts/list"):
        key = "resources" if m.startswith("resources") else "prompts"
        return {"jsonrpc": "2.0", "id": rid, "result": {key: []}}
    if rid is None:
        return None  # notification (initialized, cancelled, ...) — ignore
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "error": {"code": -32601, "message": f"method not found: {m}"},
    }


def serve_stdio():
    log(f"stdio mode, queue={QUEUE}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


def serve_http(port):
    """Minimal streamable-HTTP MCP: POST a JSON-RPC message, get JSON back."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            try:
                msg = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self.send_response(400)
                self.end_headers()
                return
            resp = handle(msg)
            body = json.dumps(resp).encode() if resp is not None else b""
            self.send_response(200 if resp is not None else 202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    log(f"http mode on 127.0.0.1:{port}, queue={QUEUE}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    if HTTP_PORT:
        serve_http(HTTP_PORT)
    else:
        serve_stdio()
