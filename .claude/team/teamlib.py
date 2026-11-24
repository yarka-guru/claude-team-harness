import contextlib
import io
import json
import os
import re
import shlex
import socket
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("TEAM_WORKSPACE") or ROOT.parent.parent)
DB = Path(os.environ.get("TEAM_DB") or ROOT / "state" / "team.db")
STATE = DB.parent
RITUALS = Path(os.environ.get("TEAM_RITUALS") or ROOT / "rituals")
LESSONS = ROOT / "lessons"
PROTOCOL = ROOT / "PROTOCOL.md"
CLI = "python3 " + shlex.quote(str(ROOT / "team.py"))
CLI_DOC = "python3 .claude/team/team.py"
PORT = int(os.environ.get("TEAM_PORT") or 7777)
ETC = Path(os.environ.get("TEAM_ETC") or "/etc")
DOMAIN_MARK = " # claude-team"
STE_MODES = ("off", "warn", "deny")
STE_NOTE_SHOWN = 6
STE_LIMIT = 2.0
STE_DATA = ("ste_lint.py", "avoid.tsv", "terms.tsv")
_STE_LINT = {}
KEEP = Path(os.environ.get("TEAM_KEEP") or ROOT / "keep")
SCOPE_TEMPLATE = ROOT / "security" / "scope-template.md"
SECURITY_MODES = ("off", "warn", "deny")
SECURITY_READERS = {"reviewer", "codex", "researcher", "Explore", "Plan"}
SECURITY_LABEL = "security"
SCOPE_MAX = 65536


def config_value(key):
    try:
        path = Path(os.environ.get("TEAM_CONFIG") or ROOT / "config.json")
        if path.is_file() and path.stat().st_size <= 4096:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return data.get(key) if isinstance(data, dict) else None
    except Exception:
        return None
    return None


def configured_lang():
    lang = os.environ.get("TEAM_LANG") or config_value("lang")
    lang = str(lang or "").strip().lower() if isinstance(lang, (str, type(None))) else ""
    return "uk" if lang.startswith("uk") or lang == "ua" else "en"


def ste_mode():
    mode = os.environ.get("TEAM_STE") or config_value("ste")
    mode = mode.strip().lower() if isinstance(mode, str) else ""
    return mode if mode in STE_MODES else "off"


def security_mode():
    mode = os.environ.get("TEAM_SECURITY") or config_value("security")
    mode = mode.strip().lower() if isinstance(mode, str) else ""
    return mode if mode in SECURITY_MODES else "deny"


LANG = configured_lang()
TEXT = {
    "guard.fact": ("fact-check: blocked {what} — {reason}", "фактчек: заблоковано {what} — {reason}"),
    "guard.ste_error": ("STE check: the linter failed, the text went through unchecked (see {log})",
                        "STE-перевірка: лінтер впав, текст пропущено без перевірки (див. {log})"),
    "guard.fact_error": ("fact-check: error in {fn}, the call went through unchecked", "фактчек: помилка в {fn}, виклик пропущено без перевірки"),
    "guard.security_error": ("security gate: error in {fn}, the call went through unchecked",
                             "безпековий гейт: помилка в {fn}, виклик пропущено без перевірки"),
    "guard.asks_error": ("open-asks reminder: error in {fn}, no reminder this time", "нагадування про «Потрібен ти»: помилка в {fn}, цього разу без нагадування"),
    "guard.waived": ("fact-check: the lead waived {h} — {reason}", "фактчек: лід пропустив перевірку {h} — {reason}"),
    "guard.git": ("{wt} ({branch}): git operation blocked — {names} editing here", "{wt} ({branch}): заблоковано git-операцію — тут редагує {names}"),
    "conflict": ("{path} — held by {holder} since {since}", "{path} — тримає {holder} з {since}"),
    "overlap": ("{where} — {me} ({branch}) and {other} ({other_branch}) are changing the same file",
                "{where} — {me} ({branch}) і {other} ({other_branch}) змінюють один файл"),
    "ask.agent": ("question from an agent", "питання від агента"),
    "note.resumed": ("resumed work", "продовжує роботу"),
    "note.finished": ("work finished, waiting for the lead's acceptance", "роботу завершено, чекає прийомки лідом"),
    "session.lead": ("lead session: {source}", "сесія ліда: {source}"),
    "session.ended": ("session ended: {reason}", "сесія завершена: {reason}"),
    "turn.end": ("end of turn", "кінець ходу"),
}
RESUME_NOTES = set(TEXT["note.resumed"])

STATUSES = ["backlog", "todo", "doing", "review", "blocked", "waiting_user", "done", "dropped"]
OPEN_STATUSES = ["todo", "doing", "review", "blocked", "waiting_user"]
CHAT_KINDS = ["say", "ask", "answer", "decision", "blocker", "handoff", "report"]
INBOX_KINDS = CHAT_KINDS + ["overlap"]
INBOX_BUDGET = 8000
AUTO_DONE_TYPES = {"codex", "reviewer", "researcher", "Explore", "Plan", "claude-code-guide", "statusline-setup"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CLAIM_TTL = 2 * 3600
LEAD_CLAIM_TTL = 30 * 60
TASK_REF = re.compile(r"\bT-(\d+)\b")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  session TEXT,
  agent_id TEXT,
  actor TEXT,
  actor_type TEXT,
  kind TEXT NOT NULL,
  target TEXT,
  task TEXT,
  text TEXT,
  data TEXT
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, id);
CREATE INDEX IF NOT EXISTS events_task ON events(task);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  title TEXT NOT NULL,
  feature TEXT,
  status TEXT NOT NULL,
  owner TEXT,
  owner_type TEXT,
  agent_id TEXT,
  model TEXT,
  effort TEXT,
  session TEXT,
  source TEXT,
  note TEXT,
  created REAL,
  updated REAL,
  started REAL,
  finished REAL
);
CREATE TABLE IF NOT EXISTS agents(
  agent_id TEXT PRIMARY KEY,
  session TEXT,
  handle TEXT,
  name TEXT,
  type TEXT,
  model TEXT,
  effort TEXT,
  description TEXT,
  task TEXT,
  status TEXT,
  started REAL,
  stopped REAL,
  last_ts REAL,
  last_tool TEXT,
  last_target TEXT,
  tools INTEGER DEFAULT 0,
  result TEXT,
  cursor INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS spawns(
  tool_use_id TEXT PRIMARY KEY,
  session TEXT,
  name TEXT,
  type TEXT,
  description TEXT,
  model TEXT,
  effort TEXT,
  task TEXT,
  ts REAL,
  agent_id TEXT
);
CREATE TABLE IF NOT EXISTS claims(
  path TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  actor TEXT,
  session TEXT,
  repo TEXT,
  rel TEXT,
  worktree TEXT,
  branch TEXT,
  task TEXT,
  since REAL,
  touched REAL,
  PRIMARY KEY(path, agent_id)
);
CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS factchecks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  hash TEXT NOT NULL,
  verdict TEXT NOT NULL,
  checker TEXT,
  checker_type TEXT,
  agent_id TEXT,
  session TEXT,
  ts REAL,
  claims TEXT,
  note TEXT,
  text_head TEXT
);
CREATE INDEX IF NOT EXISTS factchecks_hash ON factchecks(hash, id);
"""
MIGRATIONS = [
    "ALTER TABLE spawns ADD COLUMN prompt_head TEXT",
    "ALTER TABLE agents ADD COLUMN linked TEXT",
    "ALTER TABLE agents ADD COLUMN tty TEXT",
    "ALTER TABLE agents ADD COLUMN term TEXT",
    "ALTER TABLE agents ADD COLUMN label TEXT",
    "ALTER TABLE agents ADD COLUMN tab_title TEXT",
]


def now():
    return time.time()


def msg(key, **kw):
    return TEXT[key][LANG == "uk"].format(**kw)


def connect(readonly=False):
    if readonly:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    else:
        DB.parent.mkdir(parents=True, exist_ok=True)
        fresh = not DB.exists()
        con = sqlite3.connect(DB, timeout=3, isolation_level=None)
        if fresh:
            os.chmod(DB, 0o600)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=3000")
        con.executescript(SCHEMA)
        for m in MIGRATIONS:
            try:
                con.execute(m)
            except sqlite3.OperationalError:
                pass
    con.row_factory = sqlite3.Row
    return con


def short(text, n=200):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def emit(con, kind, actor, text="", session=None, agent_id=None, actor_type=None, target=None, task=None, data=None):
    cur = con.execute(
        "INSERT INTO events(ts,session,agent_id,actor,actor_type,kind,target,task,text,data) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now(), session, agent_id, actor, actor_type, kind, target, task, text, json.dumps(data, ensure_ascii=False) if data else None),
    )
    return cur.lastrowid


def max_event(con):
    return con.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]


LEAD_ASK_SQL = "COALESCE(json_valid(data) AND json_extract(data,'$.type')='lead_ask', 0)"
ASK_CLOSED_SQL = "kind='request' OR (kind='session' AND json_valid(data) AND json_extract(data,'$.ended')=1)"


def open_asks(con, session=None, upto=None):
    top = max_event(con) if upto is None else upto
    where = " AND session IS ?" if session else ""
    latest = con.execute(f"SELECT * FROM events WHERE id IN (SELECT MAX(id) FROM events WHERE kind='needs_user' AND id<=?{where} "
                         f"GROUP BY session, agent_id, {LEAD_ASK_SQL}) ORDER BY id", (top, session) if session else (top,)).fetchall()
    out = []
    for r in latest:
        try:
            data = json.loads(r["data"] or "{}")
        except ValueError:
            data = {}
        lead = isinstance(data, dict) and data.get("type") == "lead_ask"
        extra = "(kind='tool' AND target='Stop')" if lead else "agent_id = ?"
        if con.execute(f"SELECT 1 FROM events WHERE id>? AND id<=? AND session IS ? AND ({ASK_CLOSED_SQL} OR {extra}) LIMIT 1",
                       (r["id"], top, r["session"], *([] if lead else [r["agent_id"]]))).fetchone():
            continue
        out.append({"kind": "ask", "id": f"#{r['id']}", "ts": r["ts"], "session": r["session"], "actor": r["actor"],
                    "type": data.get("type") if isinstance(data, dict) else None, "text": r["text"] or ""})
    for t in con.execute("SELECT * FROM tasks WHERE status='waiting_user' ORDER BY seq"):
        out.append({"kind": "card", "id": t["id"], "ts": t["updated"], "session": t["session"], "actor": t["owner"], "type": "waiting_user",
                    "text": f"{t['title']}{' — ' + t['note'] if t['note'] else ''}"})
    return out


def ask_line(a, width=200):
    when = time.strftime("%m-%d %H:%M", time.localtime(a["ts"])) if a["ts"] else "?"
    if a["kind"] == "card":
        return f"- {a['id']} card waiting_user ({when}): {short(a['text'], width)} — close: {CLI} task done|todo {a['id']} --note \"...\""
    return f"- {a['id']} {a['type'] or 'needs_user'} by {a['actor']} ({when}): {short(a['text'], width)}"


def once(con, key):
    try:
        con.execute("INSERT INTO seen(key,ts) VALUES(?,?)", (key, now()))
        return True
    except sqlite3.IntegrityError:
        return False


def task_ref(*texts):
    for t in texts:
        m = TASK_REF.search(t or "")
        if m:
            return f"T-{int(m.group(1))}"
    return None


def get_task(con, tid):
    return con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone() if tid else None


def task_add(con, title, actor, status="todo", feature=None, owner=None, owner_type=None, agent_id=None,
             model=None, effort=None, session=None, source="lead", note=None):
    con.execute("BEGIN IMMEDIATE")
    try:
        seq = con.execute("SELECT COALESCE(MAX(seq),0)+1 FROM tasks").fetchone()[0]
        tid = f"T-{seq}"
        t = now()
        con.execute(
            "INSERT INTO tasks(id,seq,title,feature,status,owner,owner_type,agent_id,model,effort,session,source,note,created,updated,started) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, seq, title, feature, status, owner, owner_type, agent_id, model, effort, session, source, note, t, t,
             t if status == "doing" else None),
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    emit(con, "task", actor, f"{tid} → {status}: {title}", session=session, task=tid,
         data={"to": status, "title": title, "feature": feature, "owner": owner})
    return tid


def task_move(con, tid, status, actor, note=None, session=None, **fields):
    row = get_task(con, tid)
    if not row:
        raise SystemExit(f"no such task {tid}")
    t = now()
    sets = {"status": status, "updated": t}
    if status == "doing" and not row["started"]:
        sets["started"] = t
    if status in ("done", "dropped"):
        sets["finished"] = t
    if note:
        sets["note"] = note
    sets.update({k: v for k, v in fields.items() if v is not None})
    con.execute(f"UPDATE tasks SET {','.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), tid))
    if row["status"] != status or note:
        emit(con, "task", actor, f"{tid} {row['status']} → {status}: {row['title']}" + (f" — {note}" if note else ""),
             session=session, task=tid, data={"from": row["status"], "to": status, "title": row["title"], "note": note})


def agent_row(con, agent_id):
    return con.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone() if agent_id else None


def lead_id(session):
    return f"lead:{(session or 'unknown')[:8]}"


def handle_for(name, atype, agent_id):
    if name:
        return name
    return f"{atype or 'agent'}#{(agent_id or '')[-4:]}"


def identity(con, inp):
    session = inp.get("session_id")
    aid = inp.get("agent_id")
    if not aid:
        return {"agent_id": lead_id(session), "handle": "lead", "type": "team-lead", "session": session, "lead": True}
    row = agent_row(con, aid)
    if row:
        return {"agent_id": aid, "handle": row["handle"], "type": row["type"], "session": session, "lead": False, "task": row["task"]}
    atype = inp.get("agent_type")
    return {"agent_id": aid, "handle": handle_for(None, atype, aid), "type": atype, "session": session, "lead": False}


def subagents_dir(inp):
    tp = inp.get("transcript_path")
    if not tp:
        return None
    p = Path(tp)
    return p.parent / p.stem / "subagents"


def meta_for(inp, aid=None):
    d, aid = subagents_dir(inp), aid or inp.get("agent_id")
    if not d or not aid:
        return {}
    try:
        return json.loads((d / f"agent-{aid}.meta.json").read_text())
    except Exception:
        return {}


def norm(text):
    return " ".join(str(text or "").split())


def message_text(d):
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return c if isinstance(c, str) else ""


def first_prompt(inp, aid=None):
    d, aid = subagents_dir(inp), aid or inp.get("agent_id")
    if not d or not aid:
        return ""
    try:
        with open(d / f"agent-{aid}.jsonl", errors="replace") as f:
            for _, line in zip(range(8), f):
                rec = json.loads(line)
                if rec.get("type") == "user":
                    return message_text(rec)
    except Exception:
        pass
    return ""


def last_reply(path):
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except Exception:
        return ""
    for line in reversed(lines[-400:]):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "assistant":
            c = (rec.get("message") or {}).get("content")
            for b in c if isinstance(c, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "SubagentHandback":
                    return str((b.get("input") or {}).get("message") or "")
            txt = message_text(rec)
            if txt.strip():
                return txt
    return ""


def git_info(path):
    p = Path(path).resolve()
    d = p if p.is_dir() else p.parent
    while True:
        g = d / ".git"
        if g.exists():
            if g.is_dir():
                gitdir, common = g, g
            else:
                txt = g.read_text().strip()
                gitdir = Path(txt.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = (d / gitdir).resolve()
                cd = gitdir / "commondir"
                common = (gitdir / cd.read_text().strip()).resolve() if cd.exists() else gitdir.parent.parent
            branch = None
            try:
                head = (gitdir / "HEAD").read_text().strip()
                branch = head.split("refs/heads/", 1)[1] if "refs/heads/" in head else head[:12]
            except Exception:
                pass
            rel = str(p.relative_to(d)) if p != d else "."
            return {"worktree": str(d), "repo": str(common), "branch": branch, "rel": rel}
        if d.parent == d:
            return None
        d = d.parent


def port_open(port=None):
    port = port or PORT
    s = socket.socket()
    s.settimeout(0.2)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def anchor_owner():
    try:
        rule = (ETC / "pf.anchors" / "claude-team").read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"\bport 80 -> 127\.0\.0\.1 port (\d{1,5})\b", rule)
    if not m or not 0 < int(m.group(1)) < 65536:
        return None
    target = int(m.group(1))
    own = re.search(r"^# claude-team port=(\d{1,5}) backend=(\d{1,5})[ \t]*$", rule, re.M)
    if not own:
        return target, target
    return (int(own.group(1)), target) if int(own.group(2)) == target else None


def anchor_backend():
    owner = anchor_owner()
    return owner[1] if owner and owner[0] == PORT else None


def dashboard_host():
    if anchor_backend() is None:
        return None
    try:
        lines = (ETC / "hosts").read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if line.rstrip().endswith(DOMAIN_MARK) and len(parts) >= 2:
            return parts[1].lower()
    return None


def dashboard_url():
    host = dashboard_host()
    return f"http://{host}" if host else f"http://127.0.0.1:{PORT}"


def own_server(pid):
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return False
    return re.search(r"(^|\s)" + re.escape(str(ROOT / "dashboard" / "server.py")) + r"(\s|$)", cmd.strip()) is not None


def listeners(port):
    try:
        out = subprocess.run(["lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return []
    return sorted({int(p) for p in out.split() if p.isdigit()})


def pidfile_server():
    try:
        pid = int((STATE / "dashboard.pid").read_text().strip())
        os.kill(pid, 0)
    except Exception:
        return None
    return pid if own_server(pid) else None


def server_pids():
    ports = {PORT, anchor_backend() or PORT}
    pids = {p for port in ports for p in listeners(port) if own_server(p)}
    pid = pidfile_server()
    return sorted(pids | ({pid} if pid else set()))


def server_alive():
    return pidfile_server() is not None or any(own_server(p) for p in listeners(PORT))


def ensure_dashboard():
    if port_open() or server_alive():
        return False
    server = ROOT / "dashboard" / "server.py"
    if not server.exists():
        return False
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "dashboard.log", "a") as log:
        p = subprocess.Popen([sys.executable, str(server)], stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(ROOT))
    try:
        p.wait(timeout=0.8)
        return False
    except subprocess.TimeoutExpired:
        pass
    (STATE / "dashboard.pid").write_text(str(p.pid))
    return True


def active_agents(con, session=None):
    q = "SELECT * FROM agents WHERE status='running' AND last_ts>?"
    args = (now() - CLAIM_TTL,)
    if session:
        q += " AND session=?"
        args += (session,)
    return con.execute(q + " ORDER BY started", args).fetchall()


def live_claims(con):
    t = now()
    con.execute(
        "DELETE FROM claims WHERE touched < ? OR (agent_id LIKE 'lead:%' AND touched < ?) "
        "OR agent_id IN (SELECT agent_id FROM agents WHERE status IN ('done','ended'))",
        (t - CLAIM_TTL, t - LEAD_CLAIM_TTL),
    )
    return con.execute("SELECT * FROM claims ORDER BY touched DESC").fetchall()


def ritual_path(kind, day=None):
    return RITUALS / kind / f"{day or time.strftime('%Y-%m-%d')}.md"


def read_lessons(atype):
    out = []
    for name in ("all", atype or ""):
        f = LESSONS / f"{name}.md"
        if name and f.exists():
            out.append(f.read_text().strip())
    return "\n\n".join(out)


TICKET = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def own_tty():
    pid = os.getppid()
    for _ in range(6):
        try:
            out = subprocess.run(["ps", "-o", "tty=,ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout.split()
        except Exception:
            return None
        if len(out) < 2:
            return None
        if out[0] not in ("??", "-"):
            return f"/dev/{out[0]}"
        pid = int(out[1])
    return None


def session_label(con, session, cwd=None):
    rows = con.execute("SELECT title, feature FROM tasks WHERE session=? OR agent_id IN (SELECT agent_id FROM agents WHERE session=?) "
                       "ORDER BY updated DESC LIMIT 5", (session, session)).fetchall()
    for r in rows:
        m = TICKET.search(r["title"] or "") or TICKET.search(r["feature"] or "")
        if m and not m.group(0).startswith("T-"):
            return m.group(0)
    for r in rows:
        if r["feature"]:
            return r["feature"]
    for r in con.execute("SELECT text FROM events WHERE session=? AND kind='request' ORDER BY id DESC LIMIT 30", (session,)):
        m = TICKET.search(r["text"] or "")
        if m and not m.group(0).startswith("T-"):
            return m.group(0)
    return Path(cwd).name if cwd else session[:4]


def session_title(transcript_path):
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400000))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return None
    found = {}
    for line in tail:
        if '"custom-title"' in line or '"ai-title"' in line:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "custom-title" and d.get("customTitle"):
                found["custom"] = d["customTitle"]
            elif d.get("type") == "ai-title" and d.get("aiTitle"):
                found["ai"] = d["aiTitle"]
    return found.get("custom") or found.get("ai")



FACT_TTL = 24 * 3600
FACT_MAX = 100000
FACT_FILE_MAX = 1000000
FACT_CHECKERS = {"reviewer", "codex"}
FACT_DIR = STATE / "factcheck"
EN_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
UK_MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
UK_DAYS = [("понеділ", 0), ("вівтор", 1), ("серед", 2), ("четвер", 3), ("ятниц", 4), ("субот", 5), ("неділ", 6)]
CRON_NAMES = {n: i for i, n in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}
CRON_NAMES.update({n.upper(): i + 1 for i, n in enumerate(EN_MONTHS)})
STOPWORDS = {"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "of", "on", "or", "than", "that", "the",
             "then", "to", "was", "were", "with", "after", "before", "and", "but", "і", "й", "та", "а", "в", "у", "на", "з", "із", "зі", "до",
             "після", "від", "по", "за", "що", "як", "чи", "або", "але", "не"}
LIST_MARK = r"^\s*(?:#{1,6}\s+|[-*•>]+\s+|\d{1,2}[.)]\s+)"
EN_MON = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
UK_MON = "(?:" + "|".join(UK_MONTHS) + ")"
ORD = r"(?:st|nd|rd|th)?"
UNIT_ALT = (r"%|×|x|[KMGTP]i?B|[KMGTP]i|[KMGT]|bytes?|ms|s|secs?|seconds?|mins?|minutes?|h|hrs?|hours?|d|days?|weeks?|months?|years?|"
            r"cores?|vCPUs?|CPUs?|GPUs?|хв\w*|сек\w*|год\w*|дн\w*|тиж\w*|місяц\w*|рок\w*|років")
UNITS = rf"(?:{UNIT_ALT})(?!\w)"
CRON_ATOM = r"(?:\*|\?|\d{1,2}(?:-\d{1,2})?|(?:MON|TUE|WED|THU|FRI|SAT|SUN|JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?:-[A-Z]{3})?)(?:/\d{1,2})?"
CRON_FIELD = rf"{CRON_ATOM}(?:,{CRON_ATOM})*"
EN_DAY = r"(?:(?i:monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?|Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)"
UK_DAY = (r"(?:що)?(?:понеділ(?:ок|ка|ку|ки)|вівтор(?:ок|ка|ку|ки)|серед(?:а|у|и|ою|ам|ах)|четвер(?:га|гу|гом|ги)?|"
          r"п['’ʼ]?ятниц(?:я|ю|і|ею|ям|ях)|субот(?:а|у|и|ою|ам|ах)?|неділ(?:я|ю|і|ею|ям|ях))")
ANY_DAY = rf"(?:{EN_DAY}|(?i:{UK_DAY}))"
FACT_RULES = [
    ("pr", r"https?://github\.com/[\w.\-]+/[\w.\-]+/(?:pull|issues)/\d+"),
    ("jira", r"(?<=/browse/)[A-Z][A-Z0-9]+-\d+"),
    ("", r"https?://[^\s<>|)\]]+"),
    ("", r"</?[A-Za-z][^<>]*>|<[@#!][^<>]*>"),
    ("cron", rf"(?<![^\s`'\"(]){CRON_FIELD}(?:[ \t]+{CRON_FIELD}){{4,5}}(?![^\s`'\".,;)])"),
    ("date", r"(?<![\w.\-/])(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?!\d)"),
    ("date", r"(?<![\w.\-/])(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})(?![\w.]\w)"),
    ("date", rf"(?<!\w)(?P<mon>{EN_MON})\.?\s+(?P<d>\d{{1,2}}){ORD}(?!\d)(?:,?\s+(?P<y>\d{{4}}))?(?![\w:])"),
    ("date", rf"(?<![\w.])(?P<d>\d{{1,2}}){ORD}\s+(?:of\s+)?(?P<mon>{EN_MON})(?:,?\s+(?P<y>\d{{4}}))?(?!\w)"),
    ("date", rf"(?i)(?<![\w.])(?P<d>\d{{1,2}})\s+(?P<mon>{UK_MON})(?:\s+(?P<y>\d{{4}})(?:\s*р(?:оку|\.)?)?)?(?!\w)"),
    ("date", rf"(?<!\w)(?P<mon>{EN_MON})\s+(?P<y>\d{{4}})(?!\w)"),
    ("version", r"(?<![\w.\-/])v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?(?![\w.]\w|\w)|(?<![\w.\-/])v\d+(?:\.\d+)+(?!\w)"),
    ("time", r"(?<![\d:.])(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:\s?[AaPp]\.?[Mm]\.?)?"
             r"(?:\s?(?:UTC|GMT|Z|[A-Z]{2,4}T)(?:[+-]\d{1,2}(?::?\d{2})?)?|\s?[+-]\d{2}:?\d{2})?(?![\w:])"),
    ("time", r"(?<![\w.:])(?:1[0-2]|0?\d)\s?(?:am|pm|AM|PM)(?!\w)"),
    ("dm", rf"(?<![\w.\-/:$])(?P<d>\d{{1,2}})\.(?P<m>\d{{1,2}})(?![\w]|[.,]?\d|\s?{UNITS})"),
    ("weekday", rf"(?<!\w){ANY_DAY}(?!\w)"),
    ("jira", r"(?<![\w\-/])(?:CVE-\d{4}-\d+|(?!(?:UTF|SHA|ISO|AES|TLS|SSL|HTTP|GPT|MD|RFC|IPV)-)[A-Z][A-Z0-9]{1,9}-\d+)(?![\w\-])"),
    ("pr", r"(?<![\w.\-/&#])(?:[\w.\-]+/[\w.\-]+)?#\d+(?!\w)"),
    ("number", rf"(?<![\w.\-/#:$€£])[$€£]?\d+(?:[.,]\d+)*(?:{UNITS}|\s?%|\s+(?!\d)[^\W\d_][\w'’\-]*)?"),
]
SCHEDULE_HINT = r"(?i)(?:every|each)\s+$|кожн\w*\s+$"
DAILY = r"(?i)(?<!\w)(?:every\s+day|daily|щодня|щоденно|кожного\s+дня|кожен\s+день)(?!\w)"
FIRST_RUN = (r"(?i)(?<!\w)(?:(?:first|next|initial)(?:\s+\S+){0,3}?\s+(?:runs?|execution|cleanup|clean-up|firing|sweep|backup)|"
             r"(?:перш\w*|наступн\w*|найближч\w*)(?:\s+\S+){0,2}?\s+(?:запуск\w*|прогін|прогон\w*|очищенн\w*|чистк\w*|спрацюванн\w*))(?!\w)")
START_WORDS = (r"(?i)(?<!\w)(?:deploy\w*|roll(?:s|ed|ing)?\s+out|rollout|start\w*|launch\w*|go(?:es)?\s+live|enabl\w*|ship\w*|releas\w*|"
               r"from|after|since|beginning|with|upgrad\w*|migrat\w*|"
               r"розгорт\w*|викат\w*|деплой\w*|запуст\w*|старт\w*|увімкн\w*|почин\w*|почн\w*|з|із|зі|після|від|оновлен\w*|міграц\w*|реліз\w*)(?!\w)")
SENTENCE = r"[.!?]\s+(?=[A-ZА-ЯІЇЄҐ])"


def fact_now():
    v = os.environ.get("TEAM_NOW")
    t = datetime.fromisoformat(v.replace("Z", "+00:00")) if v else datetime.now(timezone.utc)
    if t.tzinfo:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


def fact_hash(text):
    import hashlib
    return hashlib.sha256(norm(text).encode()).hexdigest()[:16]


def fact_prep(text):
    return norm(" ".join(re.sub(LIST_MARK, "", line) for line in str(text or "").splitlines()))


def month_of(name):
    name = name.casefold()
    if name in UK_MONTHS:
        return UK_MONTHS.index(name) + 1
    return EN_MONTHS.index(name[:3]) + 1


def weekday_of(word):
    w = word.casefold()
    for stem, i in UK_DAYS:
        if stem in w:
            return i
    return [d[:3].casefold() for d in DAY_NAMES].index(w[:3])


def date_tok(m):
    g = m.groupdict()
    mon = month_of(g["mon"]) if g.get("mon") else int(g["m"])
    day = int(g["d"]) if g.get("d") else None
    if not 1 <= mon <= 12 or (day is not None and not 1 <= day <= 31):
        return None
    return {"date": (int(g["y"]) if g.get("y") else None, mon, day)}


def dm_tok(m, s):
    tok = date_tok(m)
    return dict(tok, loose=True) if tok else None


def cron_tok(m):
    fields = m.group(0).split()
    if not any("*" in f or "?" in f for f in fields) or not re.match(r"[\d*?]", fields[0]):
        return None
    return {"cron": " ".join(fields), "spec": cron_parse(fields)}


def number_tok(m, s):
    text = s[m.start():m.end()]
    parts = text.split()
    if len(parts) == 2 and parts[1].casefold() in STOPWORDS:
        return {"end": m.start() + len(parts[0])}
    return {}


def fact_scan(text):
    s = fact_prep(text)
    if len(s) > FACT_MAX:
        return s, None
    buf = s
    out = []
    for kind, rx in FACT_RULES:
        spans = []
        for m in re.finditer(rx, buf):
            extra = {}
            if kind == "date":
                extra = date_tok(m)
            elif kind == "dm":
                extra = dm_tok(m, s)
            elif kind == "cron":
                extra = cron_tok(m)
            elif kind == "weekday":
                extra = {"dow": weekday_of(m.group(0))}
            elif kind == "number":
                extra = number_tok(m, s)
            if extra is None:
                continue
            spans.append(m.span())
            if kind:
                tok = {"kind": "date" if kind == "dm" else kind, "start": m.start(), "end": m.end(), **extra}
                tok["text"] = s[tok["start"]:tok["end"]].strip()
                out.append(tok)
        if spans:
            chars = list(buf)
            for a, b in spans:
                chars[a:b] = " " * (b - a)
            buf = "".join(chars)
    urls = {t["text"].rsplit("/", 1)[-1] for t in out if t["kind"] == "pr" and t["text"].startswith("http")}
    out = [t for t in out if not (t["kind"] == "pr" and "#" in t["text"] and t["text"].split("#")[1] in urls)]
    out.sort(key=lambda t: t["start"])
    return s, out


def fact_tokens(text):
    toks = fact_scan(text)[1]
    if toks is None:
        return None
    seen, out = set(), []
    for t in toks:
        key = (t["kind"], t["text"].casefold())
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def cron_val(v):
    return CRON_NAMES[v] if v in CRON_NAMES else int(v)


def cron_field(f, lo, hi):
    out = set()
    for part in f.split(","):
        step = 1
        if "/" in part:
            part, st = part.split("/")
            step = int(st)
        if part in ("*", "?"):
            a, b = lo, hi
        elif "-" in part:
            a, b = (cron_val(x) for x in part.split("-"))
        else:
            a = cron_val(part)
            b = hi if step > 1 else a
        if step < 1 or a < lo or b > hi or a > b:
            raise ValueError(f)
        out |= set(range(a, b + 1, step))
    return out


def cron_parse(fields):
    try:
        sec = cron_field(fields[0], 0, 59) if len(fields) == 6 else {0}
        mi, ho, dom, mon, dow = fields[-5:]
        days = {d % 7 for d in cron_field(dow, 0, 7)}
        return {"sec": sec, "min": cron_field(mi, 0, 59), "hour": cron_field(ho, 0, 23), "dom": cron_field(dom, 1, 31),
                "mon": cron_field(mon, 1, 12), "dow": days, "dom_any": dom in ("*", "?"), "dow_any": dow in ("*", "?")}
    except (ValueError, KeyError):
        return None


def fires_on(spec, day):
    if day.month not in spec["mon"]:
        return False
    dom_ok, dow_ok = day.day in spec["dom"], (day.weekday() + 1) % 7 in spec["dow"]
    if spec["dom_any"] or spec["dow_any"]:
        return dom_ok and dow_ok
    return dom_ok or dow_ok


def first_fire(spec, start):
    for i in range(400):
        day = start.date() + timedelta(days=i)
        if not fires_on(spec, day):
            continue
        same = i == 0
        for h in sorted(spec["hour"]):
            if same and h < start.hour:
                continue
            for mi in sorted(spec["min"]):
                if same and h == start.hour and mi < start.minute:
                    continue
                for se in sorted(spec["sec"]):
                    t = datetime(day.year, day.month, day.day, h, mi, se)
                    if t >= start:
                        return t
    return None


def resolve_date(ymd, ref):
    y, m, d = ymd
    if d is None:
        return None
    years = [y] if y else [ref.year - 1, ref.year, ref.year + 1]
    cands = []
    for yy in years:
        try:
            cands.append(date(yy, m, d))
        except ValueError:
            pass
    return min(cands, key=lambda c: abs((c - ref.date()).days)) if cands else False


def sched_spec(dows, anchor_end, times, starts):
    import bisect
    hours, mins = set(range(24)), set(range(60))
    i = bisect.bisect_left(starts, anchor_end)
    for t in times[i:i + 2]:
        hm = re.match(r"(\d{1,2}):(\d{2})", t["text"])
        if hm and t["start"] - anchor_end <= 20 and re.search(r"UTC|GMT|Z\b", t["text"]):
            hours, mins = {int(hm.group(1))}, {int(hm.group(2))}
    return {"sec": {0}, "min": mins, "hour": hours, "dom": set(range(1, 32)), "mon": set(range(1, 13)), "dow": {(d + 1) % 7 for d in dows},
            "dom_any": True, "dow_any": False}


def schedules(s, toks):
    out = []
    times = [t for t in toks if t["kind"] == "time"]
    starts = [t["start"] for t in times]
    for c in toks:
        if c["kind"] == "cron" and c["spec"]:
            out.append((c["start"], f"cron `{c['cron']}`", c["spec"]))
    for w in toks:
        if w["kind"] != "weekday":
            continue
        word = w["text"].casefold()
        if not (re.search(SCHEDULE_HINT, s[max(0, w["start"] - 12):w["start"]]) or word.startswith("що") or (word[-1] == "s" and len(word) > 4)):
            continue
        out.append((w["start"], f'"{s[max(0, w["start"] - 6):w["end"]].strip()}"', sched_spec({w["dow"]}, w["end"], times, starts)))
    for m in re.finditer(DAILY, s):
        out.append((m.start(), f'"{m.group(0)}"', sched_spec(set(range(7)), m.end(), times, starts)))
    return out


def fact_calendar(text, ref=None):
    import bisect
    ref = ref or fact_now()
    s, toks = fact_scan(text)
    if toks is None:
        return []
    bounds = [0] + [m.end() for m in re.finditer(SENTENCE, s)]
    sent = lambda pos: bisect.bisect_right(bounds, pos) - 1
    phrases = list(re.finditer(FIRST_RUN, s))
    ends = [m.end() for m in phrases]
    problems, dates, loose = [], [], []
    for t in toks:
        if t["kind"] != "date":
            continue
        if t.get("loose"):
            i = bisect.bisect_right(ends, t["start"]) - 1
            if i < 0 or t["start"] - ends[i] > 40 or re.search(r"[.!?;]", s[ends[i]:t["start"]]):
                d = resolve_date(t["date"], ref)
                if d:
                    loose.append((t, d))
                continue
        d = resolve_date(t["date"], ref)
        if d is False:
            problems.append(f'"{t["text"]}" is not a real calendar date')
        elif d:
            dates.append((t, d))
    by_start = {w["start"]: w for w in toks if w["kind"] == "weekday"}
    by_end = {w["end"]: w for w in toks if w["kind"] == "weekday"}
    for t, d in dates:
        near = [by_start.get(t["end"] + k) for k in range(5)] + [by_end.get(t["start"] - k) for k in range(5)]
        for w in filter(None, near):
            a, b = (t["end"], w["start"]) if t["end"] <= w["start"] else (w["end"], t["start"])
            gap = s[a:b]
            if re.fullmatch(r"[\s,()\-–—.:/]*", gap) and not (re.search(r"\.\s", gap) and len(w["text"]) > 4) and w["dow"] != d.weekday():
                problems.append(f'"{w["text"]}" next to "{t["text"]}": {d.isoformat()} is a {DAY_NAMES[d.weekday()]}')
    for t, d in loose:
        w = next(filter(None, (by_end.get(t["start"] - k) for k in range(3))), None)
        if w and re.fullmatch(r"[\s,]*", s[w["end"]:t["start"]]) and w["dow"] != d.weekday():
            problems.append(f'"{w["text"]}" next to "{t["text"]}": {d.isoformat()} is a {DAY_NAMES[d.weekday()]}')
    scheds = schedules(s, toks)
    if not scheds:
        return list(dict.fromkeys(problems))[:20]
    in_sent = {}
    for t, d in dates:
        in_sent.setdefault(sent(t["start"]), []).append((t, d))
    starts = [(t, d) for t, d in dates + loose if ref.date() < d and re.search(START_WORDS, s[max(bounds[sent(t["start"])], t["start"] - 40):t["start"]])]
    for m in phrases:
        sid = sent(m.start())
        near = scheds if len(scheds) == 1 else [x for x in scheds if abs(x[0] - m.start()) <= 300]
        if not near:
            continue
        here = in_sent.get(sid, [])
        after = [x for x in here if x[0]["start"] >= m.start()]
        before = [x for x in here if x[0]["end"] <= m.start()]
        pick = after[0] if after else (before[-1] if before else None)
        if not pick:
            continue
        t, d = pick
        later = [x for e, x in starts if e is not t and x <= d and abs(sent(e["start"]) - sid) <= 1]
        start = max([datetime.combine(x, datetime.min.time()) for x in later] + [ref])
        misses = []
        for _, label, spec in sorted(near, key=lambda x: abs(x[0] - m.start())):
            f = first_fire(spec, start) if d >= ref.date() else None
            if not fires_on(spec, d):
                misses.append(f'"{m.group(0)}" date "{t["text"]}" ({d.isoformat()}, {DAY_NAMES[d.weekday()]}) is not a firing day of {label}')
            elif f and f.date() < d:
                misses.append(f'"{m.group(0)}" date "{t["text"]}" ({d.isoformat()}): {label} fires first on {f:%Y-%m-%d %H:%M:%S} UTC, '
                              f'counted from {start:%Y-%m-%d %H:%M} UTC. If the rollout starts later, say so with its date '
                              f'("deploy on ...", "from ...").')
            else:
                misses = []
                break
        problems += misses[:1]
        if len(problems) > 50:
            break
    return list(dict.fromkeys(problems))[:20]


def fact_problems(text, claims, ref=None):
    if not isinstance(claims, list):
        return ["claims must be a JSON list of objects, one per fact"]
    facts = {t["text"].casefold(): t for t in fact_tokens(text) or []}
    problems, covered = [], set()
    for i, c in enumerate(claims, 1):
        if not isinstance(c, dict):
            problems.append(f"claim {i}: not an object")
            continue
        tok = norm(c.get("token"))
        bad = len(problems)
        if tok.casefold() not in facts:
            problems.append(f'claim {i}: token "{tok}" is not one of the facts; copy one fact per claim from `factcheck tokens`')
        for k in ("source", "observed"):
            if not norm(c.get(k)):
                problems.append(f'claim {i} ("{tok}"): empty {k}')
        if c.get("source_kind") != "primary":
            problems.append(f'claim {i} ("{tok}"): source_kind is {json.dumps(c.get("source_kind"))}; only primary sources count '
                            f'(live system, gh/Jira read, deployed values at the merged commit, calendar computation)')
        if len(problems) == bad:
            covered.add(tok.casefold())
    for key, t in facts.items():
        if key not in covered:
            problems.append(f'{t["kind"]} "{t["text"]}" is covered by no valid primary claim')
    return problems + fact_calendar(text, ref)


def fact_record(con, text, verdict, checker, checker_type=None, agent_id=None, session=None, claims=None, note=None):
    h = fact_hash(text)
    con.execute("INSERT INTO factchecks(hash,verdict,checker,checker_type,agent_id,session,ts,claims,note,text_head) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (h, verdict, checker, checker_type, agent_id, session, now(), json.dumps(claims, ensure_ascii=False) if claims else None, note,
                 short(text, 200)))
    return h


def fact_ok(con, text, agent_id):
    r = con.execute("SELECT * FROM factchecks WHERE hash=? AND ts>? ORDER BY id DESC LIMIT 1", (fact_hash(text), now() - FACT_TTL)).fetchone()
    return bool(r) and (r["verdict"] == "WAIVED" or (r["verdict"] == "PASS" and r["agent_id"] != agent_id))


def fact_save(text):
    FACT_DIR.mkdir(parents=True, exist_ok=True)
    p = FACT_DIR / f"{fact_hash(text)}.txt"
    if not p.exists():
        p.write_text(text)
    return p


def authored(con, agent_id, path, text=None):
    if text is not None and con.execute("SELECT 1 FROM seen WHERE key=?", (f"wrote:{fact_hash(text)}:{agent_id}",)).fetchone():
        return True
    p = Path(path).expanduser().resolve()
    marks = ",".join("?" * len(EDIT_TOOLS))
    for r in con.execute(f"SELECT text FROM events WHERE kind='tool' AND agent_id=? AND target IN ({marks}) AND text LIKE ?",
                         (agent_id, *EDIT_TOOLS, "%" + p.name)):
        try:
            if Path(r["text"]).expanduser().resolve() == p:
                return True
        except Exception:
            pass
    return bool(con.execute("SELECT 1 FROM claims WHERE agent_id=? AND path=?", (agent_id, str(p))).fetchone())


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def split_heredocs(cmd):
    out, docs, end = [], [], None
    for line in cmd.split("\n"):
        if end is not None:
            if line.strip() == end:
                end = None
            else:
                docs[-1][1].append(line)
            continue
        m = HEREDOC.search(line)
        if m:
            end = m.group(2)
            docs.append((line[:m.start()] + line[m.end():], []))
            line = line[:m.start()] + line[m.end():]
        out.append(line)
    return "\n".join(out), [(head, "\n".join(body)) for head, body in docs]


def strip_heredocs(cmd):
    return split_heredocs(cmd)[0]
CONTROL = {"&&", "||", ";", "|", "&", "(", ")", ";;", "\n"}


def segments(cmd, strict=False, stripped=False):
    cmd = cmd if stripped else strip_heredocs(cmd)
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    lex.commenters = ""
    seg = []
    try:
        for tok in lex:
            if tok in CONTROL or set(tok) <= set(";&|()"):
                if seg:
                    yield seg
                seg = []
            else:
                seg.append(tok)
    except ValueError:
        if strict:
            raise
        return
    if seg:
        yield seg


SHELLS = {"bash", "sh", "zsh", "dash"}
WRAPPERS = {"env": {"-u", "-C", "--unset", "--chdir"}, "command": set(), "exec": set(), "nohup": set(), "time": set(), "stdbuf": set(),
            "sudo": {"-u", "-g", "-h", "-p"}, "nice": {"-n"}, "timeout": {"-s", "-k", "--signal", "--kill-after"},
            "xargs": {"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a"}}
GH_SUB = {"pr": {"create", "edit", "comment", "review", "close", "reopen", "merge"}, "issue": {"create", "edit", "comment", "close", "reopen"},
          "release": {"create", "edit"}}
GH_GLOBAL_VALUE = {"-R", "--repo", "--hostname"}
GH_TEXT = {"--body", "-b", "--notes", "-n", "--title", "-t", "--subject"}
GH_FILE = {"--body-file", "-F", "--notes-file"}
GH_DYNAMIC = {"--fill", "-f", "--fill-first", "--fill-verbose", "--editor", "-e", "--generate-notes", "--notes-from-tag"}
API_VALUE = {"-H", "--header", "-q", "--jq", "-t", "--template", "--hostname", "-p", "--preview", "--cache"}
API_TEXT_KEYS = {"body", "title", "name", "message", "commit_title", "commit_message", "subject", "description", "notes", "summary"}
API_TARGET = r"(?:^|/)(?:comments|replies|issues|pulls|releases|reviews|discussions|merges?)(?:/|$)"
SUBST = r"(?<!\\)(?:`|\$[({A-Za-z_]|\$\$)"
GH_WORD = r"(?:^|[\s;&|(`'\"/=])gh(?=\s)"
CMD_POS = (r"(?:^|[;&|(`\n]|\$\()\s*(?:(?:env|command|exec|nohup|time|sudo|xargs|nice|stdbuf|timeout(?:\s+-\S+)*\s+\S+)(?:\s+-\S+)*\s+"
           r"|[A-Za-z_]\w*=\S*\s+)*")
GH_RAW = (r"(?m)" + CMD_POS + r"(?:\S*/)?gh\s+(?:-\S+\s+(?:\S+\s+)?)*(?:(?:pr|issue|release)\s+(?:create|edit|comment|review|close|reopen|merge)\b"
                    r"|api\b[^\n]*(?:-X\s*(?:POST|PATCH|PUT)|--method[\s=]+(?:POST|PATCH|PUT)|\s-[fF]\s|--(?:raw-)?field))")
FACT_RAW = r"(?m)" + CMD_POS + r"(?:\S*python[\d.]*\s+(?:-\S+\s+)*)?\S*team(?:\.py)?\s+factcheck\s+(pass|fail|waive)\b"
LEDGER_WRITE = r"(?i)\b(?:(?:insert(?:\s+or\s+\w+)?|replace)\s+into|delete\s+from|update(?:\s+or\s+\w+)?)\s+[\"'`\[]?(?:\w+\.)?factchecks\b"
GATED_MCP = {
    "slack_send_message": ("message", "text"),
    "slack_send_message_draft": ("message", "text"),
    "slack_schedule_message": ("message", "text"),
    "slack_create_canvas": ("title", "content"),
    "slack_update_canvas": ("content", "sections"),
    "createJiraIssue": ("summary", "description", "additional_fields"),
    "editJiraIssue": ("fields", "update"),
    "transitionJiraIssue": ("fields", "update"),
    "addCommentToJiraIssue": ("commentBody",),
    "addWorklogToJiraIssue": ("commentBody",),
    "createConfluencePage": ("title", "body"),
    "updateConfluencePage": ("title", "body", "versionMessage"),
    "createConfluenceFooterComment": ("body",),
    "createConfluenceInlineComment": ("body",),
}
STRUCT_KEYS = {"type", "attrs", "marks", "version", "id", "accountId", "localId", "self", "key", "labels", "components", "priority", "issuetype",
               "parent", "assignee", "reporter", "project", "edit_type", "section_id", "fixVersions", "versions"}


def unwrap(seg):
    while seg:
        if re.fullmatch(r"[A-Za-z_]\w*=.*", seg[0]):
            seg = seg[1:]
            continue
        base = seg[0].rsplit("/", 1)[-1]
        if base not in WRAPPERS:
            break
        i = 1
        while i < len(seg) and seg[i].startswith("-") and seg[i] != "-":
            i += 2 if seg[i] in WRAPPERS[base] else 1
        seg = seg[i + (base == "timeout"):]
    return seg


def line_segments(line):
    try:
        return list(segments(line, strict=True, stripped=True))
    except ValueError:
        pass
    for m in re.finditer(r"(?:^|\s)#", line):
        try:
            return list(segments(line[:m.start()], strict=True, stripped=True))
        except ValueError:
            continue
    raise ValueError(line)


def shell_words(cmd):
    try:
        return list(segments(cmd, strict=True))
    except ValueError:
        return [seg for line in strip_heredocs(cmd).split("\n") for seg in line_segments(line)]


def commands(cmd, depth=0):
    out = []
    for seg in shell_words(cmd):
        seg = unwrap(seg)
        if not seg:
            continue
        base = seg[0].rsplit("/", 1)[-1]
        flag = next((i for i, a in enumerate(seg[1:-1], 1) if re.fullmatch(r"-\w*c\w*", a)), None)
        if depth < 3 and base in SHELLS and flag:
            out += commands(seg[flag + 1], depth + 1)
        elif depth < 3 and base == "eval":
            out += commands(" ".join(seg[1:]), depth + 1)
        else:
            out.append([base] + seg[1:])
    return out


def shell_heredocs(cmd):
    out = []
    for line, body in split_heredocs(cmd)[1]:
        try:
            segs = [unwrap(seg) for seg in segments(line, strict=True, stripped=True)]
        except ValueError:
            continue
        if any(seg and seg[0].rsplit("/", 1)[-1] in SHELLS and not any(re.fullmatch(r"-\w*c\w*", a) for a in seg[1:]) for seg in segs):
            out.append(body)
    return out


def strings(v):
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [t for x in v for t in strings(x)]
    if isinstance(v, dict):
        return [t for k, x in v.items() if k not in STRUCT_KEYS for t in strings(x)]
    return []


def mcp_outward(tool, ti):
    name = tool.rsplit("__", 1)[-1]
    if not tool.startswith("mcp__") or name not in GATED_MCP:
        return None
    fields = [(key, t) for key in GATED_MCP[name] for t in strings(ti.get(key))]
    return [(name, [f for f in fields if f[1].strip()], None)]


def literal(val, cmd):
    return f"'{val}'" in cmd


def read_body(val, here):
    if val == "-" or val.startswith("/dev/"):
        return None, f"`{val}` is not a regular file"
    p = Path(val).expanduser()
    p = p if p.is_absolute() or not here else Path(here) / p
    try:
        st = p.stat()
    except OSError:
        return None, f"cannot read {p}"
    if not stat.S_ISREG(st.st_mode):
        return None, f"{p} is not a regular file"
    if st.st_size > FACT_FILE_MAX:
        return None, f"{p} is over {FACT_FILE_MAX} bytes: split the text"
    try:
        with open(p, "rb") as f:
            return f.read(FACT_FILE_MAX).decode(errors="replace"), None
    except OSError:
        return None, f"cannot read {p}"


def text_value(flag, val, cmd, raw=None):
    lit = literal(val, cmd) or bool(raw and literal(raw, cmd))
    if re.search(SUBST, val) and not lit:
        return None, f"`{flag}` uses shell substitution"
    return (val if lit else re.sub(r"\\([`$\\])", r"\1", val)), None


def graphql_strings(query):
    return [(m.group(1) if m.group(1) is not None else re.sub(r'\\(.)', r"\1", m.group(2)))
            for m in re.finditer(r'"""(.*?)"""|"((?:[^"\\]|\\.)*)"', query, re.S)]


def split_flag(a, shorts):
    if a.startswith("--") and "=" in a:
        return a.split("=", 1)
    if not a.startswith("--") and len(a) > 2 and a[:2] in shorts:
        return a[:2], a[2:]
    return a, None


def gh_api(args, here, cmd):
    method, endpoint, fields, inp, i = None, None, [], None, 0
    while i < len(args):
        a, val = split_flag(args[i], {"-X", "-f", "-F", "-H", "-q", "-t", "-p"})
        if a in ("--help", "-h"):
            return None
        if a in ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input") or a in API_VALUE:
            if val is None:
                val = args[i + 1] if i + 1 < len(args) else ""
                i += 1
            if a in ("-X", "--method"):
                method = val.upper()
            elif a == "--input":
                inp = val
            elif a not in API_VALUE:
                fields.append((a, val))
        elif not a.startswith("-") and endpoint is None:
            endpoint = a
        i += 1
    if not (method in ("POST", "PATCH", "PUT") or (method is None and (fields or inp))):
        return None
    endpoint = (endpoint or "").split("?", 1)[0]
    if endpoint == "graphql":
        if inp:
            return [], "`--input` sends a query the gate does not parse"
        qflag, query = next(((f, kv.partition("=")[2]) for f, kv in fields if kv.partition("=")[0] == "query"), (None, ""))
        if qflag in ("-F", "--field") and query.startswith("@"):
            query, problem = read_body(query[1:], here)
            if problem:
                return [], problem
        elif re.search(r"(?<!\\)(?:\$\(|`)", query) and not (literal(query, cmd) or literal("query=" + query, cmd)):
            return [], "the graphql query is built with shell substitution"
        if not re.match(r"\s*(?:#[^\n]*\n\s*)*mutation\b", query):
            return None
        out = [("query", t) for t in graphql_strings(query)]
        for flag, kv in fields:
            key, _, val = kv.partition("=")
            if key == "query" or (flag in ("-F", "--field") and re.fullmatch(r"-?\d+(?:\.\d+)?|true|false|null", val)):
                continue
            if flag in ("-F", "--field") and val.startswith("@"):
                val, problem = read_body(val[1:], here)
            else:
                val, problem = text_value(flag, val, cmd, kv)
            if problem:
                return out, problem
            out.append((key, val))
        return out, None
    if not re.search(API_TARGET, endpoint):
        return None
    if inp:
        return [], "`--input` sends a file the gate does not parse"
    out = []
    for flag, kv in fields:
        key, _, val = kv.partition("=")
        if key.split("[", 1)[0] not in API_TEXT_KEYS:
            continue
        if flag in ("-F", "--field") and val.startswith("@"):
            val, problem = read_body(val[1:], here)
        else:
            val, problem = text_value(flag, val, cmd, kv)
        if problem:
            return out, problem
        out.append((key, val))
    return out, None


def gh_fields(words, here, cmd):
    args = words[1:]
    pos, i = [], 0
    while i < len(args) and len(pos) < 2:
        a = args[i]
        if a in ("--help", "-h"):
            return None
        if a in GH_GLOBAL_VALUE:
            i += 2
            continue
        if not a.startswith("-"):
            pos.append(a)
            if a == "api":
                return gh_api(args[i + 1:], here, cmd)
        i += 1
    if len(pos) < 2 or pos[1] not in GH_SUB.get(pos[0], ()):
        return None
    text = GH_TEXT | ({"--comment", "-c"} if pos[1] in ("close", "reopen") else set())
    shorts = {f for f in text | GH_FILE if len(f) == 2}
    fields = []
    while i < len(args):
        flag, val = split_flag(args[i], shorts)
        if flag in ("--help", "-h"):
            return None
        if flag in GH_DYNAMIC:
            return fields, f"`{flag}` takes the text from commits, an editor or GitHub"
        if flag in text | GH_FILE:
            if val is None:
                val = args[i + 1] if i + 1 < len(args) else ""
                i += 1
            if flag in GH_FILE:
                if val == "-" or val.startswith("/dev/"):
                    return fields, f"`{val}` is not a regular file"
                if "$" in val or "`" in val or ("$" in (here or "") and not Path(val).expanduser().is_absolute()):
                    return fields, f"the path of `{flag}` uses a shell variable"
                if cmd.count(val) > 1:
                    return fields, f"{val} is also used elsewhere in this command, so it may change before gh reads it"
                val, problem = read_body(val, here)
            else:
                val, problem = text_value(flag, val, cmd)
            if problem:
                return fields, problem
            fields.append((flag, val))
        i += 1
    return fields, None


def gh_outward(cmd, cwd, words):
    if words is None:
        return [("gh", [], "the command does not parse")] if re.search(GH_RAW, cmd) else None
    out, here = [], cwd
    for w in words:
        if w[0] == "cd" and len(w) == 2:
            p = Path(w[1]).expanduser()
            here = str(p if p.is_absolute() or not here else Path(here) / p)
            continue
        if w[0] != "gh":
            continue
        got = gh_fields(w, here, cmd)
        if got:
            sub = [a for a in w[1:] if not a.startswith("-")][:2]
            out.append((" ".join(["gh"] + sub), got[0], got[1]))
    if not out and any(re.search(GH_RAW, body) for body in shell_heredocs(cmd)):
        out.append(("gh", [], "gh runs inside a heredoc script"))
    return out or None


def fact_block(con, me, what, reason, data):
    emit(con, "guard", me["handle"], short(msg("guard.fact", what=what, reason=reason), 300), session=me["session"], agent_id=me["agent_id"],
           actor_type=me["type"], target=what, task=me.get("task"), data={"gate": "factcheck", **data})
    return reason


def fact_gate(con, me, tool, found):
    for what, fields, problem in found or []:
        if problem:
            flag = "--notes-file" if what.startswith("gh release") else "--body-file"
            hint = (f"Fact-check gate: `{what}`: {problem}." if problem.endswith("split the text") else
                    f"Fact-check gate: cannot read the text of `{what}` statically ({problem}). Write the text to a file first, "
                    f"then run gh in a separate call with `{flag} <literal path>` so the gate can check it.")
            return fact_block(con, me, what, hint, {"problem": problem})
        text = "\n".join(t for _, t in fields)
        if len(norm(text)) > FACT_MAX:
            if fact_ok(con, text, me["agent_id"]):
                continue
            path = fact_save(text)
            return fact_block(con, me, what, f"Fact-check gate: the text of `{what}` has {len(norm(text))} characters, over the {FACT_MAX} limit the gate "
                                             f"checks. Split the text, or ask the lead to waive it: `{CLI} factcheck waive {path} --as lead --reason \"...\"`.",
                              {"hash": fact_hash(text), "too_long": True, "tool": tool})
        toks = fact_tokens(text)
        if not toks:
            continue
        parts = [t for _, t in fields if fact_tokens(t)]
        if fact_ok(con, text, me["agent_id"]) or (parts and all(fact_ok(con, t, me["agent_id"]) for t in parts)):
            continue
        path = fact_save(text)
        shown = ", ".join(f'{t["kind"]} "{t["text"]}"' for t in toks[:15]) + (f" and {len(toks) - 15} more" if len(toks) > 15 else "")
        cal = fact_calendar(text)
        return fact_block(con, me, what,
                          f"Fact-check gate: `{what}` carries {len(toks)} checkable facts and this exact text has no PASS in the last 24h: {shown}. "
                          f"The text is saved at {path} (hash {fact_hash(text)}). Next: a reviewer or codex agent that did not write it checks every fact "
                          f"against a primary source (live system, gh/Jira read, deployed values at the merged commit, calendar computation; never our PR "
                          f"body, KB, drafts or chat) and runs `{CLI} factcheck pass {path} --as <its handle> --claims <claims.json>` "
                          f"(`{CLI} factcheck tokens {path} --json` prints the claims template). Then repeat this call unchanged."
                          + (" The calendar check already fails, fix the text first: " + " ".join(cal) if cal else ""),
                          {"hash": fact_hash(text), "tokens": len(toks), "tool": tool})
    return None


def fact_calls(cmd, words):
    if words is None:
        return [[m.group(1), None] for m in re.finditer(FACT_RAW, cmd)], None
    calls, bad = [], None
    for w in words:
        args = None
        if w[0].endswith("team.py"):
            args = w[1:]
        elif re.fullmatch(r"python[\d.]*", w[0]):
            j = 1
            while j < len(w) and w[j].startswith("-") and w[j] not in ("-m", "-c"):
                j += 1
            if j + 2 < len(w) and w[j] == "-m" and w[j + 1] in ("team", "team.py") and w[j + 2] == "factcheck":
                bad = f"`python3 {w[j]} ...` runs factcheck in a form the gate cannot attribute"
            elif j < len(w) and w[j].endswith("team.py"):
                args = w[j + 1:]
            elif j + 2 < len(w) and "$" in w[j] and w[j + 1] == "factcheck" and w[j + 2] in ("pass", "fail", "waive"):
                bad = "the path of team.py is a shell variable"
        elif "$" in w[0] and len(w) > 2 and w[1] == "factcheck" and w[2] in ("pass", "fail", "waive"):
            bad = "the path of team.py is a shell variable"
        if args and len(args) >= 2 and args[0] == "factcheck" and args[1] in ("pass", "fail", "waive"):
            calls.append(args[1:])
    return calls, bad


def fact_cli(con, me, cmd, cwd, words):
    if any(w[0] == "sqlite3" and re.search(LEDGER_WRITE, " ".join(w[1:])) for w in words or []):
        return fact_block(con, me, "factchecks", "Fact-check: the ledger is written only through `team.py factcheck`.", {"verb": "sql"})
    calls, bad = fact_calls(cmd, words)
    if bad:
        return fact_block(con, me, "factcheck", f"Fact-check: {bad}. Run it literally: `{CLI} factcheck <verb> <file> --as {me['handle']} ...`.",
                          {"verb": "form"})
    for rest in calls:
        verb = rest[0]
        if rest[1:] == [None]:
            return fact_block(con, me, f"factcheck {verb}", f"Fact-check: this command does not parse, so `factcheck {verb}` cannot be attributed. "
                                                            f"Run it as a plain command.", {"verb": verb})
        who, path = None, None
        for i, t in enumerate(rest[1:], 1):
            if t == "--as":
                who = rest[i + 1] if i + 1 < len(rest) else ""
            elif t.startswith("--as="):
                who = t[5:]
            elif path is None and not t.startswith("-") and rest[i - 1] not in ("--as", "--claims", "--note", "--reason"):
                path = t
        if verb == "waive":
            if not me["lead"]:
                return fact_block(con, me, "factcheck waive", "Fact-check: only the lead may waive a fact-check.", {"verb": verb})
            if who != "lead":
                return "Fact-check: a waiver needs an explicit `--as lead`."
            continue
        if not me["lead"] and who != me["handle"]:
            return f"Fact-check: record as yourself, `--as {me['handle']}`."
        if verb != "pass":
            continue
        if me["lead"] or me["type"] not in FACT_CHECKERS:
            return fact_block(con, me, "factcheck pass",
                              "Fact-check: only a reviewer or codex agent records a PASS, and never the author of the text. "
                              "Ask a reviewer to verify the facts against primary sources and run `factcheck pass` itself.", {"verb": verb})
        if path:
            p = Path(path).expanduser()
            p = p if p.is_absolute() or not cwd else Path(cwd) / p
            text, _ = read_body(str(p), cwd)
            if authored(con, me["agent_id"], p, text):
                return fact_block(con, me, "factcheck pass",
                                  f"Fact-check: you wrote or edited {p}, so you cannot pass it. Another reviewer or codex agent has to check it.",
                                  {"verb": verb, "path": str(p)})
    return None


def bash_facts(con, me, cmd, cwd):
    gh, fc = bool(re.search(GH_WORD, cmd)), "factcheck" in cmd
    if not (gh or fc):
        return None
    try:
        words = commands(cmd)
    except ValueError:
        words = None
    reason = fact_cli(con, me, cmd, cwd, words) if fc else None
    return reason or (fact_gate(con, me, "Bash", gh_outward(cmd, cwd, words)) if gh else None)


def mcp_facts(con, me, tool, ti):
    return fact_gate(con, me, tool, mcp_outward(tool, ti))


def note_authored(con, me, tool, ti):
    path = ti.get("file_path")
    if tool == "Write":
        text = ti.get("content")
    elif tool in ("Edit", "MultiEdit") and path and Path(path).is_file() and Path(path).stat().st_size < 262144:
        text = Path(path).read_text(errors="replace")
        for e in ti.get("edits") or [ti]:
            old, new = e.get("old_string") or "", e.get("new_string") or ""
            if old:
                text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    else:
        return None
    if isinstance(text, str) and text.strip():
        once(con, f"wrote:{fact_hash(text)}:{me['agent_id']}")
    return None


def ste_dir():
    return Path(os.environ.get("TEAM_STE_DIR") or ROOT / "ste")


def ste_alarm(signum, frame):
    raise TimeoutError(f"the STE check took more than {STE_LIMIT:g} s")


@contextlib.contextmanager
def ste_guard():
    timed = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    if timed:
        old = signal.signal(signal.SIGALRM, ste_alarm)
        prev = signal.setitimer(signal.ITIMER_REAL, STE_LIMIT)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            yield
    finally:
        if timed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
            if prev[0]:
                signal.setitimer(signal.ITIMER_REAL, *prev)


def ste_linter():
    import importlib.util
    path = ste_dir() / "ste_lint.py"
    if str(path) in _STE_LINT:
        return _STE_LINT[str(path)]
    try:
        spec = importlib.util.spec_from_file_location("ste_lint", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod = mod if callable(getattr(mod, "check", None)) and callable(getattr(mod, "gate", None)) else None
    except BaseException:
        mod = None
    _STE_LINT[str(path)] = mod
    return mod


def ste_log(con, me, what):
    try:
        if not once(con, f"ste-log:{what.strip().splitlines()[-1][-160:]}:{int(now() // 600)}"):
            return
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / "ste-errors.log", "a") as f:
            f.write(time.strftime("%F %T ") + f"{me.get('handle')}\n{what}\n")
        if what.startswith("Traceback") and once(con, f"ste-guard:{int(now() // 600)}"):
            emit(con, "guard", me["handle"], msg("guard.ste_error", log=STATE / "ste-errors.log"), session=me["session"],
                 agent_id=me["agent_id"], actor_type=me["type"], task=me.get("task"), data={"gate": "ste", "error": True})
    except Exception:
        pass


def ste_note(message, total):
    lines = message.split("\n")
    rows = [line for line in lines[1:] if line.startswith("  ") and not line.startswith("  … ")]
    tail = [line for line in lines[1:] if not line.startswith("  ")]
    more = [f"  … and {total - STE_NOTE_SHOWN} more"] if total > STE_NOTE_SHOWN else []
    return "\n".join(lines[:1] + rows[:STE_NOTE_SHOWN] + more + tail)


STE_RUN = {}


def ste_check(con, me, tool, kind, text):
    mode = ste_mode()
    if mode == "off" or not isinstance(text, str) or not text.strip() or STE_RUN.get("timeout"):
        return None
    try:
        missing = [name for name in STE_DATA if not (ste_dir() / name).is_file()]
        if missing:
            ste_log(con, me, f"{', '.join(missing)} missing in {ste_dir()}, the STE check is off")
            return None
        with ste_guard():
            lint = ste_linter()
            result = lint.check(text, kind) if lint else None
            decision, message = lint.gate(result, mode) if lint else ("allow", "")
        if lint is None:
            ste_log(con, me, f"no linter in {ste_dir()}, the STE check is off")
            return None
        if result.get("error"):
            ste_log(con, me, f"linter error: {result['error']}")
            return None
        hard, soft = list(result.get("hard") or []), list(result.get("soft") or [])
        if decision == "deny" and kind != "brief":
            decision = "warn"
        if hard:
            emit(con, "ste", me["handle"], f"{kind}: {len(hard)} hard, {len(soft)} soft, {decision}", session=me["session"],
                 agent_id=me["agent_id"], actor_type=me["type"], target=tool, task=me.get("task"),
                 data={"tool": tool, "kind": kind, "words": result.get("words"), "hard": len(hard), "soft": len(soft),
                       "decision": decision, "rules": dict(Counter(str(f.get("rule")) for f in hard))})
        if decision not in ("warn", "deny") or not message:
            return None
        return decision, str(message) if decision == "deny" else ste_note(str(message), len(hard) + len(soft))
    except BaseException as e:
        STE_RUN["timeout"] = STE_RUN.get("timeout") or isinstance(e, TimeoutError)
        ste_log(con, me, traceback.format_exc())
        return None


GATE_ERRORS = {"factcheck": ("factcheck-errors.log", "guard.fact_error"), "security": ("security-errors.log", "guard.security_error"),
               "asks": ("hook-errors.log", "guard.asks_error")}


def fail_open(con, me, fn, *args, gate="factcheck"):
    try:
        return fn(con, me, *args)
    except Exception:
        try:
            log, key = GATE_ERRORS[gate]
            STATE.mkdir(parents=True, exist_ok=True)
            with open(STATE / log, "a") as f:
                f.write(time.strftime("%F %T ") + f"{me.get('handle')} {fn.__name__}\n" + traceback.format_exc() + "\n")
            emit(con, "guard", me["handle"], msg(key, fn=fn.__name__), session=me["session"],
                   agent_id=me["agent_id"], actor_type=me["type"], task=me.get("task"), data={"gate": gate, "error": True})
        except Exception:
            pass
        return None


SCOPE_LINE_MAX = 1000
SCOPE_PART = re.compile(r"^[ \t]{0,8}(?:(?:>|[-*+]|\d{1,3}[.)]|\[[ xX]?\])[ \t]{0,4})*(?:\*\*|__)?security[ \t]{1,4}scope(?:\*\*|__)?[ \t]{0,4}:", re.I)
SCOPE_FORMS = "\"Security scope:\", \"- Security scope:\", \"1. Security scope:\", \"> Security scope:\" or \"**Security scope:**\""
PLACEHOLDER = re.compile(r"<[^<>\n]{0,1000}>")
CHECKBOX = re.compile(r"\[[ xX]?\]")
SECURITY_WORDS = re.compile(r"(?i)(?<!\w)(?:exploit\w*|proof[ -]{1,2}of[ -]{1,2}concept|poc|fuzz\w*|port[ -]{0,2}scan\w*|"
                            r"prompt[ -]{1,2}injection\w*|jailbreak\w*|sandbox[ -]{1,2}(?:escape|probe)\w*|reverse[ -]{1,2}shells?|"
                            r"pen[ -]?test\w*|penetration[ -]{1,2}test\w*|security[ -]{1,2}audit\w*|vulnerability[ -]{1,2}scan\w*|"
                            r"guardrail[ -]{1,2}test\w*|escape (?:from )?the container|container[ -]{1,2}escape\w*|nmap|scan the ports)(?!\w)")
PART_HEAD = re.compile(r"^[ \t]{0,8}(?:#{1,6}[ \t]{1,4}(?P<h>[^\n]{1,200}?)[ \t#:]{0,20}$|"
                       r"[*_]{0,3}(?P<k>[A-Za-z][A-Za-z /-]{0,40}?)[*_]{0,3}[ \t]{0,4}[*_]{0,3}:)")
QUIET_PARTS = {"out of scope", "constraints"}
SCOPE_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d{1,3}[.)])(?:[ \t]+(.*))?$")
SCOPE_EMPTY = {"none", "tbd", "todo", "na", "unknown", "tobedecided", "tobedefined"}
SCOPE_KEYED = re.compile(r"^([^:=—–]{1,40}?)[ \t]*(?:[:=—–]|[ \t]-[ \t])[ \t]*(.*)$")
SCOPE_HOLDER = re.compile(r"(?i)^(?:none|tbd|todo|n/?a|unknown|to[ \t]+be[ \t]+(?:decided|defined))[ \t]*(?:([,;])|[:—–]|-(?![^\W_])|$)")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
SCOPE_APPROVAL = re.compile(r"(?:(\d{4})-(\d{2})-(\d{2})|(\d{2})\.(\d{2})\.(\d{4}))[ \t]{0,4},?[ \t]{0,4}"
                            r"(?:«([^«»]*)»|\"([^\"]*)\"|“([^“”]*)”|'([^']*)'|‘([^‘’]*)’)\.?")
SCOPE_REFUSED = re.compile(r"(?i)^(?:no|nope|nah|not|not[ \t]{1,4}yet|pending|later|wait|hold[ \t]{1,4}on|refused|denied|ні|ще[ \t]{1,4}ні|"
                           r"поки[ \t]{1,4}ні|не|пізніше|потім|згодом|зачекай|don['’]?t|do[ \t]{1,4}not)(?!\w)|"
                           r"^(?:відмов|declin|refus|reject|stop|заборон)")
SCOPE_OWNER = re.compile(r"(?i)^(?:the user|employer, local copy)(?:$|[ \t]{0,4}[.,;:(\-–—])")
BASH_SCOPE = re.compile(r"keep/{1,8}t-\d+/")
BASH_WRITE = re.compile(r">|(?<![\w.-])(?:cp|mv|tee|rm|ln|dd|install|touch|truncate|chmod|python[\d.]*|perl|ruby|node)(?![\w-])|"
                        r"(?<![\w.-])sed(?:[ \t]{1,8}-[a-z]{1,8})*[ \t]{1,8}-[a-z]{0,8}i")


def scope_lines_of(text):
    return [line for line in unfenced(text).split("\n") if SCOPE_PART.match(line)]


def scope_line_of(text):
    return next(iter(scope_lines_of(text)), None)


def spawn_card(desc, prompt):
    text = unix_lines(prompt)
    line = scope_line_of(text)
    if line is None:
        return task_ref(desc, prompt[:300])
    return task_ref(desc) or task_ref(text.strip().split("\n", 1)[0]) or task_ref(line)


def scope_path(tid):
    return KEEP / tid / "scope.md"


def scope_exists(path):
    try:
        return path.is_symlink() or path.exists()
    except OSError:
        return True


def scope_label(tid):
    return SECURITY_LABEL if tid and scope_exists(scope_path(tid)) else None


def unix_lines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def unfenced(text):
    out, fence = [], None
    for line in text.split("\n"):
        m = FENCE_OPEN.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line[m.end():].strip():
                fence = None
            out.append("")
        elif m:
            fence = m.group(1)
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


def scope_value(text, key):
    m = re.search(rf"(?im)^[ \t>*_-]{{0,12}}{re.escape(key)}[ \t]{{0,4}}[*_]{{0,3}}[ \t]{{0,4}}:[*_]{{0,3}}[ \t]*(.*)$", text)
    return m.group(1).strip() if m else None


def scope_bare(value):
    return value.strip(" \t\"'«»*_`")


def scope_approval(value):
    m = SCOPE_APPROVAL.fullmatch(value)
    if not m:
        return "does not have the shape: date, comma, the words of the user in quote marks (YYYY-MM-DD, «the words of the user»)"
    y, mo, d = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(6), m.group(5), m.group(4))
    try:
        date(int(y), int(mo), int(d))
    except ValueError:
        return "has a date that is not valid"
    words = " ".join(next(g for g in m.groups()[6:] if g is not None).split())
    if not re.search(r"\w", words):
        return "has no words of the user in the quote marks"
    if SCOPE_REFUSED.search(re.sub(r"^[\W_]+", "", words.lower())):
        return "does not record an approval"
    return None


def scope_clean(text):
    return " ".join(re.sub(r"[*_`]", "", CHECKBOX.sub(" ", PLACEHOLDER.sub(" ", text))).split())


def scope_filled(value):
    while True:
        value = re.sub(r"(?<=\S)[ \t]*\([^()]*\)", " ", value).strip()
        norm = re.sub(r"[\W_]+", "", value).lower()
        if not norm or norm in SCOPE_EMPTY:
            return False
        holder = SCOPE_HOLDER.match(value)
        if not holder:
            return True
        if not holder.group(1):
            return False
        value = value[holder.end():].strip()
        keyed = SCOPE_KEYED.match(value)
        value = keyed.group(2) if keyed else value


def scope_items(text):
    m = re.search(r"(?ims)^#{1,6}[ \t]{0,4}in scope[ \t]{0,4}$(.*?)(?=^#{1,6}[ \t]|\Z)", text)
    lines = unfenced(m.group(1)).split("\n") if m else []
    out = []
    for i, line in enumerate(lines):
        item = SCOPE_ITEM.match(line)
        if not item:
            continue
        value = scope_clean(item.group(1) or "")
        keyed = SCOPE_KEYED.match(value)
        if keyed:
            value = keyed.group(2)
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if not value and nxt.strip() and not SCOPE_ITEM.match(nxt):
                value = scope_clean(nxt)
        if scope_filled(value):
            out.append(value)
    return out


def scope_problem(path):
    if path.is_symlink() or path.parent.is_symlink():
        return "symlink", f"the scope file {path} or its folder is a symbolic link"
    if not path.is_file():
        return "no-file", f"the scope file {path} is absent or is not a regular file"
    try:
        with open(path, "rb") as f:
            data = f.read(SCOPE_MAX + 1)
    except OSError as e:
        return "unreadable", f"the gate cannot read the scope file {path} ({e.strerror or e})"
    text = unix_lines(data.decode("utf-8-sig", "replace"))
    if len(data) > SCOPE_MAX or any(len(line) > SCOPE_LINE_MAX for line in text.split("\n")):
        return "too-long", f"the scope file {path} is larger than {SCOPE_MAX // 1024} KB or has a line longer than {SCOPE_LINE_MAX} characters"
    approved = scope_value(text, "Approved by the user")
    if not approved:
        return "not-approved", f"the line \"Approved by the user:\" in {path} is absent or empty"
    if PLACEHOLDER.search(approved):
        return "not-approved", f"the line \"Approved by the user:\" in {path} has the template placeholder"
    problem = scope_approval(approved)
    if problem:
        return "not-approved", f"the line \"Approved by the user:\" in {path} {problem}"
    owner = scope_bare(scope_value(text, "Owner of the target") or "")
    if PLACEHOLDER.search(owner) or not SCOPE_OWNER.match(owner):
        return "owner", (f"the line \"Owner of the target:\" in {path} does not start with \"the user\" or \"employer, local copy\" "
                         f"followed by the end of the line or one of . , ; : ( -")
    if not scope_items(text):
        return "in-scope", f"the part \"In scope\" in {path} has no filled item"
    return None


def security_words(text):
    paragraphs, current, part = [], [], None
    for line in text.split("\n"):
        m = PART_HEAD.match(line)
        if m:
            part = " ".join((m.group("h") or m.group("k")).strip(" *_").lower().split())
        if part in QUIET_PARTS or not line.strip():
            paragraphs.append(current)
            current = []
        else:
            current.append(line)
    paragraphs.append(current)
    found = set()
    for lines in paragraphs:
        found |= {w.group(0).lower() for w in SECURITY_WORDS.finditer(" ".join(" ".join(lines).split()))}
    return sorted(found)


def security_event(con, me, tool, tid, rule, decision, mode, text, path=None):
    emit(con, "security", me["handle"], short(text, 300), session=me["session"], agent_id=me["agent_id"], actor_type=me["type"],
         target=tool, task=tid or me.get("task"),
         data={"rule": rule, "decision": decision, "mode": mode, "tool": tool, "path": str(path) if path else None})


def security_note(con, me, tool, tid, rule, mode, text, path):
    security_event(con, me, tool, tid, rule, "note", mode, text, path)
    return "warn", text


def codex_note(con, me, tool, tid, mode, path):
    return security_note(con, me, tool, tid, "codex", mode,
                         f"security: the card {tid} is a security card. Do not give its scope file {path} or the transcripts of the card to codex.",
                         path)


def security_check(con, me, tool, ti, card=None):
    mode = security_mode()
    if mode == "off":
        return None
    agent = tool == "Agent"
    raw = ti.get("prompt") if agent else ti.get("message")
    text = unix_lines(raw) if isinstance(raw, str) else ""
    if not agent and not text.strip():
        return None
    atype = (ti.get("subagent_type") or "general-purpose") if agent else None
    scope_line = scope_line_of(text)
    named = task_ref(scope_line or "")
    if agent:
        tid = card
    elif scope_line is not None:
        tid = named or me.get("task")
        tid = tid if tid and get_task(con, tid) else None
    else:
        tid = task_ref(text) or me.get("task")
    path = scope_path(tid) if tid else KEEP / "T-<n>" / "scope.md"
    write = f"write {path} from {SCOPE_TEMPLATE} with the approval of the user before this security run."
    line = scope_line.strip() if scope_line is not None else None
    ids = sorted({f"T-{int(n)}" for n in TASK_REF.findall("\n".join(scope_lines_of(text)))}, key=lambda x: int(x[2:]))
    if len(ids) > 1:
        return security_verdict(con, me, tool, tid or ids[0], "many-cards", mode,
                                f"Security gate: the part \"Security scope:\" names {len(ids)} cards ({', '.join(ids)}). The rule is one card id "
                                f"for each brief: keep only the card of this run in the line and send a separate brief for each other card.",
                                None, line)
    if scope_line is not None and not tid:
        where = "the description" if agent else "the \"Security scope:\" line"
        return security_verdict(con, me, tool, tid, "no-card", mode,
                                f"Security gate: the text has the part \"Security scope:\" and no id of a card on the board. "
                                f"Put the id of a card on the board in {where} and {write}", None, line)
    if scope_line is not None and named and named != tid:
        return security_verdict(con, me, tool, tid, "other-card", mode,
                                f"Security gate: the part \"Security scope:\" names the card {named} and this run belongs to the card {tid}. "
                                f"Use the scope of {tid}: {write}", path, line)
    if scope_line is not None:
        problem = scope_problem(path)
        if problem:
            return security_verdict(con, me, tool, tid, problem[0], mode, f"Security gate: {problem[1]}. {write[0].upper()}{write[1:]}", path,
                                    line)
        return codex_note(con, me, tool, tid, mode, path) if atype == "codex" else None
    scoped = bool(tid) and scope_exists(path)
    if agent and scoped:
        if atype == "codex":
            return codex_note(con, me, tool, tid, mode, path)
        if atype in SECURITY_READERS:
            return security_note(con, me, tool, tid, "reader", mode,
                                 f"security: the card {tid} has the scope file {path}. Keep this {atype} run inside the limits of that file, "
                                 f"or add the part \"Security scope:\" to the brief.", path)
        return security_verdict(con, me, tool, tid, "no-part", mode,
                                f"Security gate: the card {tid} has the scope file {path} and the brief has no part \"Security scope:\". "
                                f"Add the part as a line {SCOPE_FORMS} with the limits from {path} to the brief.", path)
    words = security_words(text)
    if not words:
        return None
    if scoped:
        return security_note(con, me, tool, tid, "words", mode,
                             f"security: the text asks for security work ({', '.join(words)}) on the card {tid}, which has the scope file {path}, "
                             f"and has no part \"Security scope:\". Add the part as a line {SCOPE_FORMS}.", path)
    return security_note(con, me, tool, tid, "words", mode,
                         f"security: the text asks for security work ({', '.join(words)}) and has no part \"Security scope:\". "
                         f"For a security run, write {path} from {SCOPE_TEMPLATE} with the approval of the user, "
                         f"and add the part as a line {SCOPE_FORMS}.", path)


def security_verdict(con, me, tool, tid, rule, mode, text, path, line=None):
    if mode == "warn":
        return security_note(con, me, tool, tid, rule, mode, "security (warn mode, not blocked): " + text, path)
    security_event(con, me, tool, tid, rule, "deny", mode, text, path)
    return "deny", f"{short(line, SCOPE_LINE_MAX)}\n{text}" if line else text


def scope_target(raw):
    lexical = Path(os.path.normpath(os.path.abspath(os.path.expanduser(raw))))
    forms = [lexical, lexical.parent.resolve() / lexical.name, lexical.resolve(), Path(os.path.realpath(os.path.expanduser(raw)))]
    keeps = {str(KEEP.absolute()).casefold(), str(KEEP.resolve()).casefold(), os.path.normpath(str(KEEP.absolute())).casefold()}
    for p in forms:
        if p.name.casefold() == "scope.md" and re.fullmatch(r"t-\d+", p.parent.name.casefold()) and str(p.parent.parent).casefold() in keeps:
            return p
    return None


def scope_write_check(con, me, tool, ti):
    mode = security_mode()
    raw = ti.get("file_path") or ti.get("notebook_path")
    if mode == "off" or me["lead"] or not isinstance(raw, str) or not raw:
        return None
    target = scope_target(raw)
    if not target:
        return None
    return security_verdict(con, me, tool, task_ref(target.parent.name.upper()), "write", mode,
                            f"Security gate: only the lead writes the scope file {target}. Send the change that you need to the lead.", target)


def scope_bash_check(con, me, cmd):
    if me["lead"] or "scope.md" not in cmd.casefold():
        return None
    mode = security_mode()
    low = cmd.casefold()
    hit = BASH_SCOPE.search(low)
    if mode == "off" or not hit or not BASH_WRITE.search(low):
        return None
    return security_note(con, me, "Bash", task_ref(hit.group(0).upper()), "write", mode,
                         "security: this command names a scope file keep/T-<n>/scope.md and a command that can write. Only the lead writes "
                         "the scope file. If the command changes it, stop and send the change that you need to the lead.", None)
