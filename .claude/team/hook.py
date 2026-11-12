import json
import re
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import teamlib as T

SKIP_CLAIM_PREFIXES = ("/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/")
LEAD_NAMES = {"lead", "main", "team-lead"}


def ctx(event, text):
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}


def norm_target(to):
    to = to.strip() if isinstance(to, str) else ""
    return "lead" if to in LEAD_NAMES else to


def hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "?"


def target_of(tool, ti):
    if tool == "Bash":
        return T.short(ti.get("description") or ti.get("command"), 120)
    if tool in ("Read", "Edit", "Write", "MultiEdit"):
        return ti.get("file_path")
    if tool == "NotebookEdit":
        return ti.get("notebook_path")
    if tool in ("Grep", "Glob"):
        return T.short(ti.get("pattern"), 80) + (f" in {ti['path']}" if ti.get("path") else "")
    if tool == "WebFetch":
        return T.short(ti.get("url"), 100)
    if tool == "WebSearch":
        return T.short(ti.get("query"), 100)
    if tool == "Agent":
        return f"{ti.get('subagent_type') or 'general-purpose'}: {T.short(ti.get('description'), 80)}"
    if tool == "SendMessage":
        return f"→ {ti.get('to')}"
    if tool == "Skill":
        return ti.get("skill")
    return ""


def ensure_row(con, me, inp):
    row = T.agent_row(con, me["agent_id"])
    if row or not (me["lead"] or me["type"]):
        return row
    t = T.now()
    con.execute(
        "INSERT OR IGNORE INTO agents(agent_id,session,handle,name,type,status,started,last_ts,cursor) VALUES(?,?,?,?,?,?,?,?,?)",
        (me["agent_id"], me["session"], me["handle"], None, me["type"], "running", t, t, T.max_event(con)),
    )
    return T.agent_row(con, me["agent_id"])


def touch(con, me, inp, tool, ti):
    ensure_row(con, me, inp)
    target = target_of(tool, ti)
    con.execute(
        "UPDATE agents SET last_ts=?, last_tool=?, last_target=?, tools=COALESCE(tools,0)+1, status='running', stopped=NULL WHERE agent_id=?",
        (T.now(), tool, target, me["agent_id"]),
    )
    T.emit(con, "tool", me["handle"], target, session=me["session"], agent_id=me["agent_id"], actor_type=me["type"],
           target=tool, task=me.get("task"))


def on_spawn(con, me, inp, ti):
    atype = ti.get("subagent_type") or "general-purpose"
    name = ti.get("name")
    desc = ti.get("description") or atype
    prompt = ti.get("prompt") or ""
    model, effort = ti.get("model"), ti.get("effort")
    tid = T.task_ref(desc, prompt[:300])
    if not T.get_task(con, tid):
        tid = None
    con.execute(
        "INSERT OR REPLACE INTO spawns(tool_use_id,session,name,type,description,model,effort,task,ts,prompt_head) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (inp.get("tool_use_id") or f"anon-{T.now()}", me["session"], name, atype, desc, model, effort, tid, T.now(), T.norm(prompt)[:200]),
    )
    T.emit(con, "spawn", me["handle"], desc, session=me["session"], agent_id=me["agent_id"], actor_type=me["type"],
           target=name or atype, task=tid,
           data={"type": atype, "name": name, "model": model, "effort": effort, "isolation": ti.get("isolation"),
                 "background": ti.get("run_in_background"), "brief": prompt[:1500]})


def on_dm(con, me, ti):
    msg = ti.get("message")
    if isinstance(msg, dict):
        text = f"[{msg.get('type')}] {msg.get('reason') or msg.get('feedback') or ''}".strip()
    else:
        text = str(msg or "")
    if not text and not ti.get("notify_when_idle"):
        return
    T.emit(con, "dm", me["handle"], text, session=me["session"], agent_id=me["agent_id"], actor_type=me["type"],
           target=norm_target(ti.get("to")), task=T.task_ref(text[:300]) or me.get("task"))


def on_edit(con, me, ti):
    raw = ti.get("file_path") or ti.get("notebook_path")
    if not raw:
        return None, []
    path = str(Path(raw).expanduser().resolve())
    if path.startswith(SKIP_CLAIM_PREFIXES):
        return None, []
    gi = T.git_info(path) or {}
    con.execute("BEGIN IMMEDIATE")
    try:
        return edit_claim(con, me, path, gi)
    finally:
        con.execute("COMMIT")


def edit_claim(con, me, path, gi):
    claims = T.live_claims(con)
    notes = []
    key = path.casefold()
    holders = [c for c in claims if c["path"].casefold() == key and c["agent_id"] != me["agent_id"]]
    hard = [c for c in holders if not c["agent_id"].startswith("lead:")]
    for h in [c for c in holders if c["agent_id"].startswith("lead:")]:
        notes.append(f"Heads-up: the lead touched {path} at {hhmm(h['touched'])}; keep your change focused and say what you changed.")
    if hard:
        h = hard[0]
        if T.once(con, f"conflict:{me['agent_id']}:{h['agent_id']}:{path}"):
            T.emit(con, "conflict", me["handle"], T.msg("conflict", path=gi.get("rel") or path, holder=h["actor"], since=hhmm(h["since"])),
                   session=me["session"], agent_id=me["agent_id"], actor_type=me["type"], target=h["actor"],
                   task=me.get("task"), data={"path": path, "holder": h["actor"], "holder_task": h["task"]})
        if not me["lead"]:
            return (f"Team claim conflict: {path} is being edited by {h['actor']} (task {h['task'] or '-'}) since {hhmm(h['since'])}. "
                    f"Do not edit it in parallel. Ask them or the lead first: {T.CLI} say --as {me['handle']} --to {h['actor']} --kind ask \"...\", "
                    f"or work in your own worktree."), []
        notes.append(f"Heads-up: {h['actor']} holds {path} (task {h['task'] or '-'}). Coordinate before editing it under them.")
    if gi:
        for o in claims:
            if o["repo"] == gi["repo"] and (o["rel"] or "").casefold() == gi["rel"].casefold() and o["path"].casefold() != key and o["agent_id"] != me["agent_id"]:
                pair = "|".join(sorted([me["agent_id"], o["agent_id"]]))
                if T.once(con, f"overlap:{pair}:{gi['repo']}:{gi['rel']}"):
                    T.emit(con, "overlap", me["handle"],
                           T.msg("overlap", where=f"{Path(gi['worktree']).name}:{gi['rel']}", me=me["handle"], branch=gi["branch"], other=o["actor"], other_branch=o["branch"]),
                           session=me["session"], agent_id=me["agent_id"], actor_type=me["type"], target=o["actor"],
                           task=me.get("task"), data={"rel": gi["rel"], "repo": gi["repo"], "other": o["actor"], "other_worktree": o["worktree"]})
                notes.append(f"Heads-up: {o['actor']} is also changing {gi['rel']} in {o['worktree']} (branch {o['branch']}). "
                             f"Coordinate in team chat to avoid a merge conflict.")
    t = T.now()
    con.execute(
        "INSERT INTO claims(path,agent_id,actor,session,repo,rel,worktree,branch,task,since,touched) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(path,agent_id) DO UPDATE SET touched=excluded.touched, task=COALESCE(excluded.task, claims.task)",
        (path, me["agent_id"], me["handle"], me["session"], gi.get("repo"), gi.get("rel"), gi.get("worktree"), gi.get("branch"),
         me.get("task"), t, t),
    )
    return None, notes


def mutating(sub, rest):
    if sub == "checkout" and re.fullmatch(r"\s*-[bB]\s+\S+\s*", rest):
        return False
    if sub == "switch" and re.fullmatch(r"\s*-[cC]\s+\S+\s*", rest):
        return False
    if sub == "clean":
        return not re.search(r"(^|\s)(-[a-z]*n[a-z]*|--dry-run)\b", rest)
    if sub in ("checkout", "switch", "rebase", "pull", "merge", "cherry-pick"):
        return True
    if sub == "stash":
        return not re.match(r"\s*(list|show)\b", rest)
    if sub == "reset":
        return bool(re.search(r"--(hard|merge|keep)\b", rest))
    if sub == "restore":
        staged = re.search(r"(^|\s)(--staged|-[a-zA-Z]*S[a-zA-Z]*)\b", rest)
        worktree = re.search(r"(^|\s)(--worktree|-[a-zA-Z]*W[a-zA-Z]*)\b", rest)
        return bool(worktree or not staged)
    return False


def git_targets(cmd, cwd):
    here = Path(cwd) if cwd else None
    out = []
    for seg in T.segments(cmd):
        if seg[0] == "cd" and len(seg) == 2:
            p = Path(seg[1]).expanduser()
            here = p if p.is_absolute() or not here else here / p
            continue
        if seg[0] != "git":
            continue
        i, gdir = 1, None
        while i < len(seg) and seg[i].startswith("-"):
            if seg[i].startswith("--work-tree="):
                gdir = seg[i].split("=", 1)[1]
                i += 1
                continue
            if seg[i] in ("-C", "-c", "--git-dir", "--work-tree") and i + 1 < len(seg):
                if seg[i] in ("-C", "--work-tree"):
                    gdir = seg[i + 1]
                i += 2
            else:
                i += 1
        if i >= len(seg) or not mutating(seg[i], " " + " ".join(seg[i + 1:])):
            continue
        d = Path(gdir).expanduser() if gdir else here
        if d is not None and not d.is_absolute() and here:
            d = here / d
        if d is not None:
            out.append(d)
    return out


CHAT_VERBS = {"say", "ask", "answer", "decision", "blocker", "handoff", "report"}


def chat_identity(cmd, handle):
    if "team.py" not in cmd:
        return None
    for seg in T.segments(cmd):
        if not seg[0].startswith("python"):
            continue
        idx = [i for i, t in enumerate(seg) if t.endswith("team.py")]
        if not idx or idx[0] + 1 >= len(seg) or seg[idx[0] + 1] not in CHAT_VERBS:
            continue
        rest = seg[idx[0] + 1:]
        who = None
        for i, t in enumerate(rest):
            if t == "--as":
                who = rest[i + 1] if i + 1 < len(rest) else ""
            elif t.startswith("--as="):
                who = t[5:]
        if who is None:
            return f"Team chat: add `--as {handle}` so the team sees who is talking."
        if who != handle:
            return f"Team chat: post as yourself, `--as {handle}`."
    return None


def on_bash(con, me, ti, cwd):
    cmd = ti.get("command") or ""
    if "git" not in cmd:
        return None
    for d in git_targets(cmd, cwd):
        gi = T.git_info(d) if d.exists() else None
        if not gi:
            continue
        others = [c for c in T.live_claims(con) if c["worktree"] == gi["worktree"] and c["agent_id"] != me["agent_id"]]
        if not others:
            continue
        names = ", ".join(sorted({c["actor"] for c in others}))
        if T.once(con, f"guard:{me['agent_id']}:{gi['worktree']}:{int(T.now() // 600)}"):
            T.emit(con, "guard", me["handle"], T.msg("guard.git", wt=Path(gi["worktree"]).name, branch=gi["branch"], names=names),
                   session=me["session"], agent_id=me["agent_id"], actor_type=me["type"], target=names, task=me.get("task"),
                   data={"command": T.short(cmd, 200), "worktree": gi["worktree"]})
        return (f"Team guard: {names} have edits in progress in {gi['worktree']} (branch {gi['branch']}). "
                f"checkout/switch/stash/reset --hard/rebase/pull/merge/clean/restore here would change files under them. "
                f"Use your own worktree (git worktree add) or ask them first: {T.CLI} say --as {me['handle']} --to <handle> --kind ask \"...\".")
    return None


def inbox(con, me):
    con.execute("BEGIN IMMEDIATE")
    try:
        return read_inbox(con, me)
    finally:
        con.execute("COMMIT")


STOP_KINDS = ("ask", "handoff", "blocker", "decision")


def my_names(me, row):
    return {me["handle"]} | (LEAD_NAMES if me["lead"] else {row["name"] or "", row["type"] or "", me["agent_id"]} - {""})


def direct_names(me, row):
    return sorted({me["handle"], row["name"] or "", me["agent_id"]} - {""})


def pending_rows(con, me, row, top, limit):
    names = my_names(me, row)
    direct = direct_names(me, row)
    marks = ",".join("?" * len(T.INBOX_KINDS))
    tmarks = ",".join("?" * len(names))
    dmarks = ",".join("?" * len(direct))
    dm = "" if me["lead"] else f"OR (kind='dm' AND target IN ({dmarks}) AND session IS ?) "
    return con.execute(
        f"SELECT * FROM events WHERE id>? AND id<=? AND COALESCE(actor,'')<>? "
        f"AND ((kind IN ({marks}) AND (((target IS NULL OR target IN ('', 'all', 'lead', 'main', 'team-lead')) AND session IS ?) "
        f"OR (target IN ({tmarks}) AND target NOT IN ('lead', 'main', 'team-lead')))) {dm}) ORDER BY id LIMIT ?",
        (row["cursor"] or 0, top, me["handle"], *T.INBOX_KINDS, me["session"], *names, *([] if me["lead"] else [*direct, me["session"]]), limit),
    ).fetchall()


def turn_start(con, me):
    if me["lead"]:
        return 0
    r = con.execute("SELECT MAX(id) FROM events WHERE kind='start' AND agent_id=?", (me["agent_id"],)).fetchone()
    return r[0] or 0


def chat_line(r):
    how = "DM; the same message arrives again natively when your turn ends, act on it once" if r["kind"] == "dm" else r["kind"]
    return f"[team chat #{r['id']}] {r['actor']} → {r['target'] or 'all'} ({how}{', ' + r['task'] if r['task'] else ''}): {(r['text'] or '').strip()}"


def read_inbox(con, me):
    row = T.agent_row(con, me["agent_id"])
    if not row:
        return None
    top = T.max_event(con)
    rows = pending_rows(con, me, row, top, 200)
    floor = turn_start(con, me)
    upto = rows[-1]["id"] if len(rows) == 200 else top
    reply = f"Reply only if you have something useful: {T.CLI} say --as {me['handle']} --to <handle> --kind answer \"...\""
    lines, used = [], len(reply)
    for r in rows:
        if (r["target"] in LEAD_NAMES and not me["lead"]) or '"backfill": true' in (r["data"] or "") or (r["kind"] == "dm" and r["id"] < floor):
            continue
        line = chat_line(r)
        if used + len(line) + 1 > T.INBOX_BUDGET:
            if lines:
                upto = r["id"] - 1
                break
            tail = f"… [cut at {{}} of {len(line)} chars; read the rest: {T.CLI} read {r['id']}]"
            keep = T.INBOX_BUDGET - used - 1 - len(tail.format(T.INBOX_BUDGET))
            line = line[:keep] + tail.format(keep)
        lines.append(line)
        used += len(line) + 1
    con.execute("UPDATE agents SET cursor=MAX(COALESCE(cursor,0), ?) WHERE agent_id=?", (upto, me["agent_id"]))
    if not lines:
        return None
    return "\n".join(lines + [reply])


def stop_block(con, inp, row):
    me = T.identity(con, inp)
    if inp.get("stop_hook_active") or not row["name"] or row["last_tool"] == "SubagentHandback" or me["lead"]:
        return None
    direct = direct_names(me, row)
    marks = ",".join("?" * len(direct))
    kmarks = ",".join("?" * len(STOP_KINDS))
    hit = con.execute(
        f"SELECT id FROM events WHERE id>? AND kind IN ({kmarks}) AND target IN ({marks}) AND session IS ? AND COALESCE(actor,'')<>? ORDER BY id LIMIT 1",
        (row["cursor"] or 0, *STOP_KINDS, *direct, me["session"], me["handle"]),
    ).fetchone()
    if not hit or not T.once(con, f"stopblock:{me['agent_id']}:{turn_start(con, me)}"):
        return None
    box = inbox(con, me) or ""
    if (T.agent_row(con, me["agent_id"])["cursor"] or 0) < hit["id"]:
        box += f"\nMore team chat for you is queued; it arrives on your next tool call. The one that stopped you: {T.CLI} read {hit['id']}"
    return {"decision": "block", "reason": "Team chat addressed to you arrived before you stopped. Read it and act if it asks you to:\n" + box.strip()}


def pre_tool(con, inp):
    me = T.identity(con, inp)
    tool = inp.get("tool_name") or ""
    ti = inp.get("tool_input") if isinstance(inp.get("tool_input"), dict) else {}
    touch(con, me, inp, tool, ti)
    notes = [n for n in [relink(con, inp, me)] if n]
    if tool == "Agent":
        on_spawn(con, me, inp, ti)
    elif tool == "SubagentHandback" and not me["lead"]:
        con.execute("UPDATE agents SET result=? WHERE agent_id=?", (str(ti.get("message") or "")[:4000], me["agent_id"]))
    elif tool == "AskUserQuestion":
        qs = ti.get("questions") or []
        text = "; ".join(str(q.get("question") or "") for q in qs if isinstance(q, dict)) or T.msg("ask.agent")
        T.emit(con, "needs_user", me["handle"], T.short(text, 300), session=me["session"], agent_id=me["agent_id"], actor_type=me["type"],
               task=me.get("task"), data={"message": text, "type": "ask_user_question"})
    elif tool == "SendMessage":
        on_dm(con, me, ti)
    elif tool in T.EDIT_TOOLS:
        T.fail_open(con, me, T.note_authored, tool, ti)
        reason, more = on_edit(con, me, ti)
        if reason:
            return deny(reason)
        notes += more
    elif tool == "Bash":
        cmd = ti.get("command") or ""
        bad = None if me["lead"] else chat_identity(cmd, me["handle"])
        if bad:
            return deny(bad)
        reason = T.fail_open(con, me, T.bash_facts, cmd, inp.get("cwd"))
        if reason:
            return deny(reason)
        reason = on_bash(con, me, ti, inp.get("cwd"))
        if reason:
            return deny(reason)
    elif tool.startswith("mcp__"):
        reason = T.fail_open(con, me, T.mcp_facts, tool, ti)
        if reason:
            return deny(reason)
    box = inbox(con, me)
    if box:
        notes.append(box)
    return ctx("PreToolUse", "\n".join(notes)) if notes else None


def resolve(con, inp, aid):
    session = inp.get("session_id")
    meta = T.meta_for(inp, aid)
    name = meta.get("name")
    atype = meta.get("customAgentType") or meta.get("spawnedAgentType") or inp.get("agent_type") or meta.get("agentType")
    sp, how = None, None
    if meta.get("toolUseId"):
        sp = con.execute("SELECT * FROM spawns WHERE tool_use_id=?", (meta["toolUseId"],)).fetchone()
        how = "meta" if sp else None
    if not sp:
        prompt = T.norm(T.first_prompt(inp, aid))
        if prompt:
            hits = [r for r in con.execute("SELECT * FROM spawns WHERE session=? AND agent_id IS NULL AND prompt_head<>''", (session,))
                    if r["prompt_head"] in prompt]
            if len(hits) == 1:
                sp, how = hits[0], "prompt"
    known_types = {r[0] for r in con.execute("SELECT DISTINCT type FROM spawns WHERE session=?", (session,))}
    if not sp:
        for n in filter(None, [name, inp.get("agent_type") if inp.get("agent_type") not in known_types else None]):
            sp = con.execute("SELECT * FROM spawns WHERE session=? AND name=? AND (agent_id IS NULL OR agent_id=?) ORDER BY ts DESC LIMIT 1",
                             (session, n, aid)).fetchone()
            if sp:
                how = "name"
                break
    if not sp:
        rows = con.execute("SELECT * FROM spawns WHERE session=? AND type=? AND agent_id IS NULL AND ts>?",
                           (session, atype, T.now() - 300)).fetchall()
        if len(rows) == 1:
            sp, how = rows[0], "unique"
    if sp:
        name = name or sp["name"]
        atype = sp["type"] or atype
    return sp, name, atype, meta, how


def ensure_card(con, sp, name, atype, meta):
    if not sp:
        return sp
    model = meta.get("model") or sp["model"]
    effort = meta.get("effort") or sp["effort"]
    card = T.get_task(con, sp["task"])
    if card:
        if card["status"] in ("todo", "backlog", "blocked", "review") and (atype not in T.AUTO_DONE_TYPES or card["status"] in ("todo", "backlog")):
            T.task_move(con, card["id"], "doing", "lead", session=sp["session"], owner=name or atype, owner_type=atype, model=model, effort=effort)
        return sp
    if sp["agent_id"]:
        return sp
    tid = T.task_add(con, sp["description"] or atype, "lead", status="doing", owner=name or atype, owner_type=atype, model=model,
                     effort=effort, session=sp["session"], source="spawn")
    con.execute("UPDATE spawns SET task=? WHERE tool_use_id=?", (tid, sp["tool_use_id"]))
    return con.execute("SELECT * FROM spawns WHERE tool_use_id=?", (sp["tool_use_id"],)).fetchone()


def apply_link(con, aid, sp, name, atype, meta, how):
    sp = ensure_card(con, sp, name, atype, meta)
    row = T.agent_row(con, aid)
    handle = T.handle_for(name, atype, aid)
    model = meta.get("model") or (sp["model"] if sp else None)
    effort = meta.get("effort") or (sp["effort"] if sp else None)
    desc = meta.get("description") or (sp["description"] if sp else None)
    task = sp["task"] if sp else None
    con.execute("BEGIN IMMEDIATE")
    try:
        return link_rows(con, aid, row, sp, handle, name, atype, model, effort, desc, task, how)
    finally:
        con.execute("COMMIT")


def link_rows(con, aid, row, sp, handle, name, atype, model, effort, desc, task, how):
    t = T.now()
    if sp and con.execute("UPDATE spawns SET agent_id=? WHERE tool_use_id=? AND (agent_id IS NULL OR agent_id=?)",
                          (aid, sp["tool_use_id"], aid)).rowcount == 0:
        return (row["handle"] if row else handle), None
    con.execute("UPDATE agents SET handle=?, name=?, type=?, model=COALESCE(?,model), effort=COALESCE(?,effort), "
                "description=COALESCE(?,description), task=COALESCE(?,task), linked=? WHERE agent_id=?",
                (handle, name, atype, model, effort, desc, task, how, aid))
    if row and row["handle"] != handle:
        con.execute("UPDATE events SET actor=?, actor_type=? WHERE agent_id=?", (handle, atype, aid))
        con.execute("UPDATE events SET target=? WHERE target=? AND session=?", (handle, row["handle"], row["session"]))
        con.execute("UPDATE claims SET actor=? WHERE agent_id=?", (handle, aid))
    if sp:
        con.execute("UPDATE claims SET task=? WHERE agent_id=? AND task IS NULL", (task, aid))
    if task and atype not in T.AUTO_DONE_TYPES:
        con.execute("UPDATE tasks SET owner=?, owner_type=?, agent_id=?, model=COALESCE(?,model), effort=COALESCE(?,effort), updated=? WHERE id=?",
                    (handle, atype, aid, model, effort, t, task))
    elif task:
        con.execute("UPDATE tasks SET agent_id=? WHERE id=? AND (source='spawn' AND agent_id IS NULL OR status='doing' AND owner_type=?)",
                    (aid, task, atype))
    return handle, (row["handle"] if row else None)


def relink(con, inp, me):
    if me["lead"]:
        return None
    row = T.agent_row(con, me["agent_id"])
    if not row or row["linked"]:
        return None
    sp, name, atype, meta, how = resolve(con, inp, me["agent_id"])
    if not how:
        return None
    handle, old = apply_link(con, me["agent_id"], sp, name, atype, meta, how)
    me.update(handle=handle, type=atype, task=T.agent_row(con, me["agent_id"])["task"])
    if old and old != handle:
        return f"Team harness: your team handle is `{handle}` (card {me.get('task') or '-'}); use `--as {handle}` from now on."
    return None


def team_snapshot(con, me_id):
    lines = []
    claims = T.live_claims(con)
    for a in T.active_agents(con):
        if a["agent_id"] == me_id:
            continue
        t = T.get_task(con, a["task"])
        held = [c["rel"] or c["path"] for c in claims if c["agent_id"] == a["agent_id"]][:4]
        lines.append(f"- {a['handle']} ({a['type']}) — {a['task'] or '-'} {T.short(t['title'], 70) if t else T.short(a['description'], 70)}"
                     + (f" — editing: {', '.join(held)}" if held else ""))
    return lines


def agent_context(con, handle, atype, model, effort, task, aid):
    t = T.get_task(con, task)
    parts = [f"Team harness: you are `{handle}` ({atype}, {model or 'default'}/{effort or 'default'}). "
             f"Your card: {task} \"{t['title']}\"." if t else f"Team harness: you are `{handle}` ({atype}). No card."]
    if T.PROTOCOL.exists():
        parts.append(T.PROTOCOL.read_text().strip().replace("--as <handle>", f"--as {handle}").replace(T.CLI_DOC, T.CLI))
    snap = team_snapshot(con, aid)
    if snap:
        parts.append("Who else is working right now:\n" + "\n".join(snap))
    recent = con.execute(
        "SELECT * FROM events WHERE kind IN ('decision','blocker') AND ts>? ORDER BY id DESC LIMIT 5", (T.now() - 86400,)
    ).fetchall()
    if recent:
        parts.append("Recent team decisions/blockers:\n" + "\n".join(f"- {r['actor']}: {T.short(r['text'], 200)}" for r in reversed(recent)))
    lessons = T.read_lessons(atype)
    if lessons:
        parts.append("Lessons from past retros for your role:\n" + lessons)
    return "\n\n".join(parts)


def subagent_start(con, inp):
    aid = inp.get("agent_id")
    if not aid:
        return None
    t = T.now()
    existing = T.agent_row(con, aid)
    if existing:
        con.execute("UPDATE agents SET status='running', stopped=NULL, last_ts=?, last_tool=NULL WHERE agent_id=?", (t, aid))
        card = T.get_task(con, existing["task"])
        if card and card["status"] == "review" and card["agent_id"] == aid:
            T.task_move(con, card["id"], "doing", existing["handle"], session=existing["session"], note=T.msg("note.resumed"))
    else:
        con.execute("INSERT INTO agents(agent_id,session,handle,type,status,started,last_ts,tools,cursor) VALUES(?,?,?,?,?,?,?,0,?)",
                    (aid, inp.get("session_id"), T.handle_for(None, inp.get("agent_type"), aid), inp.get("agent_type"), "running", t, t,
                     T.max_event(con)))
    if not (existing and existing["linked"]):
        sp, name, atype, meta, how = resolve(con, inp, aid)
        apply_link(con, aid, sp, name, atype, meta, how)
    row = T.agent_row(con, aid)
    T.emit(con, "start", row["handle"], row["description"] or "", session=inp.get("session_id"), agent_id=aid, actor_type=row["type"],
           task=row["task"], data={"type": row["type"], "model": row["model"], "effort": row["effort"], "resumed": bool(existing),
                                   "linked": row["linked"]})
    return ctx("SubagentStart", agent_context(con, row["handle"], row["type"], row["model"], row["effort"], row["task"], aid))


def first_line(text):
    lines = [l.strip(" #*-`>|") for l in (text or "").splitlines()]
    return " — ".join([l for l in lines if l][:2])


def subagent_stop(con, inp):
    aid = inp.get("agent_id")
    row = T.agent_row(con, aid)
    if not row:
        return None
    if row["status"] == "done":
        last = con.execute("SELECT * FROM events WHERE agent_id=? AND kind='stop' ORDER BY id DESC LIMIT 1", (aid,)).fetchone()
        if inp.get("stop_hook_active") or (last and (row["last_ts"] or 0) <= last["ts"] + 1):
            late = inp.get("last_assistant_message") or ""
            if last and late.strip() and not (last["text"] or "").strip() and not load_data(last["data"]).get("result"):
                fill_stop(con, row, last, late)
            return None
    if not row["linked"]:
        relink(con, inp, T.identity(con, inp))
        row = T.agent_row(con, aid)
    block = stop_block(con, inp, row)
    if block:
        return block
    handed = row["result"] if row["last_tool"] == "SubagentHandback" else ""
    res = handed or inp.get("last_assistant_message") or T.last_reply(inp.get("agent_transcript_path"))
    t = T.now()
    con.execute("UPDATE agents SET status='done', stopped=?, last_ts=?, result=? WHERE agent_id=?", (t, t, res[:4000], aid))
    con.execute("DELETE FROM claims WHERE agent_id=?", (aid,))
    m = re.search(r"\b(PASS|FAIL)\b", res[:600])
    verdict = m.group(1) if m and row["type"] in ("reviewer", "codex") else None
    T.emit(con, "stop", row["handle"], T.short(first_line(res), 300), session=row["session"], agent_id=aid, actor_type=row["type"],
           task=row["task"], data={"duration_s": int(t - (row["started"] or t)), "tools": row["tools"], "result": res[:2000], "verdict": verdict})
    task = T.get_task(con, row["task"])
    if not task or task["status"] in ("done", "dropped"):
        return None
    if row["type"] in T.AUTO_DONE_TYPES:
        if task["source"] == "spawn" and task["agent_id"] == aid:
            T.task_move(con, task["id"], "done", row["handle"], session=row["session"])
        elif task["status"] == "doing" and task["agent_id"] == aid:
            T.task_move(con, task["id"], "review", row["handle"], note=T.msg("note.finished"), session=row["session"])
    elif task["status"] == "doing" and task["agent_id"] == aid:
        T.task_move(con, task["id"], "review", row["handle"], note=T.msg("note.finished"), session=row["session"])
    return None


def session_start(con, inp):
    if inp.get("agent_id"):
        return None
    me = T.identity(con, inp)
    t = T.now()
    top = T.max_event(con)
    con.execute(
        "INSERT INTO agents(agent_id,session,handle,type,model,status,started,last_ts,tools,cursor) VALUES(?,?,?,?,?,?,?,?,0,?) "
        "ON CONFLICT(agent_id) DO UPDATE SET status='running', stopped=NULL, last_ts=excluded.last_ts",
        (me["agent_id"], me["session"], "lead", "team-lead", inp.get("model"), "running", t, t, top),
    )
    source = inp.get("source") or "startup"
    if source != "compact":
        T.emit(con, "session", "lead", T.msg("session.lead", source=source), session=me["session"], agent_id=me["agent_id"], actor_type="team-lead")
    con.execute("UPDATE agents SET tty=?, term=?, tab_title=NULL WHERE agent_id=?", (T.own_tty(), os.environ.get("TERM_PROGRAM"), me["agent_id"]))
    lead_tab(con, inp, "done")
    T.ensure_dashboard()
    return ctx("SessionStart", lead_brief(con))


def lead_tab(con, inp, state):
    session = inp.get("session_id")
    lid = T.lead_id(session)
    row = T.agent_row(con, lid)
    if not row:
        return
    if not row["tty"]:
        con.execute("UPDATE agents SET tty=?, term=? WHERE agent_id=?", (T.own_tty(), os.environ.get("TERM_PROGRAM"), lid))
    title = T.session_title(inp.get("transcript_path"))
    con.execute("UPDATE agents SET label=?, tab_title=? WHERE agent_id=?",
                (T.session_label(con, session, inp.get("cwd")), title, lid))


def lead_brief(con):
    if os.environ.get("TEAM_RITUAL"):
        return f"Team harness on. Ritual run: {os.environ['TEAM_RITUAL']}. Do only that ritual."
    lines = [f"Team harness on. Dashboard: {T.dashboard_url()} · CLI: {T.CLI} --help · protocol: {T.PROTOCOL}"]
    marks = ",".join("?" * len(T.OPEN_STATUSES))
    rows = con.execute(f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY seq", T.OPEN_STATUSES).fetchall()
    if rows:
        lines.append("Open cards:")
        lines += [f"- {r['id']} [{r['status']}] {T.short(r['title'], 90)} — {r['owner'] or '-'}" for r in rows[:15]]
        if len(rows) > 15:
            lines.append(f"- … and {len(rows) - 15} more ({T.CLI} board)")
    due = rituals_due()
    if due:
        lines.append("Rituals due: " + "; ".join(due))
    return "\n".join(lines)


def rituals_due():
    lt = time.localtime()
    out = []
    if lt.tm_wday < 5 and lt.tm_hour >= 8 and not T.ritual_path("daily").exists():
        out.append("daily standup for today is not written yet (skill `daily`)")
    last = lambda k: max((p.stem for p in (T.RITUALS / k).glob("*.md")), default=None)
    back = 3 if lt.tm_wday == 0 else 1
    yday = time.strftime("%Y-%m-%d", time.localtime(time.time() - back * 86400))
    lr = last("reflect")
    if lr and lr < yday and lt.tm_wday < 5:
        out.append(f"reflection last ran {lr} (skill `reflect`)")
    lretro = last("retro")
    week_ago = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    if not lretro or lretro < week_ago:
        out.append(f"weekly retro is due, last {lretro or 'never'} (skill `retro`)")
    lp = last("planning")
    if not lp or lp < week_ago:
        out.append(f"weekly planning is due, last {lp or 'never'} (skill `planning`)")
    return out


HANDBACK = re.compile(r'^\s*<agent-message from="([^"]+)">\s*\[Subagent hand-back\][^\n]*\n(.*?)(\n</agent-message>\s*$|$)', re.S)
ASK_HEAD = re.compile(r"^\W*(що від тебе|твій крок|що лишилось тобі)\b", re.I)
STRICT_HEAD = re.compile(r"^ {0,3}(?:#{1,6}\s+)?(?:\d{1,2}[.)]\s+|[-*•]\s+)?[*_]{0,3}\s*(what i need from you|your steps?|від тебе|на тобі)(?P<tail>.*)$", re.I)
HEAD_SUFFIX = re.compile(r"[*_]*\s*(?:\([^)]{1,24}\)|(?:now|today|later|зараз|сьогодні|потім)(?!\w))?", re.I)
NEG = re.compile(r"(?:(?:зараз|поки(?:\s+що)?|наразі|від\s+тебе)\s+)?(?:нічого|не\s+треба|не\s+потрібно)(?!\w)|"
                 r"(?:nothing|none|not\s+needed|no\s+action(?:\s+(?:needed|required))?)(?!\w)", re.I)
NEG_BREAK = re.compile(r"`!\s?\S|\?|(?<!\w)(?:but|except|just|але|крім|тільки)(?!\w)", re.I)
BULLET = re.compile(r"^\s*(?:(?:[-*•—–]|\d{1,2}[.)])(?:\s+|$))+")
SECTION = re.compile(r"рішення|ідеї|перевір|результат|що є|рев'ю|ризики|далі|\bdecisions?\b|\bideas\b|\bcheck|\bresults?\b|\breview\b|\brisks\b|\bnext\b", re.I)
SUBHEAD = re.compile(r"^\s{0,3}(#{1,6}\s+.*|\*\*[^*]+\*\*\s*:?|__[^_]+__\s*:?)\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
USER_NOTIFY = {"permission_prompt", "elicitation_dialog"}


def load_data(raw):
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def fill_stop(con, a, e, body):
    data = load_data(e["data"])
    data["result"] = body[:2000]
    if a["type"] in ("reviewer", "codex"):
        v = re.search(r"\b(PASS|FAIL)\b", body[:600])
        data["verdict"] = v.group(1) if v else None
    summary = T.short(first_line(body), 300)
    con.execute("UPDATE events SET text=?, data=? WHERE id=?", (summary, json.dumps(data, ensure_ascii=False), e["id"]))
    con.execute("UPDATE agents SET result=COALESCE(NULLIF(result,''), ?) WHERE agent_id=?", (body[:4000], a["agent_id"]))
    T.emit(con, "report", a["handle"], summary, session=a["session"], agent_id=a["agent_id"], actor_type=a["type"], target="lead",
           task=a["task"], data={"backfill": True, "stop_id": e["id"], "result": body[:2000], "verdict": data.get("verdict")})


def backfill_report(con, session, prompt):
    m = HANDBACK.match(prompt)
    if not m:
        return
    who, body = m.group(1), m.group(2)
    body = "\n".join(line[2:] if line.startswith("  ") else line for line in body.splitlines())
    body = body.strip()
    if not body:
        return
    cands = con.execute("SELECT * FROM agents WHERE session=? AND (agent_id=? OR handle=? OR name=?)", (session, who, who, who)).fetchall()
    exact = [c for c in cands if c["agent_id"] == who]
    cands = exact or cands
    if len(cands) != 1:
        return
    a = cands[0]
    e = con.execute("SELECT * FROM events WHERE agent_id=? AND kind='stop' ORDER BY id DESC LIMIT 1", (a["agent_id"],)).fetchone()
    if not e or (e["text"] or "").strip() or load_data(e["data"]).get("result"):
        return
    fill_stop(con, a, e, body)


def prompt_submit(con, inp):
    me = T.identity(con, inp)
    ensure_row(con, me, inp)
    con.execute("UPDATE agents SET status='running', last_ts=? WHERE agent_id=?", (T.now(), me["agent_id"]))
    prompt = inp.get("prompt") or ""
    backfill_report(con, me["session"], prompt)
    if prompt.strip() and not prompt.lstrip().startswith("<"):
        T.emit(con, "request", "user", prompt[:1500], session=me["session"], target="lead", task=T.task_ref(prompt[:300]))
    if me["lead"]:
        lead_tab(con, inp, "work")
    box = inbox(con, me)
    return ctx("UserPromptSubmit", box) if box else None


def heading_rest(line):
    if re.match(r"^(?: {4}|\t)|^\s*>", line):
        return None
    m = STRICT_HEAD.match(line)
    if m:
        t = m.group("tail")
        t = t[HEAD_SUFFIX.match(t).end():].lstrip(" *_")
        if not t:
            return ""
        if t[0] in ":—–" or (t[0] == "-" and t[1:2] in ("", " ")):
            return t[1:].strip(" *_")
        if t[0] == "." and not t[1:].strip(" *_"):
            return ""
        return None
    if ASK_HEAD.match(line):
        return re.sub(r"^\W*\w[\w\s]*?\b(тебе|тобі|крок)\b\W*", "", line, flags=re.I).strip(" *:")
    return None


def negated(item):
    text = BULLET.sub("", item.strip()).strip(" *_`.")
    if not text.strip("—–- "):
        return True
    m = NEG.match(text)
    return bool(m) and not NEG_BREAK.search(text[m.end():])


def fence_end(lines, j, mark):
    k = j + 1
    while k < len(lines):
        m = FENCE.match(lines[k])
        if m and m.group(1)[0] == mark[0] and len(m.group(1)) >= len(mark) and not m.group(2).strip():
            break
        k += 1
    return k


def ask_block(lines, i, inline):
    block, items = [lines[i]], []
    if inline is not None and inline.strip():
        if negated(inline):
            return None
        items.append(inline)
    j, seen_list, blank = i + 1, False, False
    while j < len(lines) and j < i + 40:
        nxt = lines[j]
        fm = FENCE.match(nxt)
        if fm:
            k = fence_end(lines, j, fm.group(1))
            block += lines[j:k + 1]
            items += [c for c in lines[j + 1:k] if c.strip()]
            j, blank = k + 1, False
            continue
        if not nxt.strip():
            blank = True
            j += 1
            continue
        if SUBHEAD.match(nxt):
            if SECTION.search(nxt):
                break
            block.append(nxt)
            j += 1
            continue
        is_list = bool(BULLET.match(nxt)) or nxt.startswith(("  ", "\t")) and seen_list
        if not is_list and (seen_list or (blank and items)):
            break
        if not is_list and not items and negated(nxt):
            return None
        block.append(nxt)
        items.append(nxt)
        seen_list = seen_list or bool(BULLET.match(nxt))
        blank = False
        j += 1
    if items and not all(negated(x) for x in items):
        return "\n".join(block).strip()
    return None


def user_asks(text):
    lines = (text or "").splitlines()
    fence = None
    for i, line in enumerate(lines):
        fm = FENCE.match(line)
        if fence:
            if fm and fm.group(1)[0] == fence[0] and len(fm.group(1)) >= len(fence) and not fm.group(2).strip():
                fence = None
            continue
        if fm:
            fence = fm.group(1)
            continue
        inline = heading_rest(line)
        if inline is None:
            continue
        got = ask_block(lines, i, inline)
        if got:
            return got
    return None


def stop(con, inp):
    if inp.get("agent_id"):
        return None
    session = inp.get("session_id")
    lid = T.lead_id(session)
    con.execute("UPDATE agents SET status='idle', last_ts=? WHERE agent_id=?", (T.now(), lid))
    T.emit(con, "tool", "lead", T.msg("turn.end"), session=session, agent_id=lid, actor_type="team-lead", target="Stop")
    text = inp.get("last_assistant_message") or T.last_reply(inp.get("transcript_path"))
    ask = user_asks(text)
    if ask:
        T.emit(con, "needs_user", "lead", T.short(ask, 300), session=session, agent_id=lid, actor_type="team-lead",
               data={"message": ask[:1500], "type": "lead_ask"})
    lead_tab(con, inp, "ask" if ask else "done")
    return None


def session_end(con, inp):
    if inp.get("agent_id"):
        return None
    session = inp.get("session_id")
    t = T.now()
    con.execute("UPDATE agents SET status='ended', stopped=? WHERE session=? AND status IN ('running','idle')", (t, session))
    con.execute("DELETE FROM claims WHERE session=?", (session,))
    lead_tab(con, inp, "end")
    T.emit(con, "session", "lead", T.msg("session.ended", reason=inp.get("reason") or "").strip(), session=session,
           agent_id=T.lead_id(session), actor_type="team-lead", data={"ended": True})
    return None


def notification(con, inp):
    if inp.get("notification_type") not in USER_NOTIFY:
        return None
    me = T.identity(con, inp)
    if inp.get("notification_type") == "permission_prompt":
        lead_tab(con, inp, "perm")
    msg = inp.get("message") or ""
    T.emit(con, "needs_user", me["handle"], T.short(msg, 300), session=me["session"], agent_id=me["agent_id"], actor_type=me["type"],
           task=me.get("task"), data={"message": msg, "type": inp.get("notification_type")})
    return None


HANDLERS = {
    "PreToolUse": pre_tool,
    "SubagentStart": subagent_start,
    "SubagentStop": subagent_stop,
    "SessionStart": session_start,
    "UserPromptSubmit": prompt_submit,
    "Stop": stop,
    "SessionEnd": session_end,
    "Notification": notification,
}


def main():
    raw = sys.stdin.read()
    try:
        inp = json.loads(raw)
    except Exception:
        return
    if (T.STATE / "debug").exists():
        with open(T.STATE / "raw.jsonl", "a") as f:
            f.write(raw.strip() + "\n")
    handler = HANDLERS.get(inp.get("hook_event_name"))
    if not handler:
        return
    con = T.connect()
    try:
        out = handler(con, inp)
    finally:
        con.close()
    if out:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            T.STATE.mkdir(parents=True, exist_ok=True)
            with open(T.STATE / "hook-errors.log", "a") as f:
                f.write(time.strftime("%F %T ") + traceback.format_exc() + "\n")
        except Exception:
            pass
    sys.exit(0)
