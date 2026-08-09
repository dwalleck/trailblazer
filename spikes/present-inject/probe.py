#!/usr/bin/env python3
"""Present/inject spike driver (wayfinder/docs/spike-present-inject.md).

Spawns kiro-cli on the KAS ACP lane, registers the probe MCP server, runs one
test scenario (T1..T10), and captures every frame both directions as JSONL.

Harness adapted from cyril/experiments/conductor-spike/probe-kas-custom-dag-live-2.16.0.py.

    uv run --no-project probe.py --test T2 [--transport stdio|http] [--delay 2]
        [--envelope json|markdown|lines] [--kiro PATH] [--out captures/T2.jsonl]

COSTS CREDITS: every scenario drives real model turns. Prompts forbid file
tools; the working dir is a throwaway temp dir.
"""
import argparse
import glob
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER = os.path.join(HERE, "mcp_server.py")

AP = argparse.ArgumentParser()
AP.add_argument("--test", required=True)
AP.add_argument("--transport", default="stdio", choices=["stdio", "http"])
AP.add_argument("--delay", type=float, default=2.0)
AP.add_argument("--envelope", default="lines", choices=["json", "markdown", "lines"])
AP.add_argument("--kiro", default=os.path.expandvars(r"%LOCALAPPDATA%\Kiro-Cli\kiro-cli.exe"))
AP.add_argument("--out", default="")
AP.add_argument("--http-port", type=int, default=7899)
ARGS = AP.parse_args()

OUT_PATH = ARGS.out or os.path.join(HERE, "captures", f"{ARGS.test}-{ARGS.transport}.jsonl")
os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
OUT = open(OUT_PATH, "w", encoding="utf-8")

QUEUE_DIR = tempfile.mkdtemp(prefix="wf-queue-")
EVENTS = []  # (ts, kind, summary) — human-readable timeline
AGENT_TEXT = []  # accumulated agent_message_chunk text per turn


SENSITIVE_KEYS = {
    "accessToken", "access_token", "refreshToken", "refresh_token",
    "idToken", "id_token", "token", "authorization", "clientSecret", "profileArn",
}


def scrub(frame):
    """Deep-copy a frame with auth material redacted. Captures must never
    retain live tokens; the real frame still goes on the wire untouched."""
    if isinstance(frame, dict):
        return {
            k: ("[redacted]" if k in SENSITIVE_KEYS else scrub(v))
            for k, v in frame.items()
        }
    if isinstance(frame, list):
        return [scrub(v) for v in frame]
    return frame


def emit(direction, frame):
    OUT.write(json.dumps({"ts": time.time(), "dir": direction, "frame": scrub(frame)}) + "\n")
    OUT.flush()


def event(kind, summary):
    EVENTS.append((time.time(), kind, summary))
    print(f"  [{time.strftime('%H:%M:%S')}] {kind}: {summary}", flush=True)


def read_token():
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\kiro-cli\data.sqlite3"),
        os.path.expanduser("~/.local/share/kiro-cli/data.sqlite3"),
    ]
    db = next((c for c in candidates if os.path.exists(c)), None)
    if not db:
        raise SystemExit("kiro-cli data.sqlite3 not found")
    c = sqlite3.connect(db)
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
    if row is None:
        raise SystemExit("logged out — no token")
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
            pass
    return {"accessToken": d["access_token"], "expiresAt": d["expires_at"], "profileArn": parn}


TOK = read_token()
CWD = tempfile.mkdtemp(prefix="wf-spike-")
try:
    subprocess.run(
        "git init -q -b main && git config user.email p@p && git config user.name p",
        cwd=CWD, shell=True, timeout=15,
    )
except Exception as e:
    event("warn", f"git init failed ({e}); continuing without a repo")

# HOME isolation, cyril methodology (feedback_isolate_kiro_probes_with_home).
# Best-effort on Windows: kiro-cli may resolve its data dir via known-folder
# APIs instead of HOME; the token callback below makes auth work regardless.
TMPH = tempfile.mkdtemp(prefix="wf-spikehome-")
env = dict(os.environ)
env["HOME"] = TMPH

p = subprocess.Popen(
    [ARGS.kiro, "acp", "--agent-engine", "kas"],
    cwd=CWD, env=env,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, text=True, bufsize=1,
)
q: queue.Queue = queue.Queue()
assert p.stdout is not None and p.stdin is not None
threading.Thread(
    target=lambda: [q.put(l.strip()) for l in p.stdout if l.strip()], daemon=True
).start()

_id = [0]
TURN_ACTIVE = [False]  # True between session/prompt send and its response


def req(method, params):
    _id[0] += 1
    frame = {"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params}
    emit("out", frame)
    p.stdin.write(json.dumps(frame) + "\n")
    p.stdin.flush()
    return _id[0]


def notify(method, params):
    frame = {"jsonrpc": "2.0", "method": method, "params": params}
    emit("out", frame)
    p.stdin.write(json.dumps(frame) + "\n")
    p.stdin.flush()


def rep(rid, result):
    frame = {"jsonrpc": "2.0", "id": rid, "result": result}
    emit("out", frame)
    p.stdin.write(json.dumps(frame) + "\n")
    p.stdin.flush()


def handle_server_request(m, rid, pr):
    if m == "_kiro/auth/getAccessToken":
        # Re-read on every request: the on-disk token is refreshed out of band
        # by other kiro-cli processes, and a cached copy goes stale inside the
        # host's 180s refresh buffer on any session that sits idle (T3).
        rep(rid, read_token())
    elif m == "_kiro/terminal/shell_type":
        rep(rid, {"shellType": "bash"})
    elif m == "session/request_permission":
        opts = pr.get("options", [])
        pick = next(
            (x for x in opts if "allow" in (x.get("kind", "") + x.get("optionId", "")).lower()),
            opts[0] if opts else None,
        )
        event("permission", f"{pr.get('toolCall', {}).get('title', '?')} -> {pick}")
        rep(
            rid,
            {"outcome": {"outcome": "selected", "optionId": pick["optionId"]}}
            if pick
            else {"outcome": {"outcome": "cancelled"}},
        )
    else:
        rep(rid, {})


RESP = {}  # response frames stashed by id — a pump waiting on rid2 must not
           # discard rid1's response (T6's original correlation bug)


def pump(until=None, timeout=60, stop_pred=None):
    """Pump frames until the response to `until` arrives, stop_pred fires, or
    timeout. Returns the matching response frame, the stop_pred frame, or None.
    Unmatched responses are stashed in RESP for later pumps."""
    if until is not None and until in RESP:
        return RESP.pop(until)
    end = time.time() + timeout
    while time.time() < end:
        try:
            raw = q.get(timeout=2)
        except queue.Empty:
            continue
        try:
            o = json.loads(raw)
        except Exception:
            continue
        emit("in", o)
        m, rid, pr = o.get("method"), o.get("id"), o.get("params") or {}

        if m == "session/update":
            upd = pr.get("update", {})
            kind = upd.get("sessionUpdate")
            if kind == "agent_message_chunk":
                AGENT_TEXT.append(upd.get("content", {}).get("text", ""))
            elif kind == "tool_call":
                event("tool_call", f"{upd.get('title')} [{upd.get('status')}]")
            elif kind == "tool_call_update":
                event("tool_call_update", f"{upd.get('toolCallId')} -> {upd.get('status')}")
        if m and rid is not None:
            handle_server_request(m, rid, pr)
            continue
        if stop_pred and stop_pred(o):
            return o
        if rid is not None and not m and ("result" in o or "error" in o):
            if until is not None and rid == until:
                return o
            RESP[rid] = o  # a response we weren't waiting on — keep it
    return None


def prompt(sid, text, timeout=300):
    """Send a user turn and wait for the turn to complete (the session/prompt
    response IS turn end in ACP — it carries stopReason)."""
    TURN_ACTIVE[0] = True
    rid = req("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]})
    event("prompt", text[:120].replace("\n", " "))
    AGENT_TEXT.clear()
    resp = pump(rid, timeout)
    TURN_ACTIVE[0] = False
    stop = (resp or {}).get("result", {}).get("stopReason")
    err = (resp or {}).get("error")
    event("turn_end", f"stopReason={stop} error={err}")
    return resp


def mcp_server_spec():
    if ARGS.transport == "stdio":
        return {
            "name": "wayfinder-probe",
            "command": "uv",
            "args": ["run", "--no-project", "python", MCP_SERVER],
            "env": [
                {"name": "PROBE_QUEUE_DIR", "value": QUEUE_DIR},
                {"name": "PROBE_BLOCKING", "value": "1" if ARGS.test == "T10" else "0"},
                {"name": "PROBE_BLOCK_TIMEOUT", "value": str(max(ARGS.delay * 2, 660))},
            ],
        }
    return {
        "name": "wayfinder-probe",
        "type": "http",
        "url": f"http://127.0.0.1:{ARGS.http_port}/mcp",
        "headers": [],
    }


def new_session():
    iid = req(
        "initialize",
        {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
            "_meta": {"kiro": {"clientName": "wayfinder-spike", "checkpoints": True}},
        },
    )
    init_resp = pump(iid, 30)
    if not init_resp or "result" not in init_resp:
        event("fatal", "initialize did not complete")
        shutdown(1)
    if ARGS.transport == "http":
        # run the HTTP MCP server ourselves; kiro connects to it
        subprocess.Popen(
            ["uv", "run", "--no-project", "python", MCP_SERVER,
             "--http", str(ARGS.http_port)],
            env={**os.environ, "PROBE_QUEUE_DIR": QUEUE_DIR},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
    nid = req(
        "session/new",
        {
            "cwd": CWD,
            "mcpServers": [mcp_server_spec()],
            "_meta": {"kiro": {"settings": {"workflows": {"enabled": True}}}},
        },
    )
    sess = pump(nid, 90)
    sid = (sess or {}).get("result", {}).get("sessionId")
    if not sid:
        event("fatal", f"session/new failed: {json.dumps(sess)[:400]}")
        shutdown(1)
    event("session", f"sessionId={sid} transport={ARGS.transport} queue={QUEUE_DIR}")
    pump(-999, 5)  # drain session-start pushes
    return sid


def pending_rounds(seen):
    out = []
    for f in sorted(glob.glob(os.path.join(QUEUE_DIR, "round-*.json"))):
        rid = os.path.basename(f)[len("round-"):-len(".json")]
        if rid not in seen:
            with open(f, encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


def wait_for_round(seen, timeout=240, also=None):
    """Pump while polling for a new presented round. Returns the round record.
    `also` is an optional callable invoked each poll cycle (T6 uses it)."""
    end = time.time() + timeout
    while time.time() < end:
        rounds = pending_rounds(seen)
        if rounds:
            r = rounds[0]
            seen.add(r["round_id"])
            event("presented", f"round {r['round_id']}: "
                  + "; ".join(q_.get("id", "?") for q_ in r["questions"]))
            return r
        pump(-999, 2)  # keep servicing frames while we wait
        if also:
            also()
    event("fail", "no round presented in time")
    return None


def format_answers(round_rec, fmt):
    answers = [
        (q_["id"], q_.get("recommended") or q_["options"][0]) for q_ in round_rec["questions"]
    ]
    rid = round_rec["round_id"]
    if fmt == "json":
        body = json.dumps(
            {"round_id": rid, "answers": [{"id": i, "answer": a} for i, a in answers]},
            indent=2,
        )
        return f"[wayfinder answers for round {rid}]\n```json\n{body}\n```\n"
    if fmt == "markdown":
        lines = "\n".join(f"{n}. **{i}** — {a}" for n, (i, a) in enumerate(answers, 1))
        return f"[wayfinder answers for round {rid}]\n{lines}\n"
    lines = "\n".join(f"{i}: {a}" for i, a in answers)
    return f"[wayfinder answers for round {rid}]\n{lines}\n"


PRESENT_INSTRUCTION = (
    "You are helping plan a demo service. You have an MCP tool "
    "present_questions from the server 'wayfinder-probe'. Use it NOW to ask "
    "the operator exactly 3 decisions: (1) deploy-target with options "
    "[aws-lambda, ecs-fargate, fly-io], (2) db with options [sqlite, postgres, "
    "dynamodb], (3) auth with options [none, api-key, oauth]. Give each your "
    "recommended option. Do not ask anything in plain text."
)

CONFIRM_INSTRUCTION = (
    "The block above contains the operator's answers. Reply with exactly one "
    "line per decision, formatted 'id: choice', and nothing else."
)


def t1(sid):
    resp = prompt(sid, "Call the get_map tool from wayfinder-probe and tell me the destination.", 180)
    ok = resp and "result" in resp
    event("verdict", f"T1 {ARGS.transport}: {'PASS' if ok else 'FAIL'}")


def t2(sid):
    seen = set()
    t0 = time.time()
    TURN_ACTIVE[0] = True
    rid = req("session/prompt", {"sessionId": sid,
              "prompt": [{"type": "text", "text": PRESENT_INSTRUCTION}]})
    rnd = wait_for_round(seen)
    if not rnd:
        return event("verdict", "T2: FAIL (no round)")
    t_presented = time.time()
    time.sleep(ARGS.delay)
    resp = pump(rid, 240)  # turn 1 ends when?
    t_end = time.time()
    event("timing", f"tool-result->turn_end = {t_end - t_presented - ARGS.delay:.1f}s "
                    f"(total turn {t_end - t0:.1f}s)")
    injected = format_answers(rnd, ARGS.envelope) + CONFIRM_INSTRUCTION
    prompt(sid, injected, 240)
    text = "".join(AGENT_TEXT)
    hits = sum(1 for q_ in rnd["questions"] if q_["id"] in text)
    event("verdict", f"T2: turn ended after present; injection accepted; "
                     f"answers referenced {hits}/{len(rnd['questions'])}")


def t3(sid):
    seen = set()
    rid = req("session/prompt", {"sessionId": sid,
              "prompt": [{"type": "text", "text": PRESENT_INSTRUCTION}]})
    rnd = wait_for_round(seen)
    if not rnd:
        return event("verdict", "T3: FAIL (no round)")
    event("wait", f"delaying answer {ARGS.delay}s")
    end = time.time() + ARGS.delay
    turn1_done = False
    while time.time() < end:
        resp = pump(None if turn1_done else rid, min(5.0, end - time.time()))
        if resp is not None and not turn1_done:
            turn1_done = True
            event("turn1_end", "turn 1 ended early; holding answer until delay elapses")
    prompt(sid, format_answers(rnd, ARGS.envelope) + CONFIRM_INSTRUCTION, 240)
    text = "".join(AGENT_TEXT)
    hits = sum(1 for q_ in rnd["questions"] if q_["id"] in text)
    event("verdict", f"T3: {ARGS.delay}s-delayed injection; answers referenced "
                     f"{hits}/{len(rnd['questions'])}")


def t4(sid):
    seen = set()
    rid = req("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": (
        PRESENT_INSTRUCTION
        + " While you wait for the answers, also start drafting the deployment "
          "plan document, making your best guesses where the answers would matter."
    )}]})
    rnd = wait_for_round(seen)
    if not rnd:
        return event("verdict", "T4: FAIL (no round)")
    pump(rid, 240)
    text = "".join(AGENT_TEXT)
    fabricated = any(q_["id"] in text for q_ in rnd["questions"])
    event("verdict", f"T4: agent {'CONTINUED and referenced the decision ids (fabrication risk)' if fabricated else 'did not fabricate answers in visible text'}; see capture")


def t5(sid):
    seen = set()
    rid = req("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": (
        PRESENT_INSTRUCTION
        + " THEN, still in this same turn, call present_questions a SECOND time "
          "with 1 more question: monitoring with options [cloudwatch, datadog], "
          "with your recommendation."
    )}]})
    r1 = wait_for_round(seen)
    r2 = wait_for_round(seen, timeout=120)
    pump(rid, 240)
    if r1 and r2:
        for r in (r1, r2):
            prompt(sid, format_answers(r, ARGS.envelope) + CONFIRM_INSTRUCTION, 240)
        event("verdict", "T5: two rounds in one turn presented and answered")
    else:
        event("verdict", f"T5: rounds presented = {bool(r1)},{bool(r2)} (queue/supersede decision needed)")


def t6(sid):
    seen = set()
    rid1 = req("session/prompt", {"sessionId": sid,
               "prompt": [{"type": "text", "text": PRESENT_INSTRUCTION}]})
    rnd = wait_for_round(seen)
    if not rnd:
        return event("verdict", "T6: FAIL (no round)")
    # inject WHILE turn 1 is (probably) still running — do NOT wait for rid1
    rid2 = req("session/prompt", {"sessionId": sid, "prompt": [
        {"type": "text", "text": format_answers(rnd, ARGS.envelope) + CONFIRM_INSTRUCTION}]})
    event("inject", f"mid-turn injection sent (rid {rid2})")
    resp2 = pump(rid2, 120)
    resp1 = pump(rid1, 120)
    event("verdict", f"T6: mid-turn prompt response={json.dumps((resp2 or {}).get('error') or (resp2 or {}).get('result'))[:200]} "
                     f"| first-turn response={json.dumps((resp1 or {}).get('error') or (resp1 or {}).get('result'))[:200]}")


def t7(sid):
    seen = set()
    rid = req("session/prompt", {"sessionId": sid,
              "prompt": [{"type": "text", "text": PRESENT_INSTRUCTION}]})
    rnd = wait_for_round(seen)
    pump(rid, 240)
    if not rnd:
        return event("verdict", "T7: FAIL (no round)")
    notify("_kiro/session/notify", {
        "sessionId": sid,
        "callerSessionId": "wayfinder-spike",
        "message": format_answers(rnd, ARGS.envelope),
        "severity": "success",
    })
    event("inject", "sent _kiro/session/notify with the answers")
    pump(-999, 10)
    prompt(sid, "If any steering or follow-up message just arrived for you, act on it: "
                "reply with one line per decision, 'id: choice'. If nothing arrived, say NONE.", 180)
    text = "".join(AGENT_TEXT)
    hits = sum(1 for q_ in rnd["questions"] if q_["id"] in text)
    event("verdict", f"T7: steering-buffer route — answers referenced {hits}/{len(rnd['questions'])}"
                     f"{' (NONE = route dead)' if 'NONE' in text else ''}")


def t8(sid):
    global p
    seen = set()
    rid = req("session/prompt", {"sessionId": sid,
              "prompt": [{"type": "text", "text": PRESENT_INSTRUCTION}]})
    rnd = wait_for_round(seen)
    if not rnd:
        return event("verdict", "T8: FAIL (no round)")
    pump(rid, 120)
    event("kill", f"killing kiro-cli with round {rnd['round_id']} pending")
    p.kill()
    time.sleep(2)
    alive = p.poll() is None
    event("kill", f"process dead={not alive}")

    # Restart half: can the pending round's session be reloaded and answered?
    p = subprocess.Popen(
        [ARGS.kiro, "acp", "--agent-engine", "kas"],
        cwd=CWD, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    assert p.stdout is not None and p.stdin is not None
    threading.Thread(
        target=lambda: [q.put(l.strip()) for l in p.stdout if l.strip()], daemon=True
    ).start()
    iid = req("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
        "_meta": {"kiro": {"clientName": "wayfinder-spike", "checkpoints": True}},
    })
    init_resp = pump(iid, 30)
    if not init_resp or "result" not in init_resp:
        return event("verdict", "T8: FAIL (restart initialize did not complete)")
    lid = req("session/load", {"sessionId": sid, "cwd": CWD, "mcpServers": [mcp_server_spec()]})
    loaded = pump(lid, 90)
    if not loaded or "error" in loaded:
        event("verdict", f"T8: death detectable; session/load FAILED after kill "
                         f"({json.dumps((loaded or {}).get('error'))[:200]}) — "
                         "dead-session card rule is mandatory")
        return
    event("load", f"session {sid} reloaded after process death")
    pump(-999, 5)  # drain replay stream
    prompt(sid, format_answers(rnd, ARGS.envelope) + CONFIRM_INSTRUCTION, 240)
    text = "".join(AGENT_TEXT)
    hits = sum(1 for q_ in rnd["questions"] if q_["id"] in text)
    event("verdict", f"T8: death detectable; session RELOADED; answers after reload "
                     f"referenced {hits}/{len(rnd['questions'])}")


def t9(sid):
    for fmt in ("json", "markdown", "lines"):
        seen = set()
        rid = req("session/prompt", {"sessionId": sid,
                  "prompt": [{"type": "text", "text": PRESENT_INSTRUCTION}]})
        rnd = wait_for_round(seen)
        pump(rid, 240)
        if not rnd:
            event("verdict", f"T9/{fmt}: FAIL (no round)")
            continue
        prompt(sid, format_answers(rnd, fmt) + CONFIRM_INSTRUCTION, 240)
        text = "".join(AGENT_TEXT)
        hits = sum(
            1 for q_ in rnd["questions"]
            if q_["id"] in text and (q_.get("recommended") or q_["options"][0]) in text
        )
        event("verdict", f"T9/{fmt}: exact id+choice pairs {hits}/{len(rnd['questions'])}")


def t10(sid):
    # server spawned with PROBE_BLOCKING=1 (see mcp_server_spec); delay is the
    # answer latency the blocking tool call must survive.
    seen = set()
    rid = req("session/prompt", {"sessionId": sid,
              "prompt": [{"type": "text", "text": PRESENT_INSTRUCTION}]})
    rnd = wait_for_round(seen)
    if not rnd:
        return event("verdict", "T10: FAIL (no round)")

    def answer_when_ready():
        apath = os.path.join(QUEUE_DIR, f"answer-{rnd['round_id']}.json")
        if time.time() >= answer_at and not os.path.exists(apath):
            with open(apath, "w", encoding="utf-8") as f:
                f.write(json.dumps({"round_id": rnd["round_id"], "answers": [
                    {"id": q_["id"], "answer": q_.get("recommended") or q_["options"][0]}
                    for q_ in rnd["questions"]]}))
            event("inject", f"wrote answer file for blocking round {rnd['round_id']}")

    answer_at = time.time() + ARGS.delay
    end = time.time() + ARGS.delay + 300
    resp = None
    while time.time() < end and resp is None:
        answer_when_ready()
        resp = pump(rid, 5)
    stop = (resp or {}).get("result", {}).get("stopReason")
    err = (resp or {}).get("error")
    text = "".join(AGENT_TEXT)
    timeout_mentioned = "TIMEOUT" in text
    event("verdict", f"T10: blocking call after {ARGS.delay}s -> stopReason={stop} "
                     f"error={err} timeout_mentioned={timeout_mentioned}")


def shutdown(code=0):
    try:
        OUT.close()
        p.stdin.close()
        p.terminate()
    except Exception:
        pass
    shutil.rmtree(QUEUE_DIR, ignore_errors=True)
    sys.exit(code)


def main():
    sid = new_session()
    t0 = time.time()
    {
        "T1": t1, "T2": t2, "T3": t3, "T4": t4, "T5": t5,
        "T6": t6, "T7": t7, "T8": t8, "T9": t9, "T10": t10,
    }[ARGS.test](sid)
    event("done", f"{ARGS.test} finished in {time.time() - t0:.0f}s; capture={OUT_PATH}")
    print("\n=== TIMELINE ===")
    for ts, kind, summary in EVENTS:
        print(f"{time.strftime('%H:%M:%S', time.localtime(ts))}  {kind:<18} {summary}")
    shutdown(0)


main()
