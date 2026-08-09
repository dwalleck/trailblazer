#!/usr/bin/env python3
"""
Spike: native `_kiro/userInput` question cards + their turn-end mechanics.

Arms A-D (reachability, executed 2026-08-09 — see docs/spike-user-input.md):
  A  control: gate ON, default vibe agent                       (not run; covered by B)
  B  gate ON + customAgents[wayfinder-interviewer] + modeId     -> FAIL (vendor-pinned)
  C  gate OFF + customAgents + modeId                           (not run; bundle-static)
  D  gate ON + agent dispatched as DELEGATED subagent stage     -> PASS (full round-trip)

Arms E-G (turn-end mechanics, the trailblazer present-inject counterpart):
  E  D + park the answer --park seconds (default 600, T10 twin):
       does the turn stay open? anything time out? then answer -> resume?
  F  D + never answer; session/cancel 15s after the question arrives:
       teardown semantics (turn_end stopReason, interaction_resolved, late reply)
  G  D + never answer; SIGKILL the process group, respawn, session/load (T8 twin):
       is pending_interaction replayed? does _kiro/userInput re-fire? can a fresh
       prompt deliver the answer to the reloaded session?

Bundle statics these arms verify live (2.16.1 acp-server.js):
  - withPersistedUserInput persists a `pending_interaction` transcript message
    (full question+options) BEFORE sending _kiro/userInput, emits it live as a
    session_info_update, and `await execute()` has NO timeout; an
    `interaction_resolved` message follows the answer.

Auth: fresh token from data.sqlite3 on EVERY _kiro/auth/getAccessToken callback.
HOME-isolated spawn (HOME=<tmp>, XDG_DATA_HOME real). Capture: every frame both
directions as {ts, dir, msg} JSONL, auth redacted.
"""

import argparse
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

AP = argparse.ArgumentParser()
AP.add_argument("--arm", required=True, choices=["A", "B", "C", "D", "E", "F", "G"])
AP.add_argument("--kiro", default="kiro-cli")
AP.add_argument("--park", type=int, default=600, help="arm E: seconds to hold the answer")
AP.add_argument("--turn-timeout", type=int, default=300)
ARGS = AP.parse_args()

CAP_DIR = HERE / "captures"
CAP_DIR.mkdir(exist_ok=True)
CAP = open(CAP_DIR / f"{ARGS.arm}-stdio.jsonl", "w")
LOGF = open(HERE / f"arm-{ARGS.arm}.log", "w")

REDACT_KEYS = {"accessToken", "access_token", "expiresAt", "expires_at", "profileArn", "profile_arn"}
T0 = time.time()


def log(*a):
    s = f"[{time.time() - T0:7.1f}s] " + " ".join(str(x) for x in a)
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
    """Fresh read per callback — never cache (180s refresh buffer)."""
    c = sqlite3.connect(AUTH_DB)
    try:
        row = c.execute(
            "select value from auth_kv where key in "
            "('kirocli:odic:token','kirocli:social:token') order by key desc"
        ).fetchone()
        prow = c.execute(
            "select value from state where key='api.codewhisperer.profile'"
        ).fetchone()
    finally:
        c.close()
    if not row:
        raise SystemExit("no kiro auth token in data.sqlite3 — run kiro-cli login")
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


# ---------------------------------------------------------------- arm config
AGENT_ID = "wayfinder-interviewer"
CLIENT_AGENT = {
    "id": AGENT_ID,
    "description": "Interviews the user with structured questions before acting.",
    "prompt": (
        "You are the Wayfinder interviewer. Before acting on any request, you gather the "
        "user's decisions. When you need to ask the user a question, you MUST use the "
        "user_input tool — do NOT write questions as plain chat text. Offer concrete "
        "options and mark one as recommended. After the user answers, restate their "
        "choice in one short sentence and stop."
    ),
    "tools": ["user_input", "fs_read"],
    "permissions": {"rules": [{"capability": "fs_read", "match": ["./**"], "effect": "allow"}]},
}

GATE_ON = ARGS.arm in ("A", "B", "D", "E", "F", "G")
INJECT = ARGS.arm in ("B", "C", "D", "E", "F", "G")
AS_MODE = ARGS.arm in ("B", "C")
DELEGATED = ARGS.arm in ("D", "E", "F", "G")
# what to do when _kiro/userInput arrives
UI_MODE = {"E": "park", "F": "hold", "G": "hold"}.get(ARGS.arm, "answer")

INIT_KIRO = {}
if GATE_ON:
    INIT_KIRO["userInput"] = True
if DELEGATED:
    INIT_KIRO["settings"] = {"subagentOrchestration": {"enabled": True}}
INIT_META = {"kiro": INIT_KIRO}

NEW_KIRO = {}
if INJECT:
    NEW_KIRO["customAgents"] = [CLIENT_AGENT]
    if AS_MODE:
        NEW_KIRO["modeId"] = AGENT_ID
NEW_META = {"kiro": NEW_KIRO}

if DELEGATED:
    PROMPT = (
        "Dispatch the registered agent 'wayfinder-interviewer' as a sub-agent (a single "
        "stage/task) with this task: ask the user which storage backend the project should "
        "use — options 'sqlite' (recommended), 'postgres', 'jsonl' — wait for their answer, "
        "and report the chosen backend back. When the sub-agent reports, restate the chosen "
        "backend in one sentence and finish."
    )
else:
    PROMPT = (
        "I want to start a small project. Before doing anything else, ask me one structured "
        "question using your user_input tool: which storage backend should the project use — "
        "options 'sqlite' (recommended), 'postgres', 'jsonl'. After I answer, restate my "
        "choice in one sentence and finish."
    )

# ---------------------------------------------------------------- process mgmt
CWD = tempfile.mkdtemp(prefix=f"wf-userinput-{ARGS.arm}-")
subprocess.run(
    "git init -q -b main && git config user.email p@p && git config user.name p",
    cwd=CWD, shell=True, check=True,
)
TMPH = tempfile.mkdtemp(prefix="wf-userinput-home-")
ENV = dict(os.environ)
ENV["HOME"] = TMPH                                    # session store lives here — reused on respawn
ENV["XDG_DATA_HOME"] = str(REAL_HOME / ".local/share")

proc = None
PIN = POUT = None
msgs = queue.Queue()


def spawn():
    global proc, PIN, POUT
    proc = subprocess.Popen(
        [ARGS.kiro, "acp", "--agent-engine", "kas"],
        cwd=CWD, env=ENV, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
        start_new_session=True,
    )
    assert proc.stdin and proc.stdout
    PIN, POUT = proc.stdin, proc.stdout
    out = POUT
    threading.Thread(
        target=lambda: ([msgs.put(l.strip()) for l in out if l.strip()], msgs.put(None)),
        daemon=True,
    ).start()
    log(f"# spawned kiro-cli pid={proc.pid}")


def hard_kill():
    log(f"# SIGKILL process group of pid={proc.pid}")
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()
    # drain reader sentinel so later pumps see fresh frames only
    while True:
        try:
            if msgs.get(timeout=2) is None:
                break
        except queue.Empty:
            break


log(f"# arm={ARGS.arm} gate={GATE_ON} inject={INJECT} as_mode={AS_MODE} delegated={DELEGATED} "
    f"ui_mode={UI_MODE} park={ARGS.park if ARGS.arm == 'E' else '-'} cwd={CWD} home={TMPH}")
spawn()

_id = [0]


def send(obj):
    capture("client->agent", obj)
    PIN.write(json.dumps(obj) + "\n")
    PIN.flush()


def req(m, p):
    _id[0] += 1
    send({"jsonrpc": "2.0", "id": _id[0], "method": m, "params": p})
    return _id[0]


def notify(m, p):
    send({"jsonrpc": "2.0", "method": m, "params": p})


def reply(rid, res):
    send({"jsonrpc": "2.0", "id": rid, "result": res})


# ---------------------------------------------------------------- observation
USERINPUTS = []          # (rid, params, arrival_time) — replied per UI_MODE
PERMS = []
AGENT_TEXT = []
TIMELINE = []            # (t_rel, tag, detail)
ERRORS = []
TURN_ENDS = [0]
PROMPT_RIDS = {}         # rid -> label; responses recorded on arrival
PROMPT_RESPONSES = []    # (label, stopReason/error)
REPLAYS = [0]
PENDING_INTERACTIONS = []
ANSWER = "sqlite"


def mark(tag, detail=""):
    TIMELINE.append((round(time.time() - T0, 1), tag, detail))
    log(f"  ~ {tag} {detail}"[:240])


def handle(o):
    m = o.get("method")
    rid = o.get("id")
    p = o.get("params", {}) or {}
    if rid is not None and m:
        if m == "_kiro/auth/getAccessToken":
            reply(rid, read_token())
            mark("auth_callback")
        elif m == "_kiro/terminal/shell_type":
            reply(rid, {"shellType": "bash"})
        elif m == "_kiro/userInput":
            USERINPUTS.append((rid, p, time.time()))
            mark("userInput_request", f"rid={rid} Q={json.dumps(p.get('question'))[:100]}")
            if UI_MODE == "answer":
                reply(rid, {"action": "answered", "answer": ANSWER})
                mark("userInput_answered", ANSWER)
        elif m == "session/request_permission":
            PERMS.append(p)
            title = (p.get("toolCall") or {}).get("title")
            mark("permission_request", repr(title))
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
    if not m:
        return
    if "session/update" in m:
        u = (p.get("update") or {}) if isinstance(p, dict) else {}
        if not isinstance(u, dict):
            return
        meta_kiro = ((u.get("_meta") or {}).get("kiro") or {})
        if meta_kiro.get("replay"):
            REPLAYS[0] += 1
        v = u.get("sessionUpdate")
        if v == "agent_message_chunk":
            AGENT_TEXT.append((u.get("content") or {}).get("text", ""))
        elif v in ("tool_call", "tool_call_update"):
            mark("tool", f"{v} {u.get('title') or u.get('toolCallId')} -> {u.get('status')}"
                 + (" [replay]" if meta_kiro.get("replay") else ""))
        elif v == "session_info_update":
            kind = meta_kiro.get("kind")
            if kind == "turn_end":
                TURN_ENDS[0] += 1
                mark("turn_end", json.dumps({k: meta_kiro.get(k) for k in ("stopReason", "replay") if k in meta_kiro}))
            elif kind in ("pending_interaction", "interaction_resolved"):
                PENDING_INTERACTIONS.append((kind, meta_kiro))
                mark(kind, json.dumps(meta_kiro)[:220])
            elif kind in ("turn_start", "turn_completion"):
                mark(kind, "[replay]" if meta_kiro.get("replay") else "")


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            raw = msgs.get(timeout=1)
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
            label = PROMPT_RIDS[o["id"]]
            if "error" in o:
                PROMPT_RESPONSES.append((label, f"ERROR {json.dumps(o['error'])[:120]}"))
            else:
                PROMPT_RESPONSES.append((label, json.dumps((o.get("result") or {}).get("stopReason"))))
            mark("prompt_response", f"{label}: {PROMPT_RESPONSES[-1][1]}")
        elif "error" in o:
            ERRORS.append(o["error"])
            mark("rpc_error", json.dumps(o["error"])[:160])
    return True


def call_sync(method, params, to=60):
    rid = req(method, params)
    end = time.time() + to
    while time.time() < end:
        try:
            raw = msgs.get(timeout=1)
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
            if "error" in o:
                ERRORS.append(o["error"])
                mark("rpc_error", f"{method}: {json.dumps(o['error'])[:160]}")
            return o
        elif o.get("id") in PROMPT_RIDS:
            label = PROMPT_RIDS[o["id"]]
            PROMPT_RESPONSES.append((label, json.dumps((o.get("result") or {}).get("stopReason"))
                                     if "result" in o else f"ERROR {json.dumps(o.get('error'))[:120]}"))
            mark("prompt_response", f"{label}: {PROMPT_RESPONSES[-1][1]}")
    return None


def send_prompt(sid, text, label):
    rid = req("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]})
    PROMPT_RIDS[rid] = label
    return rid


def extract_mode(container):
    opt = next((c for c in (container.get("configOptions") or []) if c.get("id") == "mode"), None)
    cur = (opt or {}).get("currentValue")
    values = [v.get("value") if isinstance(v, dict) else v for v in ((opt or {}).get("options") or [])]
    return cur, values


# ---------------------------------------------------------------- run
ir = call_sync("initialize", {"protocolVersion": 1, "clientCapabilities": {"_meta": INIT_META}})
log("# initialized:", bool(ir and "result" in ir))

nr = call_sync("session/new", {"cwd": CWD, "mcpServers": [], "_meta": NEW_META})
res = (nr.get("result") or {}) if nr and "result" in nr else {}
sid = res.get("sessionId")
cur_mode = (res.get("_meta") or {}).get("agentMode")
log(f"# sessionId: {sid}  agentMode={cur_mode!r}")

if AS_MODE and cur_mode != AGENT_ID and sid:
    sr = call_sync("session/set_config_option", {"sessionId": sid, "configId": "mode", "value": AGENT_ID})
    if sr and "result" in sr:
        cur_mode, _ = extract_mode(sr["result"] or {})
        log(f"# after set_config_option: mode={cur_mode!r}")

before = TURN_ENDS[0]
send_prompt(sid, PROMPT, "turn1")
mark("prompt_sent", "turn1")

acted = [False]          # arm-specific action performed
late_reply_done = [False]

deadline = time.time() + ARGS.turn_timeout + (ARGS.park if ARGS.arm == "E" else 0)
while time.time() < deadline:
    alive = pump(2)
    now = time.time()
    pending = USERINPUTS[-1] if USERINPUTS else None

    if ARGS.arm == "E" and pending and not acted[0] and now - pending[2] >= ARGS.park:
        turn_ended_during_park = TURN_ENDS[0] > before
        mark("park_over", f"parked={round(now - pending[2])}s turn_ended_during_park={turn_ended_during_park}")
        reply(pending[0], {"action": "answered", "answer": ANSWER})
        mark("userInput_answered_late", ANSWER)
        acted[0] = True

    if ARGS.arm == "F" and pending and not acted[0] and now - pending[2] >= 15:
        mark("sending_session_cancel")
        notify("session/cancel", {"sessionId": sid})
        acted[0] = True

    if ARGS.arm == "F" and acted[0] and not late_reply_done[0] and TURN_ENDS[0] > before:
        pump(5)
        mark("late_reply_to_stale_rid", f"rid={pending[0]}")
        reply(pending[0], {"action": "answered", "answer": ANSWER})
        late_reply_done[0] = True
        pump(10)
        break

    if ARGS.arm == "G" and pending and not acted[0] and now - pending[2] >= 5:
        acted[0] = True
        hard_kill()
        mark("respawning")
        spawn()
        ir2 = call_sync("initialize", {"protocolVersion": 1, "clientCapabilities": {"_meta": INIT_META}})
        mark("reinitialized", str(bool(ir2 and "result" in ir2)))
        lr = call_sync("session/load", {"sessionId": sid, "cwd": CWD, "mcpServers": [], "_meta": NEW_META}, to=90)
        ok = bool(lr and "result" in lr)
        mark("session_load", f"ok={ok} replayed_so_far={REPLAYS[0]}")
        pump(15)
        refired = [u for u in USERINPUTS if u[2] > pending[2]]
        mark("post_load_state",
             f"replay_frames={REPLAYS[0]} userInput_refired={bool(refired)} "
             f"pending_interaction_msgs={[k for k, _ in PENDING_INTERACTIONS]}")
        if refired:
            reply(refired[-1][0], {"action": "answered", "answer": ANSWER})
            mark("userInput_answered_after_reload", ANSWER)
        else:
            before = TURN_ENDS[0]
            send_prompt(sid, f"Earlier you asked which storage backend to use; my answer is {ANSWER}. "
                             "Confirm my choice in one sentence.", "post-reload-turn")
            mark("prompt_sent", "post-reload-turn")
        continue

    if TURN_ENDS[0] > before and acted[0] and ARGS.arm in ("E", "G"):
        pump(4)
        break
    if ARGS.arm == "D" and TURN_ENDS[0] > before:
        pump(4)
        break
    if not alive and ARGS.arm != "G":
        break

# ---------------------------------------------------------------- verdict
text = "".join(AGENT_TEXT)
log("\n===== TIMELINE =====")
for t, tag, detail in TIMELINE:
    log(f"  {t:7.1f}s  {tag:24s} {detail[:180]}")
log("\n===== VERDICT =====")
log(f"  arm: {ARGS.arm}  mode_at_prompt={cur_mode!r}")
log(f"  _kiro/userInput fired: {len(USERINPUTS)}x")
log(f"  prompt responses: {PROMPT_RESPONSES}")
log(f"  pending/resolved session_info kinds: {[k for k, _ in PENDING_INTERACTIONS]}")
log(f"  replayed frames: {REPLAYS[0]}")
log(f"  rpc errors: {[json.dumps(e)[:120] for e in ERRORS] or '(none)'}")
log(f"  answer round-trip (agent restated {ANSWER!r}): {ANSWER.lower() in text.lower()}")
log("\n===== agent text (head) =====")
log(text[:800] or "(none)")

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
log(f"\n# capture: {CAP_DIR / (ARGS.arm + '-stdio.jsonl')}")
