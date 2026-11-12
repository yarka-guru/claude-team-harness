import json
import re
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import teamlib

RITUAL_KINDS = ["daily", "retro", "planning", "reflect"]
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
TASK_ID_RE = re.compile(r"T-[0-9]{1,9}")
SESSION_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
TTY_RE = re.compile(r"/dev/ttys[0-9]{3}")
FOCUS_MAX_BODY = 200
FOCUS_LOCK = threading.Lock()
FOCUS_LAST = 0.0
FOCUS_SCRIPT = """on run argv
  set target to item 1 of argv
  tell application "Terminal"
    repeat with w in windows
      try
        repeat with t in tabs of w
          if tty of t is target then
            set selected of t to true
            set index of w to 1
            activate
            return "ok"
          end if
        end repeat
      end try
    end repeat
  end tell
  return "missing"
end run"""
FONT_RE = re.compile(r"/fonts/[A-Za-z0-9][A-Za-z0-9-]{0,63}\.woff2")
VERDICT_RE = re.compile(r"\b(PASS|FAIL)\b")
FONTS = HERE / "fonts"
WIP_LIMIT = 3
A2A_KINDS = {"dm", "ask", "answer", "handoff", "say"}
SAID_WINDOW = 30 * 60
OPS_TTL = 20
TIMELINE_MAX = 12 * 3600
BASE_HOSTS = {f"127.0.0.1:{teamlib.PORT}", f"localhost:{teamlib.PORT}"}


def allowed_hosts():
    domain = teamlib.dashboard_host()
    return BASE_HOSTS | ({domain, f"{domain}:80"} if domain else set())
FINISHED_WINDOW = 6 * 3600
GHOST_AFTER = 2 * 3600
ACTIVE_SESSION_WINDOW = 30 * 60
DONE_LIMIT = 300
EVENTS_DEFAULT = 400
EVENTS_MAX = 2000
SESSION_SCAN = 20000
TASK_EVENTS_MAX = 500
TEXT_MAX = 20000
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; font-src 'self'; "
                               "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                               "frame-ancestors 'none'",
}


LIVE_CLAIMS = """
    SELECT c.*, a.type AS actor_type, a.handle AS handle FROM claims c
    LEFT JOIN agents a ON a.agent_id=c.agent_id
    WHERE c.touched >= ?
      AND NOT (c.agent_id LIKE 'lead:%' AND c.touched < ?)
      AND COALESCE(a.status,'') NOT IN ('done','ended')"""


def claim_args(t):
    return (t - teamlib.CLAIM_TTL, t - teamlib.LEAD_CLAIM_TTL)


def open_db():
    if not teamlib.DB.exists():
        return None
    try:
        con = teamlib.connect(readonly=True)
        con.execute("BEGIN")
        return con
    except sqlite3.Error:
        return None


def rows(con, sql, args=()):
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise


def parse_data(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw}


def clip(text):
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX] + "…"


def event_dict(r):
    r["data"] = parse_data(r.get("data"))
    r["text"] = clip(r.get("text"))
    return r


def ritual_files():
    out = []
    base = teamlib.RITUALS
    for kind in RITUAL_KINDS:
        d = base / kind
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.suffix == ".md" and DATE_RE.fullmatch(f.stem) and f.is_file():
                st = f.stat()
                out.append({"kind": kind, "date": f.stem, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: (x["date"], x["mtime"]), reverse=True)
    return out


def latest_rituals():
    latest = {}
    for r in ritual_files():
        latest.setdefault(r["kind"], r)
    return {k: latest.get(k) for k in RITUAL_KINDS}


LEAD_ASK = "COALESCE(json_valid(data) AND json_extract(data,'$.type')='lead_ask', 0)"
ENDED = "(kind='session' AND json_valid(data) AND json_extract(data,'$.ended')=1)"


def needs_user(con):
    out = []
    latest = rows(con, "SELECT * FROM events WHERE id IN "
                       f"(SELECT MAX(id) FROM events WHERE kind='needs_user' GROUP BY session, agent_id, {LEAD_ASK}) ORDER BY id")
    for r in latest:
        data = parse_data(r.get("data"))
        if isinstance(data, dict) and data.get("type") == "lead_ask":
            sql = (f"SELECT 1 FROM events WHERE id>? AND session IS ? "
                   f"AND (kind='request' OR {ENDED} OR (kind='tool' AND target='Stop')) LIMIT 1")
            args = (r["id"], r["session"])
        else:
            sql = (f"SELECT 1 FROM events WHERE id>? AND session IS ? "
                   f"AND (agent_id = ? OR kind='request' OR {ENDED}) LIMIT 1")
            args = (r["id"], r["session"], r["agent_id"])
        if not con.execute(sql, args).fetchone():
            out.append(event_dict(r))
    return out


def normalize_agents(agents, t):
    for a in agents:
        a["result"] = clip(a.get("result"))
        a["ghost"] = a.get("status") == "running" and (a.get("last_ts") or a.get("started") or 0) < t - GHOST_AFTER
        if a["ghost"]:
            a["status"] = "stale"
    return agents


def snapshot():
    t = time.time()
    snap = {
        "now": t,
        "db": teamlib.DB.exists(),
        "last_event_id": 0,
        "tasks": [],
        "agents": [],
        "claims": [],
        "sessions": [],
        "needs_user": [],
        "rituals": latest_rituals(),
        "counters": {"running": 0, "doing": 0, "review": 0, "blocked": 0, "waiting_user": 0},
        "claim_ttl": teamlib.CLAIM_TTL,
        "lead_claim_ttl": teamlib.LEAD_CLAIM_TTL,
    }
    con = open_db()
    if con is None:
        return snap
    try:
        snap["last_event_id"] = (rows(con, "SELECT COALESCE(MAX(id),0) AS m FROM events") or [{"m": 0}])[0]["m"]
        tasks = rows(con, """
            SELECT t.*, COALESCE((SELECT MAX(e.ts) FROM events e WHERE e.task=t.id AND e.kind='task'
                AND json_extract(e.data,'$.to')=t.status
                AND json_extract(e.data,'$.from') IS NOT json_extract(e.data,'$.to')), t.updated, t.created) AS since
            FROM tasks t WHERE t.status!='done' AND t.status!='dropped'
            ORDER BY t.seq""")
        done = rows(con, """
            SELECT t.*, COALESCE(t.finished, t.updated) AS since FROM tasks t WHERE t.status='done'
            ORDER BY COALESCE(t.finished, t.updated) DESC LIMIT ?""", (DONE_LIMIT,))
        snap["tasks"] = tasks + done
        snap["done_total"] = (rows(con, "SELECT COUNT(*) AS c FROM tasks WHERE status='done'") or [{"c": 0}])[0]["c"]
        agents = rows(con, """
            SELECT * FROM agents
            WHERE (status='running' AND COALESCE(last_ts, started, 0) >= ?)
               OR COALESCE(stopped, last_ts, started, 0) >= ?""", (t - GHOST_AFTER, t - FINISHED_WINDOW))
        normalize_agents(agents, t)
        live = sorted((a for a in agents if a["status"] == "running"), key=lambda a: a.get("started") or 0)
        rest = sorted((a for a in agents if a["status"] != "running"),
                      key=lambda a: a.get("stopped") or a.get("last_ts") or a.get("started") or 0, reverse=True)
        agents = live + rest
        said = {}
        for r in rows(con, "SELECT agent_id, kind, ts, text FROM events WHERE kind IN ('say','report','handoff','decision') "
                           "AND ts >= ? ORDER BY id DESC", (t - SAID_WINDOW,)):
            said.setdefault(r["agent_id"], {"kind": r["kind"], "ts": r["ts"], "text": clip(r["text"])})
        for a in agents:
            a["said"] = said.get(a["agent_id"])
        snap["agents"] = agents
        snap["claims"] = rows(con, LIVE_CLAIMS + " ORDER BY c.touched DESC", claim_args(t))
        for c in snap["claims"]:
            repo = Path(c["repo"]) if c.get("repo") else None
            c["repo_name"] = (repo.parent.name if repo.name == ".git" else repo.name) if repo else None
        sessions = rows(con, """
            SELECT session, MIN(ts) AS started, MAX(ts) AS last_ts FROM events
            WHERE id > ? AND session IS NOT NULL AND ts >= ? GROUP BY session""",
                        (snap["last_event_id"] - SESSION_SCAN, t - 24 * 3600))
        running = {}
        for a in agents:
            if a.get("status") == "running" and a.get("session"):
                running[a["session"]] = running.get(a["session"], 0) + (0 if a.get("type") == "team-lead" else 1)
        known = {s["session"] for s in sessions}
        for sid in running:
            if sid not in known:
                mine = [a for a in agents if a.get("session") == sid]
                sessions.append({"session": sid, "started": min(a.get("started") or t for a in mine),
                                 "last_ts": max(a.get("last_ts") or 0 for a in mine)})
        active = []
        for s in sessions:
            if s["session"] in running or s["last_ts"] >= t - ACTIVE_SESSION_WINDOW:
                s["short"] = s["session"][:8]
                s["running"] = running.get(s["session"], 0)
                active.append(s)
        active.sort(key=lambda s: s["started"])
        snap["sessions"] = active
        snap["needs_user"] = needs_user(con)
        c = snap["counters"]
        c["running"] = sum(1 for a in agents if a.get("status") == "running" and a.get("type") != "team-lead")
        for task in tasks:
            if task["status"] in c:
                c[task["status"]] += 1
    finally:
        con.close()
    return snap


def events(after, limit, tools):
    con = open_db()
    if con is None:
        return {"events": [], "last_event_id": 0}
    try:
        last = (rows(con, "SELECT COALESCE(MAX(id),0) AS m FROM events") or [{"m": 0}])[0]["m"]
        cond = "" if tools else " AND kind!='tool'"
        if after is None:
            out = rows(con, f"SELECT * FROM events WHERE id<=?{cond} ORDER BY id DESC LIMIT ?", (last, limit))
            out.reverse()
        else:
            out = rows(con, f"SELECT * FROM events WHERE id>? AND id<=?{cond} ORDER BY id ASC LIMIT ?", (after, last, limit))
        return {"events": [event_dict(r) for r in out], "last_event_id": last, "complete": len(out) < limit}
    finally:
        con.close()


def task_detail(tid):
    if not tid or not TASK_ID_RE.fullmatch(tid):
        return None
    con = open_db()
    if con is None:
        return None
    try:
        found = rows(con, "SELECT * FROM tasks WHERE id=?", (tid,))
        if not found:
            return None
        evs = rows(con, "SELECT * FROM events WHERE task=? AND kind!='tool' ORDER BY id DESC LIMIT ?", (tid, TASK_EVENTS_MAX))
        evs.reverse()
        tools = rows(con, "SELECT COUNT(*) AS c, MAX(ts) AS last FROM events WHERE task=? AND kind='tool'", (tid,))
        agents = normalize_agents(rows(con, "SELECT * FROM agents WHERE task=? ORDER BY started", (tid,)), time.time())
        claims = rows(con, LIVE_CLAIMS + " AND c.task=? ORDER BY c.touched DESC", claim_args(time.time()) + (tid,))
        return {"task": found[0], "events": [event_dict(r) for r in evs], "agents": agents, "claims": claims,
                "tool_count": tools[0]["c"] if tools else 0, "tool_last": tools[0]["last"] if tools else None}
    finally:
        con.close()


def first_id_since(con, ts):
    hi = con.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
    lo = 1
    if not hi:
        return 1
    while lo < hi:
        mid = (lo + hi) // 2
        r = con.execute("SELECT ts FROM events WHERE id>=? ORDER BY id LIMIT 1", (mid,)).fetchone()
        if r is None or r[0] >= ts:
            hi = mid
        else:
            lo = mid + 1
    return lo


def lane_key(e, labels):
    a, typ, sess = e["actor"], e["actor_type"], e["session"] or ""
    if not a or a == "user":
        return None
    if not typ and "#" in a:
        typ = a.split("#", 1)[0]
    if typ == "team-lead" or a == "lead" or str(e["agent_id"] or "").startswith("lead:"):
        return "lead:" + sess[:8], labels.get(sess, sess[:4]) if sess else "CLI", "team-lead"
    if typ == "reviewer":
        return "reviewer", "reviewer", "reviewer"
    if typ == "codex":
        return "codex", "Codex", "codex"
    base = a.split("#", 1)[0] or a
    return "a:" + base, base, typ


def verdict_of(e):
    d = e["data"] if isinstance(e["data"], dict) else {}
    v = str(d.get("verdict") or "").upper()
    if v in ("PASS", "FAIL"):
        return v
    m = VERDICT_RE.search(e["text"] or "")
    return m.group(1) if m else None


def wait_end(evs, i):
    e = evs[i]
    d = e["data"] if isinstance(e["data"], dict) else {}
    lead_ask = d.get("type") == "lead_ask"
    for j in range(i + 1, len(evs)):
        f = evs[j]
        if f["session"] != e["session"]:
            continue
        fd = f["data"] if isinstance(f["data"], dict) else {}
        if f["kind"] == "request" or (f["kind"] == "session" and fd.get("ended") is True):
            return f["ts"]
        if not lead_ask and e["agent_id"] is not None and f["agent_id"] == e["agent_id"]:
            return f["ts"]
    return None


def merge(spans):
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def empty_ops(t):
    return {"now": t, "day0": t, "window": [t - 3600, t + 300], "bucket": 60, "lanes": [], "rituals": [], "links": [],
            "kpi": {"done": 0, "runs": 0, "resumes": 0, "waited": 0, "waitCount": 0, "longest": None, "doing": 0, "wip": WIP_LIMIT,
                    "wipSessions": 1, "wipPer": [], "wipOver": [],
                    "review": 0, "todo": 0, "backlog": 0, "blocked": 0, "waitingCards": 0, "pass": 0, "fail": 0, "firstPass": 0,
                    "firstOf": 0, "rework": 0, "reworkBy": [], "cycleMedian": None, "cycleMax": None, "cycleN": 0, "tools": 0,
                    "toolsHour": 0, "toolSpark": [0] * 12, "claims": 0, "conflicts": 0, "overlaps": 0}}


OPS_CACHE = {}


def ops():
    t = time.time()
    lt = time.localtime(t)
    day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    con = open_db()
    if con is None:
        return empty_ops(t)
    try:
        key = (str(teamlib.DB), int(t // OPS_TTL), day0)
        if OPS_CACHE.get("key") == key:
            return OPS_CACHE["val"]
        out = compute_ops(con, t, day0)
        OPS_CACHE.update(key=key, val=out)
        return out
    finally:
        con.close()


def is_lead(e):
    return e["actor_type"] == "team-lead" or e["actor"] == "lead" or str(e["agent_id"] or "").startswith("lead:")


def is_reviewer(e):
    typ = e["actor_type"] or str(e["actor"] or "").split("#", 1)[0]
    return typ in ("reviewer", "codex")


def review_verdict(e):
    return verdict_of(e) if e["kind"] == "report" and is_reviewer(e) else None


TOOL_AGG = {}
TOOL_LOCK = threading.Lock()


def tool_minutes(con, start_id, day0):
    with TOOL_LOCK:
        key = (str(teamlib.DB), day0)
        if TOOL_AGG.get("key") != key:
            TOOL_AGG.clear()
            TOOL_AGG.update(key=key, last=start_id - 1, cells={})
        top = con.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        if top < TOOL_AGG["last"]:
            TOOL_AGG.update(last=start_id - 1, cells={})
        cells = TOOL_AGG["cells"]
        for r in con.execute("SELECT actor, actor_type, session, agent_id, CAST(ts/60 AS INTEGER) AS m, COUNT(*), MIN(ts), MAX(ts) "
                             "FROM events WHERE kind='tool' AND id>? AND id<=? AND ts>=? GROUP BY actor, actor_type, session, agent_id, m",
                             (TOOL_AGG["last"], top, day0)):
            c = cells.get(r[:5])
            if c:
                c[0] += r[5]
                c[1] = min(c[1], r[6])
                c[2] = max(c[2], r[7])
            else:
                cells[r[:5]] = [r[5], r[6], r[7]]
        TOOL_AGG["last"] = top
        return [{"actor": kk[0], "actor_type": kk[1], "session": kk[2], "agent_id": kk[3], "m": kk[4], "n": v[0], "t0": v[1], "t1": v[2]}
                for kk, v in cells.items()]


def compute_ops(con, t, day0):
    out = empty_ops(t)
    out["day0"] = day0
    k = out["kpi"]
    labels = {}
    for r in rows(con, "SELECT session, substr(text,1,40) AS text FROM events WHERE kind='request' AND text LIKE '/%' "
                       "AND id IN (SELECT MIN(id) FROM events WHERE kind='request' GROUP BY session)"):
        labels[r["session"]] = r["text"].split()[0].lstrip("/") if r["text"].split() else r["text"]
    for r in rows(con, "SELECT a.session AS session, t.feature AS feature, MAX(a.started) AS m FROM agents a JOIN tasks t ON t.id=a.task "
                       "WHERE t.feature IS NOT NULL AND a.session IS NOT NULL GROUP BY a.session, t.feature ORDER BY m"):
        labels[r["session"]] = r["feature"]
    for r in rows(con, "SELECT session, feature FROM tasks WHERE session IS NOT NULL AND feature IS NOT NULL ORDER BY updated"):
        labels[r["session"]] = r["feature"]
    start_id = first_id_since(con, day0)
    evs = rows(con, "SELECT id, ts, kind, actor, actor_type, session, agent_id, task, target, substr(text,1,300) AS text, data "
                    "FROM events WHERE id>=? AND ts>=? AND kind!='tool' ORDER BY id", (start_id, day0))
    for e in evs:
        e["data"] = parse_data(e["data"]) if e["data"] else None
    tool_rows = tool_minutes(con, start_id, day0)
    tasks = {x["id"]: x for x in rows(con, "SELECT id, status, started, finished, session, owner, owner_type FROM tasks")}
    for st, name in (("doing", "doing"), ("review", "review"), ("todo", "todo"), ("backlog", "backlog"),
                     ("blocked", "blocked"), ("waiting_user", "waitingCards")):
        k[name] = sum(1 for x in tasks.values() if x["status"] == st)
    cycles = sorted(round((x["finished"] - x["started"]) / 60) for x in tasks.values()
                    if x["status"] == "done" and x["finished"] and x["started"] and x["finished"] >= day0)
    if cycles:
        k["cycleMedian"], k["cycleMax"], k["cycleN"] = round(statistics.median(cycles)), cycles[-1], len(cycles)
    k["claims"] = len(rows(con, LIVE_CLAIMS, claim_args(t)))
    agents = rows(con, "SELECT agent_id, session, task, status, started, handle, type FROM agents")
    handle_type = {a["handle"]: a["type"] for a in agents if a["handle"]}
    running = {a["agent_id"] for a in agents if a["status"] in ("running", "idle")}
    active = {a["session"] for a in agents if a["status"] == "running" and a["session"]}
    active |= {e["session"] for e in evs if e["session"] and e["ts"] >= t - ACTIVE_SESSION_WINDOW}
    active |= {r["session"] for r in tool_rows if r["session"] and r["t1"] >= t - ACTIVE_SESSION_WINDOW}
    per = {}
    for x in tasks.values():
        if x["status"] != "doing":
            continue
        ag = max((a for a in agents if a["task"] == x["id"] and a["session"]), key=lambda a: a["started"] or 0, default=None)
        sess = x["session"] or (ag["session"] if ag else "")
        per[sess] = per.get(sess, 0) + 1
    k["wipSessions"] = max(1, len(active))
    k["wip"] = WIP_LIMIT * k["wipSessions"]
    k["wipPer"] = sorted(([labels.get(sess, sess[:4]) if sess else None, n] for sess, n in per.items()), key=lambda r: -r[1])
    k["wipOver"] = [r for r in k["wipPer"] if r[1] > WIP_LIMIT]
    if not evs and not tool_rows:
        return out
    first_ts = min(([evs[0]["ts"]] if evs else []) + ([min(r["t0"] for r in tool_rows)] if tool_rows else []))
    w0 = max(day0, t - TIMELINE_MAX, first_ts)
    w0 = w0 - w0 % 900
    w1 = t + max(300, (t - w0) * 0.04)
    bucket = next((b for b in (60, 120, 300, 600, 900) if (w1 - w0) / b <= 360), 900)
    out["window"], out["bucket"] = [w0, w1], bucket
    nb = int((w1 - w0) // bucket) + 1
    lanes = {}
    last_lead = {"s": None}

    def get_lane(key_, name, role, ts, session):
        ln = lanes.get(key_)
        if ln is None:
            ln = lanes[key_] = {"key": key_, "name": name, "role": role, "first": ts, "last": ts, "acts": [0] * nb, "spans": [],
                                "open": {}, "waits": [], "marks": [], "spark": [0] * 12, "tools": 0, "lead": role == "team-lead",
                                "session": session}
        ln["first"], ln["last"] = min(ln["first"], ts), max(ln["last"], ts)
        return ln

    def lane(e, touch=True):
        if is_lead(e):
            if e["session"]:
                last_lead["s"] = e["session"]
            elif last_lead["s"]:
                e = dict(e, session=last_lead["s"])
        lk = lane_key(e, labels)
        if lk is None:
            return None
        return get_lane(lk[0], lk[1], lk[2], e["ts"], e["session"]) if touch else lk

    def task_lane(task, ts):
        x = tasks.get(task) or {}
        owner, otype = x.get("owner"), x.get("owner_type")
        if owner and owner != "lead" and otype != "team-lead":
            lk = lane_key({"actor": owner, "actor_type": otype, "session": None, "agent_id": None}, labels)
            if lk:
                return get_lane(lk[0], lk[1], lk[2], ts, None)
        sess = x.get("session") or last_lead["s"]
        lk = lane_key({"actor": "lead", "actor_type": "team-lead", "session": sess, "agent_id": None}, labels)
        return get_lane(lk[0], lk[1], lk[2], ts, sess)

    tool_total = tool_hour = 0
    tool_spark = [0] * 12
    for r in tool_rows:
        lk = lane_key({"actor": r["actor"], "actor_type": r["actor_type"], "session": r["session"], "agent_id": r["agent_id"]}, labels)
        n, mt = r["n"], r["m"] * 60
        tool_total += n
        if r["t1"] > t - 3600:
            tool_hour += n
        j = int((mt - (t - 7200)) // 600)
        if 0 <= j < 12:
            tool_spark[j] += n
        if lk is None:
            continue
        ln = get_lane(lk[0], lk[1], lk[2], r["t0"], r["session"])
        ln["last"] = max(ln["last"], r["t1"])
        ln["tools"] += n
        b = int((mt - w0) // bucket)
        if 0 <= b < nb:
            ln["acts"][b] += n
        s5 = int((mt - (t - 3600)) // 300)
        if 0 <= s5 < 12:
            ln["spark"][s5] += n
    k["tools"], k["toolsHour"], k["toolSpark"] = tool_total, tool_hour, tool_spark

    done, rework, first_verdict, last_verdict = set(), {}, {}, {}
    waits = []
    n_ev = len(evs)
    next_task_move = {}
    for i in range(n_ev - 1, -1, -1):
        e = evs[i]
        if e["kind"] == "task" and e["task"]:
            e["_next_move"] = next_task_move.get(e["task"])
            to = (e["data"] or {}).get("to") if isinstance(e["data"], dict) else None
            if to != "waiting_user":
                next_task_move[e["task"]] = e["ts"]
    for i, e in enumerate(evs):
        kind, d = e["kind"], e["data"] if isinstance(e["data"], dict) else {}
        ln = lane(e)
        if kind == "start":
            k["resumes" if d.get("resumed") else "runs"] += 1
            if ln:
                ln["open"].setdefault(e["agent_id"], e["ts"])
        elif kind == "stop":
            if ln and e["agent_id"] in ln["open"]:
                ln["spans"].append([ln["open"].pop(e["agent_id"]), e["ts"]])
        elif kind == "task":
            if d.get("to") == "done" and d.get("from") != "done":
                done.add(e["task"])
            if d.get("from") == "review" and d.get("to") == "doing":
                failed = last_verdict.get(e["task"]) == "FAIL"
                manual = (is_lead(e) or e["actor"] == "user") and not any(m in str(d.get("note") or "") for m in teamlib.RESUME_NOTES)
                if failed or manual:
                    rework[e["task"]] = rework.get(e["task"], 0) + 1
                    last_verdict.pop(e["task"], None)
            if d.get("to") == "waiting_user" and d.get("from") != "waiting_user":
                end = e.get("_next_move")
                tl = task_lane(e["task"], e["ts"])
                waits.append({"from": e["ts"], "to": end or t, "open": end is None, "lane": tl["key"],
                              "key": "t:" + str(e["task"]), "what": f"{e['task']}: {d['title']}" if d.get("title") else None})
        elif kind == "needs_user":
            end = wait_end(evs, i)
            if ln is None:
                ln = task_lane(e["task"], e["ts"]) if e["task"] else None
            waits.append({"from": e["ts"], "to": end or t, "open": end is None, "lane": ln["key"] if ln else None,
                          "key": f"p:{e['session']}:{e['agent_id']}:{d.get('type') == 'lead_ask'}",
                          "what": (e["text"] or "")[:120] or None})
        elif kind == "report":
            v = review_verdict(e)
            if v:
                k["pass" if v == "PASS" else "fail"] += 1
                if e["task"]:
                    first_verdict.setdefault(e["task"], v)
                    last_verdict[e["task"]] = v
                if ln:
                    ln["marks"].append([e["ts"], v.lower(), e["task"], (e["text"] or "")[:160]])
        elif kind in ("decision", "blocker"):
            if ln:
                ln["marks"].append([e["ts"], kind, e["task"], (e["text"] or "")[:160]])
        if kind in A2A_KINDS and ln and not ln["lead"] and e["actor"] != "user":
            tgt = e.get("target")
            if tgt and tgt not in ("all", "user", "lead") and not str(tgt).startswith("lead"):
                ttype = handle_type.get(tgt)
                if ttype != "team-lead":
                    tk = lane_key({"actor": tgt, "actor_type": ttype, "session": None, "agent_id": None}, labels)
                    if tk and tk[0] != ln["key"]:
                        out["links"].append([e["ts"], ln["key"], tk[0], kind, e["actor"], tgt, (e["text"] or "")[:120]])
        if kind == "conflict":
            k["conflicts"] += 1
        elif kind == "overlap":
            k["overlaps"] += 1
        elif kind == "ritual":
            out["rituals"].append([e["ts"], d.get("kind") or "ritual"])
    k["done"] = len(done)
    k["rework"] = sum(rework.values())
    k["reworkBy"] = sorted(rework.items(), key=lambda kv: -kv[1])
    k["firstOf"] = len(first_verdict)
    k["firstPass"] = sum(1 for v in first_verdict.values() if v == "PASS")
    per_key = {}
    for w in waits:
        per_key.setdefault(w["key"], []).append(w)
    flat = []
    for ws in per_key.values():
        ws.sort(key=lambda w: w["from"])
        cur = None
        for w in ws:
            if cur and w["from"] <= cur["to"]:
                cur["to"] = max(cur["to"], w["to"])
                cur["open"] = cur["open"] or w["open"]
            else:
                cur = dict(w)
                flat.append(cur)
    k["waitCount"] = len(flat)
    k["waited"] = round(sum(b2 - a2 for a2, b2 in merge([[w["from"], w["to"]] for w in flat])) / 60)
    if flat:
        lw = max(flat, key=lambda w: w["to"] - w["from"])
        k["longest"] = {"min": round((lw["to"] - lw["from"]) / 60), "from": lw["from"], "to": lw["to"], "what": lw["what"], "open": lw["open"]}
    for w in flat:
        ln = lanes.get(w["lane"])
        if ln is None:
            ln = get_lane(w["lane"] or "lead:", None, "team-lead", w["from"], None)
        ln["waits"].append([w["from"], w["to"], w["open"], w["what"]])
    for ln in lanes.values():
        for aid, st in ln.pop("open").items():
            ln["spans"].append([st, t if aid in running else ln["last"]])
        if ln["lead"]:
            ln["spans"] = [[ln["first"], t if any(a.startswith("lead:" + (ln["session"] or "")[:8]) for a in running) else ln["last"]]]
        ln["spans"] = merge(ln["spans"])
    out["lanes"] = sorted((ln for ln in lanes.values() if ln["tools"] >= 3 or ln["marks"] or ln["waits"]),
                          key=lambda ln: (not ln["lead"], ln["first"]))
    return out


def focus_target(session):
    con = open_db()
    if con is None:
        return None
    try:
        r = con.execute("SELECT tty, term FROM agents WHERE agent_id=? AND session=?", (teamlib.lead_id(session), session)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not r or r["term"] != "Apple_Terminal" or not isinstance(r["tty"], str) or not TTY_RE.fullmatch(r["tty"]):
        return None
    return r["tty"]


def focus_tab(session):
    tty = focus_target(session)
    if tty is None:
        return 404, "no terminal tab for this session"
    try:
        out = subprocess.run(["/usr/bin/osascript", "-e", FOCUS_SCRIPT, tty], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return 502, "osascript failed"
    res = out.stdout.strip()
    if out.returncode != 0:
        return 502, "osascript failed"
    return (200, "focused") if res == "ok" else (404, "tab not found")


def ritual(kind, date):
    if kind not in RITUAL_KINDS or not date or not DATE_RE.fullmatch(date):
        return None
    base = teamlib.RITUALS.resolve()
    f = (base / kind / f"{date}.md").resolve()
    try:
        f.relative_to(base)
    except ValueError:
        return None
    if not f.is_file():
        return None
    return {"kind": kind, "date": date, "mtime": f.stat().st_mtime, "text": f.read_text(errors="replace")}


def int_param(q, name, default, lo, hi):
    v = q.get(name, [None])[0]
    if v is None or v == "":
        return default
    if not re.fullmatch(r"\d{1,12}", v):
        raise ValueError(name)
    return max(lo, min(hi, int(v)))


class Handler(BaseHTTPRequestHandler):
    timeout = 10
    server_version = "team-dashboard"
    sys_version = ""

    def log_message(self, fmt, *args):
        if len(args) > 1 and str(args[1]).startswith(("2", "3")):
            return
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def send(self, code, body, ctype="application/json; charset=utf-8", cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in HEADERS.items():
            if not (cache and k == "Cache-Control"):
                self.send_header(k, v)
        if cache:
            self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def deny(self, code, msg):
        self.send(code, {"error": msg})

    def guard(self):
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in allowed_hosts():
            self.deny(403, "bad host")
            return False
        return True

    def do_GET(self):
        if not self.guard():
            return
        try:
            url = urlsplit(self.path)
            q = parse_qs(url.query, keep_blank_values=True)
        except ValueError:
            self.deny(400, "bad request")
            return
        try:
            if url.path in ("/", "/index.html"):
                self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/snapshot":
                self.send(200, snapshot())
            elif url.path == "/api/events":
                after = q.get("after", [None])[0]
                after = None if after in (None, "") else int_param(q, "after", 0, 0, 10**12)
                limit = int_param(q, "limit", EVENTS_DEFAULT, 1, EVENTS_MAX)
                tools = q.get("tools", ["1"])[0] != "0"
                self.send(200, events(after, limit, tools))
            elif url.path == "/api/task":
                r = task_detail(q.get("id", [""])[0])
                if r is None:
                    self.deny(404, "not found")
                else:
                    self.send(200, r)
            elif url.path == "/api/ops":
                self.send(200, ops())
            elif FONT_RE.fullmatch(url.path):
                f = (FONTS / url.path.rsplit("/", 1)[1]).resolve()
                if f.parent != FONTS.resolve() or not f.is_file():
                    self.deny(404, "not found")
                else:
                    self.send(200, f.read_bytes(), "font/woff2", cache=True)
            elif url.path == "/api/rituals":
                self.send(200, {"rituals": ritual_files()})
            elif url.path == "/api/ritual":
                r = ritual(q.get("kind", [""])[0], q.get("date", [""])[0])
                if r is None:
                    self.deny(404, "not found")
                else:
                    self.send(200, r)
            else:
                self.deny(404, "not found")
        except ValueError as e:
            self.deny(400, f"bad parameter {e}")
        except sqlite3.Error as e:
            self.deny(503, f"db: {e}")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self.guard():
            return
        if self.path != "/api/focus":
            self.deny(405, "read-only")
            return
        if self.headers.get("Origin") not in {f"http://{h}" for h in allowed_hosts()} or self.headers.get("X-Team-Action") != "focus" \
                or self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
            self.deny(403, "forbidden")
            return
        try:
            n = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            n = -1
        if n < 2 or n > FOCUS_MAX_BODY:
            self.deny(400, "bad body")
            return
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            self.deny(400, "bad body")
            return
        if not isinstance(body, dict) or set(body) != {"session"} or not isinstance(body["session"], str) \
                or not SESSION_RE.fullmatch(body["session"]):
            self.deny(400, "bad body")
            return
        global FOCUS_LAST
        with FOCUS_LOCK:
            if time.monotonic() - FOCUS_LAST < 1:
                self.deny(429, "too many")
                return
            FOCUS_LAST = time.monotonic()
        code, msg = focus_tab(body["session"])
        self.send(code, {"ok": code == 200, "result": msg})

    def do_OPTIONS(self):
        if self.guard():
            self.deny(405, "read-only")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", teamlib.PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write(f"team dashboard on http://127.0.0.1:{teamlib.PORT}/ db={teamlib.DB}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
