#!/usr/bin/env python3
"""
Spike: N concurrent sessions with PARALLEL TURNS on one KAS connection.

design.md risk 6 / build-order step 0 — the last un-probed load-bearing wire
assumption under "work each node in its own session". Proven for the v2 engine
(cyril research); for KAS only inferred (the ws-mux in-flight-prompt guard is
per-session). This probe observes it live on the stdio path wayfinder uses.

Method: one `kiro-cli acp --agent-engine kas` process, two `session/new`, then
prompt A; the moment A's first agent_message_chunk arrives (A is mid-turn),
prompt B. Both prompts are pure text generation (no tools — nothing to
serialize on) with distinguishable sentinels.

Verdicts:
  PARALLEL    — B's turn_start precedes A's turn_end; chunks interleave;
                both stopReason=end_turn; no cross-talk (sentinels route to
                their own sessions).
  SERIALIZED  — B's first activity only after A's turn_end.
  REJECTED    — B's session/prompt errors while A is in flight.

Auth: fresh sqlite token per _kiro/auth/getAccessToken. HOME-isolated spawn.
Capture: captures/parallel-stdio.jsonl, auth redacted.
"""

import json
import os
import pathlib
import queue
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
REAL_HOME = pathlib.Path.home()
AUTH_DB = REAL_HOME / ".local/share/kiro-cli/data.sqlite3"

CAP_DIR = HERE / "captures"
CAP_DIR.mkdir(exist_ok=True)
CAP = open(CAP_DIR / "parallel-stdio.jsonl", "w")
LOGF = open(HERE / "parallel.log", "w")
REDACT_KEYS = {"accessToken", "access_token", "expiresAt", "expires_at", "profileArn", "profile_arn"}
T0 = time.time()


def log(*a):
    s = f"[{time.time() - T0:7.2f}s] " + " ".join(str(x) for x in a)
    print(s)
    LOGF.write(s + "\n")
    LOGF.flush()


def redact(o):
    if isinstance(o, dict):
        return {k: ("<redacted>" if k in REDACT_KEYS else redact(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [redact(x) for x in o]
    return o


def capture(direction, msg):
    CAP.write(json.dumps({"ts": time.time(), "dir": direction, "msg": redact(msg)}) + "\n")
    CAP.flush()


def read_token():
    c = sqlite3.connect(AUTH_DB)
    try:
        row = c.execute(
            "select value from auth_kv where key in "
            "('kirocli:odic:token','kirocli:social:token') order by key desc"
        ).fetchone()
        prow = c.execute("select value from state where key='api.codewhisperer.profile'").fetchone()
    finally:
        c.close()
    if not row:
        raise SystemExit("no kiro auth token — run kiro-cli login")
    v = row[0]
    v = v.decode() if isinstance(v, (bytes, bytearray)) else v
    d = json.loads(v)
    parn = d.get("profile_arn")
    if not parn and prow:
        pv = prow[0]
        pv = pv.decode() if isinstance(pv, (bytes, bytearray)) else pv
        try:
            parn = json.loads(pv).get("arn")
        except Exception:
            parn = None
    return {"accessToken": d["access_token"], "expiresAt": d["expires_at"], "profileArn": parn}


# ---------------------------------------------------------------- spawn
CWD = tempfile.mkdtemp(prefix="wf-parallel-")
subprocess.run(
    "git init -q -b main && git config user.email p@p && git config user.name p",
    cwd=CWD, shell=True, check=True,
)
TMPH = tempfile.mkdtemp(prefix="wf-parallel-home-")
ENV = dict(os.environ)
ENV["HOME"] = TMPH
ENV["XDG_DATA_HOME"] = str(REAL_HOME / ".local/share")

proc = subprocess.Popen(
    ["kiro-cli", "acp", "--agent-engine", "kas"],
    cwd=CWD, env=ENV, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True,
)
assert proc.stdin and proc.stdout
PIN, POUT = proc.stdin, proc.stdout
msgs = queue.Queue()
threading.Thread(
    target=lambda: ([msgs.put(l.strip()) for l in POUT if l.strip()], msgs.put(None)),
    daemon=True,
).start()
log(f"# spawned kiro-cli pid={proc.pid} cwd={CWD} home={TMPH}")

_id = [0]


def send(obj):
    capture("client->agent", obj)
    PIN.write(json.dumps(obj) + "\n")
    PIN.flush()


def req(m, p):
    _id[0] += 1
    send({"jsonrpc": "2.0", "id": _id[0], "method": m, "params": p})
    return _id[0]


def reply(rid, res):
    send({"jsonrpc": "2.0", "id": rid, "result": res})


# ---------------------------------------------------------------- observation
SESS = {}                # sessionId -> label
EV = {"A": {}, "B": {}, "C": {}}  # label -> {turn_start, first_chunk, last_chunk, turn_end, stop, text}
CHUNK_SEQ = []           # (t, label) per agent_message_chunk — interleave evidence
PROMPT_RIDS = {}         # rid -> label
PROMPT_RESP = {}         # label -> stopReason or ERROR...
ERRORS = []


def lab(p):
    return SESS.get(p.get("sessionId"))


def handle(o):
    m = o.get("method")
    rid = o.get("id")
    p = o.get("params", {}) or {}
    if rid is not None and m:
        if m == "_kiro/auth/getAccessToken":
            reply(rid, read_token())
        elif m == "_kiro/terminal/shell_type":
            reply(rid, {"shellType": "bash"})
        elif m == "session/request_permission":
            opts = p.get("options", [])
            pick = next(
                (x for x in opts if "allow" in (str(x.get("kind", "")) + str(x.get("optionId", ""))).lower()),
                opts[0] if opts else None,
            )
            reply(rid, {"outcome": {"outcome": "selected", "optionId": pick["optionId"]}} if pick
                  else {"outcome": {"outcome": "cancelled"}})
        else:
            reply(rid, {})
        return
    if not m or "session/update" not in m:
        return
    u = (p.get("update") or {}) if isinstance(p, dict) else {}
    if not isinstance(u, dict):
        return
    s = lab(p)
    if s is None:
        return
    t = time.time() - T0
    v = u.get("sessionUpdate")
    ev = EV[s]
    if v == "agent_message_chunk":
        ev.setdefault("first_chunk", t)
        ev["last_chunk"] = t
        ev["text"] = ev.get("text", "") + (u.get("content") or {}).get("text", "")
        CHUNK_SEQ.append((t, s))
    elif v == "session_info_update":
        meta = ((u.get("_meta") or {}).get("kiro") or {})
        kind = meta.get("kind")
        if kind == "turn_start":
            ev["turn_start"] = t
            log(f"  ~ {s}: turn_start")
        elif kind == "turn_end":
            ev["turn_end"] = t
            ev["stop"] = meta.get("stopReason")
            log(f"  ~ {s}: turn_end stopReason={meta.get('stopReason')!r}")


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            raw = msgs.get(timeout=0.5)
        except queue.Empty:
            continue
        if raw is None:
            log("# agent stdout closed")
            return False
        try:
            o = json.loads(raw)
        except Exception:
            continue
        capture("agent->client", o)
        if "method" in o:
            handle(o)
        elif o.get("id") in PROMPT_RIDS:
            s = PROMPT_RIDS[o["id"]]
            if "error" in o:
                PROMPT_RESP[s] = f"ERROR {json.dumps(o['error'])[:140]}"
            else:
                PROMPT_RESP[s] = (o.get("result") or {}).get("stopReason")
            log(f"  ~ {s}: prompt response -> {PROMPT_RESP[s]!r}")
        elif "error" in o:
            ERRORS.append(o["error"])
            log(f"  !! rpc error: {json.dumps(o['error'])[:160]}")
    return True


def call_sync(method, params, to=60):
    rid = req(method, params)
    end = time.time() + to
    while time.time() < end:
        try:
            raw = msgs.get(timeout=0.5)
        except queue.Empty:
            continue
        if raw is None:
            return None
        try:
            o = json.loads(raw)
        except Exception:
            continue
        capture("agent->client", o)
        if "method" in o:
            handle(o)
        elif o.get("id") == rid:
            return o
    return None


PROMPTS = {
    "A": ("Write the numbers one through four hundred as English words, one word per line, "
          "no skipping, no other commentary. On the final line write exactly: DONE-ALPHA"),
    "B": ("Write the NATO phonetic alphabet words for A through Z twelve times over, "
          "one word per line, no skipping, no other commentary. On the final line write exactly: DONE-BRAVO"),
    "C": ("Write the twelve month names twenty-five times over, one word per line, "
          "no skipping, no other commentary. On the final line write exactly: DONE-CHARLIE"),
}
SENTINEL = {"A": "DONE-ALPHA", "B": "DONE-BRAVO", "C": "DONE-CHARLIE"}
LABELS = list(PROMPTS)

# ---------------------------------------------------------------- run
call_sync("initialize", {
    "protocolVersion": 1,
    "clientInfo": {"name": "wayfinder-spike", "version": "0.1.0"},
    "clientCapabilities": {},
})
log("# initialized")

for label in LABELS:
    nr = call_sync("session/new", {"cwd": CWD, "mcpServers": []})
    sid = ((nr or {}).get("result") or {}).get("sessionId")
    if not sid:
        raise SystemExit(f"session/new {label} failed: {json.dumps(nr)[:300]}")
    SESS[sid] = label
    log(f"# session {label} = {sid}")

SID = {v: k for k, v in SESS.items()}

sent = []


def send_prompt(label):
    rid = req("session/prompt", {"sessionId": SID[label], "prompt": [{"type": "text", "text": PROMPTS[label]}]})
    PROMPT_RIDS[rid] = label
    sent.append(label)
    prev = sent[-2] if len(sent) > 1 else None
    log(f"# prompt {label} sent" + (f" (while {prev} mid-turn: first_chunk={'first_chunk' in EV[prev]})" if prev else ""))


send_prompt("A")
t_last = time.time()
deadline = time.time() + 300
while time.time() < deadline:
    if not pump(0.3):
        break
    if sent and len(sent) < len(LABELS):
        cur = sent[-1]
        if "first_chunk" in EV[cur] or time.time() - t_last > 5:
            send_prompt(LABELS[len(sent)])
            t_last = time.time()
    if len(sent) == len(LABELS) and all("turn_end" in EV[s] for s in LABELS) and len(PROMPT_RESP) == len(LABELS):
        pump(3)
        break

# ---------------------------------------------------------------- verdict
def fmt(s):
    e = EV[s]
    return (f"{s}: turn_start={e.get('turn_start')} first_chunk={e.get('first_chunk')} "
            f"last_chunk={e.get('last_chunk')} turn_end={e.get('turn_end')} stop={e.get('stop')!r}")


log("\n===== per-session timing =====")
for s in LABELS:
    log(" ", fmt(s))

interleaves = sum(1 for i in range(1, len(CHUNK_SEQ)) if CHUNK_SEQ[i][1] != CHUNK_SEQ[i - 1][1])
overlaps = []
for i, s2 in enumerate(LABELS[1:], 1):
    for s1 in LABELS[:i]:
        a, b = EV[s1], EV[s2]
        if b.get("turn_start") is not None and a.get("turn_end") is not None:
            overlaps.append((s1, s2, b["turn_start"] < a["turn_end"]))
latencies = {s: round(EV[s]["first_chunk"] - EV[s]["turn_start"], 2)
             for s in LABELS if "first_chunk" in EV[s] and "turn_start" in EV[s]}
ok = {s: SENTINEL[s] in EV[s].get("text", "") for s in LABELS}
crosstalk = any(SENTINEL[o] in EV[s].get("text", "") for s in LABELS for o in LABELS if o != s)
max_concurrent = 0
open_set = set()
events = sorted([(EV[s]["turn_start"], 1, s) for s in LABELS if "turn_start" in EV[s]]
                + [(EV[s]["turn_end"], -1, s) for s in LABELS if "turn_end" in EV[s]])
for _, d, s in events:
    open_set.add(s) if d == 1 else open_set.discard(s)
    max_concurrent = max(max_concurrent, len(open_set))

log("\n===== VERDICT =====")
log(f"  pairwise overlap (later turn_start < earlier turn_end): {overlaps}")
log(f"  max simultaneously-open turns: {max_concurrent} of {len(LABELS)}")
log(f"  chunk interleaves: {interleaves} of {len(CHUNK_SEQ)} chunks")
log(f"  first-token latency per session (s): {latencies}")
log(f"  prompt responses: {PROMPT_RESP}")
log(f"  content correct: {ok}  cross-talk={crosstalk}")
log(f"  rpc errors: {[json.dumps(e)[:120] for e in ERRORS] or '(none)'}")
if max_concurrent == len(LABELS) and interleaves > 3 and all(ok.values()) and not crosstalk:
    log(f"  ==> PARALLEL x{len(LABELS)}: all turns simultaneously open, streams interleaved, clean routing")
elif any(str(PROMPT_RESP.get(s, "")).startswith("ERROR") for s in LABELS):
    log("  ==> REJECTED for at least one session")
else:
    log("  ==> inspect: weaker than full parallel")

PIN.close()
proc.terminate()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
try:
    os.killpg(proc.pid, signal.SIGTERM)
except (ProcessLookupError, PermissionError):
    pass
log(f"\n# capture: {CAP_DIR / 'parallel-stdio.jsonl'}")
