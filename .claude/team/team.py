import argparse
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import teamlib as T

MOVES = {"start": "doing", "review": "review", "block": "blocked", "wait": "waiting_user", "done": "done",
         "drop": "dropped", "todo": "todo", "backlog": "backlog"}
LABELS = {
    "en": {"backlog": "Backlog", "todo": "To do", "doing": "In progress", "review": "In review", "blocked": "Blocked",
           "waiting_user": "Waiting for you", "done": "Done"},
    "uk": {"backlog": "Беклог", "todo": "До роботи", "doing": "В роботі", "review": "На прийомці", "blocked": "Заблоковано",
           "waiting_user": "Чекає на тебе", "done": "Готово"},
}[T.LANG]
PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(T.WORKSPACE))


def actor_of(args):
    return getattr(args, "as_", None) or os.environ.get("TEAM_AS") or "lead"


def agent_for(con, handle):
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        r = con.execute("SELECT * FROM agents WHERE handle=? AND session=? ORDER BY last_ts DESC LIMIT 1", (handle, sid)).fetchone()
        if r:
            return r
    return con.execute("SELECT * FROM agents WHERE handle=? ORDER BY (status='running') DESC, last_ts DESC LIMIT 1", (handle,)).fetchone()


def ago(ts):
    if not ts:
        return "-"
    s = int(T.now() - ts)
    return f"{s}s" if s < 90 else f"{s // 60}m" if s < 5400 else f"{s // 3600}h{(s % 3600) // 60:02d}m"


def hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def cmd_say(con, args, kind=None):
    actor = actor_of(args)
    a = agent_for(con, actor)
    text = " ".join(args.text).strip()
    if not text:
        raise SystemExit("empty message")
    kind = kind or args.kind
    task = args.task or (a["task"] if a else None)
    eid = T.emit(con, kind, actor, text, session=(a["session"] if a else None) or os.environ.get("CLAUDE_CODE_SESSION_ID"), agent_id=a["agent_id"] if a else None,
                 actor_type=a["type"] if a else ("team-lead" if actor == "lead" else None), target=args.to or "all", task=task)
    print(f"ok #{eid}")
    tgt = args.to or "all"
    if tgt not in ("all", "lead", "main", "team-lead"):
        rows = con.execute("SELECT agent_id, handle, name, status, last_ts FROM agents WHERE handle=? OR name=? OR type=? OR agent_id=? "
                           "ORDER BY last_ts DESC", (tgt, tgt, tgt, tgt)).fetchall()
        live = [r for r in rows if r["status"] == "running" and T.now() - (r["last_ts"] or 0) <= 120]
        if not rows:
            print(f"warning: no agent is called \"{tgt}\", so nobody will receive this. Check the handle with `{T.CLI} who`.")
        elif not live and any(tgt in (r["agent_id"], r["handle"], r["name"]) for r in rows):
            print(f"warning: {tgt} is idle and will not see this until it is woken. "
                  f"To ask it to act, send the same text with SendMessage to \"{tgt}\".")
        elif not live:
            names = ", ".join(dict.fromkeys(r["handle"] for r in rows[:3]))
            print(f"warning: no {tgt} is working right now, so nobody will see this until one is woken. "
                  f"SendMessage needs a handle, not a role: pick one from `{T.CLI} who` (recent: {names}).")


def cmd_task(con, args):
    actor = actor_of(args)
    if args.action == "add":
        tid = T.task_add(con, " ".join(args.rest), actor, status=args.status, feature=args.feature, owner=args.owner,
                         owner_type=args.owner_type, model=args.model, effort=args.effort, source="lead" if actor == "lead" else actor,
                         note=args.note, session=os.environ.get("CLAUDE_CODE_SESSION_ID"))
        print(tid)
    elif args.action in ("show", "note") and not args.rest:
        raise SystemExit("task id required")
    elif args.action == "show":
        tid = args.rest[0]
        r = T.get_task(con, tid)
        if not r:
            raise SystemExit(f"no such task {tid}")
        print(json.dumps(dict(r), ensure_ascii=False, indent=1))
        for e in con.execute("SELECT * FROM events WHERE task=? AND kind<>'tool' ORDER BY id", (tid,)):
            print(f"{hhmm(e['ts'])} {e['kind']:<9} {e['actor']} → {e['target'] or ''}: {T.short(e['text'], 160)}")
    elif args.action == "note":
        tid = args.rest[0]
        r = T.get_task(con, tid)
        if not r:
            raise SystemExit(f"no such task {tid}")
        T.task_move(con, tid, r["status"], actor, note=" ".join(args.rest[1:]))
        print("ok")
    elif args.action in MOVES:
        if not args.rest:
            raise SystemExit("task id required")
        T.task_move(con, args.rest[0], MOVES[args.action], actor, note=args.note, owner=args.owner, feature=args.feature,
                    model=args.model, effort=args.effort)
        print("ok")
    else:
        raise SystemExit(f"unknown action {args.action}")


def cmd_board(con, args):
    rows = con.execute("SELECT * FROM tasks ORDER BY seq").fetchall()
    day = T.now() - 86400
    for st in T.STATUSES:
        if st == "dropped":
            continue
        items = [r for r in rows if r["status"] == st and (args.all or st != "done" or (r["finished"] or 0) > day)]
        if not items:
            continue
        print(f"== {LABELS[st]} ({len(items)})")
        for r in items:
            chip = "/".join(x for x in (r["model"], r["effort"]) if x)
            print(f"  {r['id']:<6} {T.short(r['title'], 80)}  [{r['owner'] or '-'}{' ' + chip if chip else ''}]"
                  f"{' #' + r['feature'] if r['feature'] else ''}  {ago(r['updated'])}{' — ' + T.short(r['note'], 80) if r['note'] else ''}")


def cmd_feed(con, args):
    kinds = None if args.all else T.CHAT_KINDS + ["dm", "request", "spawn", "stop", "task", "overlap", "conflict", "guard", "needs_user", "ritual"]
    q = "SELECT * FROM events"
    params = []
    if kinds:
        q += f" WHERE kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    rows = con.execute(q + " ORDER BY id DESC LIMIT ?", (*params, args.n)).fetchall()
    for e in reversed(rows):
        print(f"{hhmm(e['ts'])} #{e['id']} {e['kind']:<9} {e['actor']} → {e['target'] or 'all'}"
              f"{' [' + e['task'] + ']' if e['task'] else ''}: {T.short(e['text'], 220)}")


def cmd_read(con, args):
    for i in args.ids:
        if not re.fullmatch(r"#?\d{1,18}", i):
            print(f"{i!r}: not a message id, use the number after # in team chat")
            continue
        e = con.execute("SELECT * FROM events WHERE id=?", (int(i.lstrip("#")),)).fetchone()
        if not e:
            print(f"#{i.lstrip('#')}: no such message")
            continue
        print(f"#{e['id']} {hhmm(e['ts'])} {e['kind']} {e['actor']} → {e['target'] or 'all'}"
              f"{' [' + e['task'] + ']' if e['task'] else ''}\n{e['text'] or ''}\n")


def cmd_who(con, args):
    claims = T.live_claims(con)
    for a in con.execute("SELECT * FROM agents WHERE status IN ('running','idle') ORDER BY started").fetchall():
        held = [c["rel"] or c["path"] for c in claims if c["agent_id"] == a["agent_id"]]
        print(f"{a['handle']:<28} {a['type'] or '-':<18} {a['status']:<7} {a['task'] or '-':<6} "
              f"now: {a['last_tool'] or '-'} {T.short(a['last_target'], 60)} ({ago(a['last_ts'])})")
        for h in held:
            print(f"    holds {h}")


def cmd_release(con, args):
    actor = actor_of(args)
    if args.path:
        n = con.execute("DELETE FROM claims WHERE path=? AND (actor=? OR ?)", (str(Path(args.path).expanduser().resolve()), actor, args.force)).rowcount
    else:
        n = con.execute("DELETE FROM claims WHERE actor=?", (actor,)).rowcount
    print(f"released {n}")


def cmd_dashboard(con, args):
    url = T.dashboard_url() + "/"
    pidf = T.STATE / "dashboard.pid"
    if args.action == "stop":
        pids = subprocess.run(["lsof", "-t", f"-iTCP:{T.PORT}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
        for pid in pids:
            out = subprocess.run(["ps", "-p", pid, "-o", "command="], capture_output=True, text=True).stdout
            if "dashboard/server.py" in out:
                os.kill(int(pid), signal.SIGTERM)
                print(f"stopped {pid}")
        pidf.unlink(missing_ok=True)
        return
    if args.action in ("start", "open"):
        started = T.ensure_dashboard()
        for _ in range(20):
            if T.port_open():
                break
            time.sleep(0.1)
        print(("started " if started else "running ") + url)
        if args.action == "open":
            webbrowser.open(url)
        return
    print(("up " if T.port_open() else "down ") + url)


def cmd_ritual(con, args):
    path = Path(args.path).expanduser()
    T.emit(con, "ritual", "lead", " ".join(args.summary) or f"{args.kind} {path.stem}", actor_type="team-lead",
           data={"kind": args.kind, "path": str(path), "date": path.stem})
    print("ok")


def since_ts(hours):
    return T.now() - hours * 3600


def cmd_digest(con, args):
    t0 = since_ts(args.hours)
    print(f"# Digest since {time.strftime('%F %H:%M', time.localtime(t0))}")
    moved = con.execute("SELECT * FROM events WHERE kind='task' AND ts>? ORDER BY id", (t0,)).fetchall()
    done = [e for e in moved if json.loads(e["data"] or "{}").get("to") == "done"]
    print(f"\n## Done ({len(done)})")
    for e in done:
        print(f"- {e['task']} {json.loads(e['data'])['title']}")
    print("\n## Open now")
    for r in con.execute(f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(T.OPEN_STATUSES))}) ORDER BY seq", T.OPEN_STATUSES):
        print(f"- {r['id']} [{r['status']}] {r['title']} — {r['owner'] or '-'}{' — ' + r['note'] if r['note'] else ''}")
    print("\n## Agent runs")
    for e in con.execute("SELECT * FROM events WHERE kind='stop' AND ts>? ORDER BY id", (t0,)):
        d = json.loads(e["data"] or "{}")
        print(f"- {e['actor']} ({e['actor_type']}) {e['task'] or ''} {d.get('duration_s', 0) // 60}m"
              f"{' ' + d['verdict'] if d.get('verdict') else ''}: {T.short(e['text'], 160)}")
    print("\n## Decisions, blockers, asks")
    for e in con.execute("SELECT * FROM events WHERE kind IN ('decision','blocker','ask','handoff') AND ts>? ORDER BY id", (t0,)):
        print(f"- {hhmm(e['ts'])} {e['kind']} {e['actor']} → {e['target']}: {T.short(e['text'], 200)}")
    print("\n## Coordination incidents")
    for e in con.execute("SELECT * FROM events WHERE kind IN ('conflict','overlap','guard','needs_user') AND ts>? ORDER BY id", (t0,)):
        print(f"- {hhmm(e['ts'])} {e['kind']} {e['actor']}: {T.short(e['text'], 200)}")
    print("\n## User requests")
    for e in con.execute("SELECT * FROM events WHERE kind='request' AND ts>? ORDER BY id", (t0,)):
        print(f"- {hhmm(e['ts'])} {T.short(e['text'], 240)}")


def cmd_stats(con, args):
    t0 = since_ts(args.days * 24)
    stops = con.execute("SELECT * FROM events WHERE kind='stop' AND ts>? ORDER BY id", (t0,)).fetchall()
    spawns = con.execute("SELECT * FROM events WHERE kind='spawn' AND ts>?", (t0,)).fetchall()
    last_stop, turns = {}, Counter()
    for e in stops:
        last_stop[(e["actor_type"], e["agent_id"] or e["actor"])] =json.loads(e["data"] or "{}")
        turns[e["actor_type"]] += 1
    by_type = defaultdict(list)
    for (atype, _), d in last_stop.items():
        by_type[atype].append(d)
    mix = Counter()
    tagged = 0
    for e in spawns:
        d = json.loads(e["data"] or "{}")
        mix[f"{d.get('type')} {d.get('model') or 'default'}/{d.get('effort') or 'default'}"] += 1
        tagged += bool(d.get("model") and d.get("effort"))
    per_task = Counter(e["task"] for e in spawns if json.loads(e["data"] or "{}").get("type") not in T.AUTO_DONE_TYPES and e["task"])
    verdicts = Counter((e["actor_type"], json.loads(e["data"] or "{}").get("verdict")) for e in stops if e["actor_type"] in ("reviewer", "codex"))
    done = con.execute("SELECT * FROM tasks WHERE finished>? AND status='done'", (t0,)).fetchall()
    cycle = [(r["finished"] - (r["started"] or r["created"])) / 60 for r in done]
    kinds = Counter(r["kind"] for r in con.execute("SELECT kind FROM events WHERE ts>?", (t0,)))
    fc = Counter(r["verdict"] for r in con.execute("SELECT verdict FROM factchecks WHERE ts>?", (t0,)))
    blocked = con.execute("SELECT COUNT(*) FROM events WHERE kind='guard' AND ts>? AND json_valid(data) AND json_extract(data,'$.gate')='factcheck' "
                          "AND (json_extract(data,'$.hash') IS NOT NULL OR json_extract(data,'$.problem') IS NOT NULL)",
                          (t0,)).fetchone()[0]
    asks = con.execute("SELECT * FROM events WHERE kind='ask' AND ts>?", (t0,)).fetchall()
    answered = sum(1 for a in asks if con.execute(
        "SELECT 1 FROM events WHERE kind='answer' AND id>? AND target=? LIMIT 1", (a["id"], a["actor"])).fetchone())
    out = {
        "window_days": args.days,
        "tasks_done": len(done),
        "cycle_minutes_median": round(statistics.median(cycle), 1) if cycle else None,
        "open_by_status": dict(Counter(r["status"] for r in con.execute("SELECT status FROM tasks WHERE status NOT IN ('done','dropped')"))),
        "spawns": len(spawns), "spawns_with_model_effort": tagged,
        "runs_by_type": {k: {"runs": len(v), "turns": turns[k], "median_min": round(statistics.median([x.get("duration_s", 0) for x in v]) / 60, 1),
                             "median_tools": statistics.median([x.get("tools") or 0 for x in v])} for k, v in by_type.items()},
        "model_effort_mix": dict(mix.most_common()),
        "rework_tasks": {k: v for k, v in per_task.items() if v > 1},
        "review_verdicts": {f"{a}:{v}": n for (a, v), n in verdicts.items()},
        "asks": len(asks), "asks_answered": answered,
        "coordination": {k: kinds.get(k, 0) for k in ("conflict", "overlap", "guard", "needs_user", "blocker", "decision")},
        "chat_messages": sum(kinds.get(k, 0) for k in T.CHAT_KINDS + ["dm"]),
        "tool_calls": kinds.get("tool", 0),
        "factcheck": {"pass": fc["PASS"], "fail": fc["FAIL"], "waived": fc["WAIVED"], "blocked": blocked},
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


def fact_text(path):
    try:
        return sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(errors="replace")
    except OSError as e:
        raise SystemExit(f"cannot read {path}: {e.strerror}")


def cmd_factcheck(con, args):
    actor = actor_of(args)
    if args.action == "status" and not args.file:
        for r in con.execute("SELECT * FROM factchecks ORDER BY id DESC LIMIT 20").fetchall()[::-1]:
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))} {r['hash']} {r['verdict']:<7} {r['checker'] or '-'}: "
                  f"{T.short(r['note'] or r['text_head'], 100)}")
        return
    if not args.file:
        raise SystemExit("file required (- reads stdin for tokens)")
    if args.file == "-" and args.action != "tokens":
        raise SystemExit("save the text to a file: the record must point at the exact text")
    text = fact_text(args.file)
    h, toks = T.fact_hash(text), T.fact_tokens(text)
    if toks is None and args.action in ("tokens", "pass"):
        raise SystemExit(f"hash {h}: the text is longer than {T.FACT_MAX} characters; the gate does not check it. "
                         f"Split it, or the lead waives it with `factcheck waive <file> --as lead --reason ...`")
    if args.action == "tokens":
        if args.json:
            print(json.dumps([{"token": t["text"], "claim": "", "source": "", "observed": "", "source_kind": ""} for t in toks],
                             ensure_ascii=False, indent=1))
            return
        print(f"hash {h}")
        print(f"{len(toks)} checkable facts" if toks else "no checkable facts: no fact-check needed")
        for t in toks:
            print(f"- {t['kind']}: {t['text']}")
        for p in T.fact_calendar(text):
            print(f"! {p}")
        return
    if args.action == "status":
        print(f"hash {h}, {len(toks) if toks is not None else 'too long to scan, no'} checkable facts")
        for r in con.execute("SELECT * FROM factchecks WHERE hash=? ORDER BY id", (h,)):
            live = r["ts"] > T.now() - T.FACT_TTL
            print(f"{time.strftime('%F %H:%M', time.localtime(r['ts']))} {r['verdict']} by {r['checker'] or '-'}"
                  f"{'' if live else ' (expired)'}{': ' + r['note'] if r['note'] else ''}")
        return
    a = agent_for(con, actor)
    who = dict(checker=actor, checker_type=a["type"] if a else ("team-lead" if actor == "lead" else None),
               agent_id=a["agent_id"] if a else None, session=(a["session"] if a else None) or os.environ.get("CLAUDE_CODE_SESSION_ID"))
    if args.action == "pass":
        if not args.as_ or actor == "lead" or not a or a["type"] not in T.FACT_CHECKERS:
            raise SystemExit("refused: only a reviewer or codex agent records a PASS, with an explicit --as <its handle>")
        if T.authored(con, a["agent_id"], args.file, text):
            raise SystemExit(f"refused: {actor} wrote or edited this text; another reviewer or codex agent has to check it")
        if not toks:
            print("no checkable facts: nothing to record")
            return
        if not args.claims:
            raise SystemExit("refused: --claims <claims.json> is required")
        try:
            claims = json.loads(Path(args.claims).expanduser().read_text())
        except (OSError, ValueError) as e:
            raise SystemExit(f"refused: cannot read claims: {e}")
        problems = T.fact_problems(text, claims)
        if problems:
            print(f"refused PASS for {h}:", file=sys.stderr)
            for p in problems:
                print(f"- {p}", file=sys.stderr)
            raise SystemExit(1)
        T.fact_record(con, text, "PASS", claims=claims, **who)
        print(f"PASS recorded for {h} ({len(toks)} facts), valid 24h")
    elif args.action == "fail":
        if not args.note:
            raise SystemExit("--note required: what was wrong")
        T.fact_record(con, text, "FAIL", note=args.note, **who)
        print(f"FAIL recorded for {h}")
    elif args.action == "waive":
        if args.as_ != "lead":
            raise SystemExit("refused: only the lead waives a fact-check, with an explicit --as lead")
        if not args.reason:
            raise SystemExit("--reason required")
        T.fact_record(con, text, "WAIVED", note=args.reason, **who)
        T.emit(con, "guard", actor, T.msg("guard.waived", h=h, reason=T.short(args.reason, 200)), session=who["session"],
               agent_id=who["agent_id"], actor_type="team-lead", data={"gate": "factcheck", "waived": h, "reason": args.reason})
        print(f"WAIVED recorded for {h}, valid 24h")


def user_text(d):
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str):
        return None
    c = c.strip()
    if not c or c.startswith(("<", "[Request interrupted", "Another Claude session sent")):
        return None
    return c


def stamp(d):
    try:
        return datetime.fromisoformat(d.get("timestamp", "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def cmd_transcripts(con, args):
    t0 = since_ts(args.hours)
    for f in sorted(PROJECTS.glob("*.jsonl"), key=os.path.getmtime):
        if os.path.getmtime(f) < t0:
            continue
        msgs = []
        for line in open(f, errors="replace"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            txt = user_text(d)
            if txt and stamp(d) >= t0:
                msgs.append((d.get("timestamp", "")[:16], txt))
        if msgs:
            print(f"## session {f.stem[:8]} ({len(msgs)} user messages)")
            for ts, txt in msgs:
                print(f"- {ts} {T.short(txt, args.width)}")


def cmd_vacuum(con, args):
    t0 = since_ts(args.days * 24)
    n = con.execute("DELETE FROM events WHERE kind='tool' AND ts<?", (t0,)).rowcount
    m = con.execute("DELETE FROM seen WHERE ts<?", (t0,)).rowcount
    con.execute("DELETE FROM spawns WHERE ts<?", (t0,))
    con.execute("VACUUM")
    print(f"dropped {n} tool events and {m} dedupe keys older than {args.days:g} days")


def main():
    p = argparse.ArgumentParser(prog="team", description="Team chat, board and rituals for the Claude dev team")
    sub = p.add_subparsers(dest="cmd", required=True)

    def chat_parser(name, help_):
        s = sub.add_parser(name, help=help_)
        s.add_argument("text", nargs="+")
        s.add_argument("--as", dest="as_")
        s.add_argument("--to", help="handle, role or all (default all)")
        s.add_argument("--task")
        return s

    s = chat_parser("say", "post to team chat")
    s.add_argument("--kind", default="say", choices=T.CHAT_KINDS)
    for k in ("ask", "answer", "decision", "blocker", "handoff", "report"):
        chat_parser(k, f"post a {k}")

    s = sub.add_parser("task", help="board cards: add|show|note|start|review|block|wait|done|drop|todo|backlog")
    s.add_argument("action")
    s.add_argument("rest", nargs="*")
    s.add_argument("--as", dest="as_")
    s.add_argument("--status", default="todo", choices=T.STATUSES)
    s.add_argument("--feature")
    s.add_argument("--owner")
    s.add_argument("--owner-type")
    s.add_argument("--model")
    s.add_argument("--effort")
    s.add_argument("--note")

    s = sub.add_parser("board", help="print the board")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("feed", help="recent team chat and events")
    s.add_argument("-n", type=int, default=40)
    s.add_argument("--all", action="store_true", help="include tool activity")
    s = sub.add_parser("read", help="print team chat or DM messages in full by id")
    s.add_argument("ids", nargs="+")
    sub.add_parser("who", help="running agents, what they do and which files they hold")
    s = sub.add_parser("release", help="release your file claims")
    s.add_argument("--as", dest="as_")
    s.add_argument("--path")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("dashboard", help="start|stop|status|open")
    s.add_argument("action", nargs="?", default="status", choices=["start", "stop", "status", "open"])
    s = sub.add_parser("ritual", help="record a written ritual note")
    s.add_argument("kind", choices=["daily", "retro", "planning", "reflect"])
    s.add_argument("path")
    s.add_argument("summary", nargs="*")
    s = sub.add_parser("digest", help="what happened in the last N hours")
    s.add_argument("--hours", type=float, default=24)
    s = sub.add_parser("stats", help="team metrics for the retro")
    s.add_argument("--days", type=float, default=7)
    s = sub.add_parser("vacuum", help="drop old tool-activity rows")
    s.add_argument("--days", type=float, default=14)
    s = sub.add_parser("factcheck", help="fact-check ledger for outward text: tokens|pass|fail|waive|status")
    s.add_argument("action", choices=["tokens", "pass", "fail", "waive", "status"])
    s.add_argument("file", nargs="?")
    s.add_argument("--as", dest="as_")
    s.add_argument("--claims")
    s.add_argument("--note")
    s.add_argument("--reason")
    s.add_argument("--json", action="store_true", help="tokens: print a claims.json template")
    s = sub.add_parser("transcripts", help="the user's own messages from recent sessions")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--width", type=int, default=400)

    args = p.parse_args()
    con = T.connect()
    try:
        if args.cmd == "say":
            cmd_say(con, args)
        elif args.cmd in ("ask", "answer", "decision", "blocker", "handoff", "report"):
            cmd_say(con, args, kind=args.cmd)
        else:
            {"task": cmd_task, "board": cmd_board, "feed": cmd_feed, "read": cmd_read, "who": cmd_who, "release": cmd_release,
             "dashboard": cmd_dashboard, "ritual": cmd_ritual, "digest": cmd_digest, "stats": cmd_stats,
             "transcripts": cmd_transcripts, "vacuum": cmd_vacuum, "factcheck": cmd_factcheck}[args.cmd](con, args)
    finally:
        con.close()


if __name__ == "__main__":
    main()
