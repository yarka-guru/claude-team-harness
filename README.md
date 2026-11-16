# Claude team harness

A local harness that runs a Claude Code session as a team lead with specialist subagents, and
keeps a shared record of what the team is doing. Hooks write every spawn, tool call, message,
file edit and finish into a SQLite event log. On top of that log sit a task board, a team chat,
file claims, a fact-check gate for outward text, and a dashboard in the browser.

Everything is in English by default. The dashboard also has Ukrainian: it follows the browser
language, an EN / УК toggle in the header switches it (the choice is saved), and `?lang=en|uk` in the
URL overrides both. `TEAM_LANG=uk` switches the CLI board headings and the event texts that hooks
write. The hook that turns the lead's "What I need from you" block into a "Needs you" item also
recognises the Ukrainian heading "Що від тебе"; see `CLAUDE.example.md`.

## Install with an agent

Start your coding agent (Claude Code or a similar CLI agent) in the workspace you want to equip and give it this prompt. Replace the path with where you cloned this repository (a `git clone`, not a zip).

```
Install the Claude team harness, cloned at <path to this clone>, into the workspace in the current directory. Follow the "Install with an agent" section of its README.md: run every step and its check, and show me the output. Stop and ask before overwriting or modifying any existing file, and before running any launchd or sudo script. Do not touch ~/.claude. At the end, list what you changed and what is left for me.
```

Run from the workspace root. Shell state may not carry over between commands, so the install command sets `H` itself. `grep -c` exits 1 when it prints `0`; that is not a failure when `0` is the expected answer.

1. Prerequisites: `python3 --version` prints 3.9 or newer, `claude --version` prints a version, `git` and `lsof` exist. On a first install, `lsof -nP -iTCP:7777 -sTCP:LISTEN` prints nothing; if it prints a line, pick a free port and put `TEAM_PORT=<port>` directly in front of each `python3` below. On a re-run skip this check and use the `TEAM_PORT` already in `settings.json`, if any. Snapshot for the rollback check, only if it does not exist yet (delete it when done): `test -e "../$(basename "$PWD").before" || cp -R . "../$(basename "$PWD").before"`.
2. Install. It copies the clone's tracked `.claude` files without overwriting, adds the hooks and the agent-teams `env` key to `settings.json` (plus `TEAM_PORT` if you set it), appends `CLAUDE.example.md` to `CLAUDE.md` and, in a git workspace, three lines to `.gitignore`. It is safe to run twice, and records what it did in `.claude/backup/install.json` for the rollback.
```
H='<path to the clone>' python3 - <<'EOF'
import json,os,subprocess,shutil,hashlib
H=os.environ['H']; R='.claude/backup/install.json'
GI='.claude/team/state/\n.claude/team/rituals/\n.claude/backup/\n'
def rd(p): return open(p,newline='',encoding='utf-8').read() if os.path.exists(p) else None
def put(p,t,m='w'): open(p,m,newline='',encoding='utf-8').write(t)
def sha(p): return hashlib.sha256(open(p,'rb').read()).hexdigest()
rec=json.loads(rd(R) or '{}') or dict(cache=[r for r,d,_ in os.walk('.claude') if '__pycache__' in d],dirs=[d for d in ['.claude'] if not os.path.isdir(d)],runtime=[d for d in GI.split() if not os.path.exists(d)])
def save(): os.makedirs('.claude/backup',exist_ok=True); put(R,json.dumps(rec))
save()
for f in subprocess.check_output(['git','-C',H,'ls-files','-z','.claude'],text=True).split('\0')[:-1]:
    if os.path.exists(f):
        if rd(f)!=rd(H+'/'+f): print('kept yours, merge by hand:',f)
        continue
    d=os.path.dirname(f); miss=[]
    while not os.path.isdir(d): miss.insert(0,d); d=os.path.dirname(d)
    rec['dirs']+=miss; rec.setdefault('files',{})[f]=sha(H+'/'+f); save()
    os.makedirs(os.path.dirname(f),exist_ok=True); shutil.copy(H+'/'+f,f)
p='.claude/settings.json'; raw=rd(p); cur=json.loads(raw or '{}'); ex=json.load(open(H+'/.claude/settings.example.json')); before=json.dumps(cur)
want=dict(ex['env'],**({'TEAM_PORT':os.environ['TEAM_PORT']} if 'TEAM_PORT' in os.environ else {})); env=cur.setdefault('env',{}); new=[k for k in want if k not in env]; env.update({k:want[k] for k in new}); ev=[]
for e,es in ex['hooks'].items():
    a=cur.setdefault('hooks',{}).setdefault(e,[])
    if not any('team/hook.py' in h.get('command','') for x in a for h in x.get('hooks',[])): a+=es; ev.append(e)
rec.setdefault('settings',raw); rec['env']=rec.get('env',[])+new; rec['hooks']=rec.get('hooks',[])+ev; save()
if json.dumps(cur)!=before: put(p,json.dumps(cur,indent=2,ensure_ascii=False)+'\n')
def add(p,s,mark):
    t=rd(p)
    if t and mark in t: return
    s=('\n' if t else '')+s; rec.setdefault('appended',{})[p]=s
    if t is None: rec.setdefault('new',[]).append(p)
    save(); put(p,s,'a')
add('CLAUDE.md',rd(H+'/CLAUDE.example.md'),'# Team lead mode')
if os.path.exists('.git'): add('.gitignore',GI,'.claude/team/state/')
print('env keys added:',rec['env'],'hook events added:',rec['hooks'])
EOF
```
   Checks: no `kept yours` line (each one is a file of yours that differs from the clone's and was not overwritten); `grep -c team/hook.py .claude/settings.json` prints `8`; `grep -c '^# Team lead mode' CLAUDE.md` prints `1`; in a git repo `git check-ignore .claude/team/state/team.db .claude/team/rituals/x .claude/backup/x` prints all three paths. If `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` is not in `env keys added`, your settings already set it: tell the human to check it is `1`. An existing `settings.json` is rewritten with 2-space indentation, content kept.
3. Hook smoke test: `echo '{"session_id":"t","hook_event_name":"SessionStart","source":"startup"}' | CLAUDE_PROJECT_DIR="$PWD" python3 .claude/team/hook.py`. Check: the JSON contains `Team harness on.` It creates `.claude/team/state/team.db` and starts the dashboard.
4. Log, board, dashboard: `python3 .claude/team/team.py feed --all` prints a line ending `lead session: startup`. `python3 .claude/team/team.py task add "smoke test" --owner lead` prints the new id (`T-<n>`); `python3 .claude/team/team.py board` shows it under `To do`; `python3 .claude/team/team.py task drop T-<n>` prints `ok`. `python3 .claude/team/team.py dashboard start` prints `running http://` (or `started http://`); `failed: ... held by another program` means the port is taken, so go back to step 1.
5. Gate: `n=$(mktemp) && echo 'Fixed in v1.2.3' > "$n" && printf '{"session_id":"t","hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"gh pr comment 5 --body-file %s"}}' "$n" | CLAUDE_PROJECT_DIR="$PWD" python3 .claude/team/hook.py; rm "$n"` prints a `permissionDecision` of `deny` that names the version.

Steps that stay with the human:

- The human starts a new `claude` in the workspace and approves any trust prompt, so the SessionStart hook runs for real. Then `team.py feed --all` has a new `lead session: startup` line after the smoke test's.
- Rituals: `.claude/team/schedule/install.sh` (macOS) registers four launchd jobs that run `claude -p` unattended with `--permission-mode auto`. Caps per run: daily $3, planning $5, retro $6, reflect $6. Daily and reflect run on 5 weekdays, planning and retro once a week, so the worst case is 5x3 + 5 + 6 + 5x6 = $56 a week. The labels `com.claude-team.*` are per user, not per workspace: installing from a second workspace replaces the first one's jobs, and `uninstall.sh` removes them. Check: `launchctl list | grep com.claude-team` lists four labels. `claude --help` must show `--effort`, `--permission-mode` and `--max-budget-usd`.
- Local domain: `sudo .claude/team/schedule/install-domain.sh` edits `/etc/hosts`, `pf` and a LaunchDaemon. See "Local domain".
- Accounts: the Codex CLI, `gh` and the Slack and Jira connectors are the human's; replace the example MCP tool ids in `researcher.md`.

Roll back. It undoes only what the install recorded, so it works after a partial install or a second run, and keeps your own edits and files added since. It does delete `.claude/team/state` (the event log) and `.claude/team/rituals` (ritual notes) if the install created them. If the human installed the schedule or the local domain, ask them to run `.claude/team/schedule/uninstall.sh` and `sudo .claude/team/schedule/uninstall-domain.sh` first; do not run them yourself.
```
python3 .claude/team/team.py dashboard stop
python3 - <<'EOF'
import json,os,shutil,hashlib
R='.claude/backup/install.json'
def rd(p): return open(p,newline='',encoding='utf-8').read() if os.path.exists(p) else None
def put(p,t): open(p,'w',newline='',encoding='utf-8').write(t)
def sha(p): return hashlib.sha256(open(p,'rb').read()).hexdigest()
rec=json.loads(rd(R) or '{}'); g=rec.get
for p,s in g('appended',{}).items():
    t=rd(p)
    if t is None: continue
    if s not in t: print('kept, edited since install:',p); continue
    t=t.replace(s,'',1)
    if t or p not in g('new',[]): put(p,t)
    else: os.remove(p)
p='.claude/settings.json'
if 'settings' in rec and os.path.exists(p):
    raw=rec['settings']; old=json.loads(raw or '{}'); cur=json.loads(rd(p)); hk=cur.get('hooks',{})
    for e in g('hooks',[]):
        keep=[]
        for x in hk.get(e,[]):
            l=[h for h in x.get('hooks',[]) if 'team/hook.py' not in h.get('command','')]
            gone=len(l)<len(x.get('hooks',[]))
            if gone: x['hooks']=l
            if l or not gone: keep.append(x)
        hk[e]=keep
        if not keep and e not in old.get('hooks',{}): del hk[e]
    for k in g('env',[]): cur.get('env',{}).pop(k,None)
    for k in ('hooks','env'):
        if k in cur and not cur[k] and k not in old: del cur[k]
    if raw is not None and cur==old: put(p,raw)
    elif cur or raw is not None: put(p,json.dumps(cur,indent=2,ensure_ascii=False)+'\n')
    else: os.remove(p)
for f,s in g('files',{}).items():
    if not os.path.exists(f): continue
    if sha(f)==s: os.remove(f)
    else: print('kept, edited since install:',f)
for d in g('runtime',[]): shutil.rmtree(d,ignore_errors=True)
for r,ds,_ in os.walk('.claude'):
    if '__pycache__' in ds and r not in g('cache',[]): shutil.rmtree(r+'/__pycache__')
for d in sorted(g('dirs',[]),key=len,reverse=True):
    if os.path.isdir(d) and not os.listdir(d): os.rmdir(d)
if os.path.exists(R): os.remove(R)
print('rolled back' if rec else 'no install record, nothing to undo')
EOF
```
Each `kept, edited since install` line names a file or appended block you changed after the install; report it to the human and leave it. Check: `diff -r -x .git . "../$(basename "$PWD").before"` prints nothing apart from those and files added since the install. Tell the human to restart any `claude` session started in the workspace after the install.

## How it works

```
Claude Code hooks ──> .claude/team/hook.py ──> state/team.db (SQLite, WAL)
                                                   │
          team.py (CLI: board, chat, factcheck) ───┤
                                                   └──> dashboard/server.py ──> http://127.0.0.1:7777
                                                                         (or http://mission.control, see below)
launchd ──> schedule/run-ritual.sh ──> claude -p "/daily" | "/planning" | "/retro" | "/reflect"
```

- `hook.py` handles `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `SubagentStart`,
  `SubagentStop`, `Stop`, `SessionEnd` and `Notification`. It logs events, links each subagent to
  its spawn and board card, injects `PROTOCOL.md`, a team snapshot and role lessons into every
  subagent, and delivers team chat addressed to an agent on its next tool call.
- Board: cards `T-n` with status, owner role and model/effort. An Agent call whose description
  starts with `T-12: ...` attaches to that card; an untagged spawn opens its own card. A finished
  worker's card moves to review; only the lead moves it to done.
- File claims: every Edit/Write claims the file for that agent. Another agent's edit of the same
  file is denied, and so are `git checkout/switch/stash/reset --hard/rebase/pull/merge/clean/restore`
  in a worktree where someone else has edits.
- Fact-check gate: Slack, Jira and Confluence write tools (matched by tool name, any MCP server
  prefix) and `gh pr|issue|release` writes and `gh api` writes are denied while their text holds
  dates, weekdays, times, numbers, versions, ticket/PR refs or cron expressions with no PASS for that
  exact text in the last 24 hours. A `reviewer` or `codex` agent that did not write the text
  records the PASS with one primary source per fact (`team.py factcheck pass`). It also checks that
  weekdays match dates and that a "first run" date is the schedule's real first firing.
- Dashboard: `dashboard/server.py` listens on 127.0.0.1 only and accepts only its own host names.
  Every `GET` endpoint reads the database without writing to it. The one write endpoint is
  `POST /api/focus` (same-origin, an `X-Team-Action: focus` header, at most one call per second): it
  runs `osascript` to bring the Apple Terminal tab of a lead session to the front, for the "Go" button
  on a "Needs you" item. The `SessionStart` hook starts the server if the port is free and prints its
  URL. There is no authentication: any local process or user on the machine can read the board and
  chat over loopback, so do not run it on a shared machine.
- Rituals: skills `daily`, `planning`, `retro` and `reflect`. `schedule/install.sh` registers
  launchd jobs (macOS) that run them headless with `claude -p`. Notes land in
  `.claude/team/rituals/<kind>/<date>.md`.
- Knowledge base: `.claude/knowledge/` starts empty. The team adds one entry per solved problem
  while working and in `reflect`/`retro`; `python3 .claude/skills/kb/kb.py` validates the entries and
  regenerates `INDEX.md`, and agents read the index before they start (skill `kb`).

## Layout

```
CLAUDE.example.md            lead instructions to merge into your CLAUDE.md
.claude/settings.example.json  hooks wiring to merge into .claude/settings.json
.claude/team/                hook.py, team.py, teamlib.py, PROTOCOL.md, lessons/, schedule/, dashboard/, tests/
.claude/agents/              role templates: software-engineer, delivery-engineer, platform-sre,
                             product-designer, researcher, reviewer, codex
.claude/skills/              daily, planning, retro, reflect, humanize, kb
.claude/knowledge/           the knowledge base (README.md and a generated INDEX.md)
```

## Requirements

- Claude Code with hooks and subagents; `settings.example.json` turns on agent teams
  (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`).
- Python 3.9 or newer, standard library only (SQLite with JSON functions, as shipped with Python).
- macOS for the launchd rituals, the local domain and the dashboard's "Go" button (Apple Terminal
  via `osascript`); the rest runs anywhere Python does. `lsof` for `team.py dashboard stop`.
- The rituals call the Claude Code CLI as `claude -p "/<skill>" --model <m> --effort <e>
  --permission-mode auto --max-budget-usd <n>`; check that your `claude` version accepts these flags.
- Optional: the Codex CLI for the `codex` agent; Slack, Jira and GitHub (`gh`) connectors for the
  researcher and the fact-check gate to have something to guard. `agents/researcher.md` lists Slack
  and Atlassian MCP tool ids as an example; change them to the connectors you actually have.

## Install

1. Copy `.claude/` into your workspace root (the directory you start `claude` in), so you get
   `<workspace>/.claude/team/`, `.claude/agents/`, `.claude/skills/` and `.claude/knowledge/`.
2. Merge `.claude/settings.example.json` into `<workspace>/.claude/settings.json`. The hook command
   uses `$CLAUDE_PROJECT_DIR`, so no paths need editing.
3. Append `CLAUDE.example.md` to `<workspace>/CLAUDE.md` and adjust the model table and reporting
   language to taste.
4. Add these to your workspace `.gitignore`: `.claude/team/state/`, `.claude/team/rituals/` and
   `.claude/backup/`. The event log stores prompts, agent briefs and drafts.
5. Start `claude` in the workspace. The session brief prints the dashboard URL and the CLI path.
6. Optional rituals: `.claude/team/schedule/install.sh` (uninstall with `uninstall.sh`).
   `RITUAL_MODEL` sets the model for planning, retro and reflect (default `opus`); `CLAUDE_BIN`
   points at `claude` if it is not on the login shell's `PATH`; `TEAM_LANG` picks the hook language.
   When set at install time, `CLAUDE_BIN`, `RITUAL_MODEL` and `TEAM_LANG` are written into each
   launchd job's `EnvironmentVariables`.

Paths are derived from the files' own location. Environment overrides:

| Variable | Default | Meaning |
|---|---|---|
| `TEAM_DB` | `.claude/team/state/team.db` | event log and board |
| `TEAM_RITUALS` | `.claude/team/rituals` | ritual notes |
| `TEAM_PORT` | `7777` | dashboard port (set it in the settings `env` so hooks see it too) |
| `TEAM_BACKEND_PORT` | `TEAM_PORT` + 1 | `install-domain.sh` only: the port the local-domain redirect points at; the dashboard reads it from the anchor file |
| `TEAM_WORKSPACE` | two levels above `.claude/team` | workspace root, used to find this workspace's transcripts in `~/.claude/projects/<slug>` |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code config dir |
| `TEAM_AS` | `lead` | default `--as` for the CLI |
| `TEAM_LANG` | `en` | `uk` for Ukrainian CLI board headings and hook event texts; without it the value comes from `.claude/team/config.json` (`{"lang": "uk"}`), which also reaches the CLI run from a plain terminal |
| `TEAM_CONFIG` | `.claude/team/config.json` | path of that config file |

The project slug is the workspace's absolute path with every non-alphanumeric character replaced
by `-`, as Claude Code names its project directories.

## Local domain (optional, macOS)

The dashboard can open at `http://mission.control` instead of `http://127.0.0.1:7777`. Start the
dashboard first (`python3 .claude/team/team.py dashboard start`), then run once, from the workspace
root:

```
sudo .claude/team/schedule/install-domain.sh
curl -sI http://mission.control      # expect HTTP/1.0 200 OK and Server: team-dashboard
```

On the first visit type the address with `http://`. Choosing a name: reserved zones such as `.test`
and `.internal` are the safe choice; `.control` is not reserved, so a real site under that name
would be shadowed on this machine; avoid `.dev`, `.app` and `.page` (browsers force https there) and
`.local` (mDNS on macOS).

What it changes on the machine:

- `lo0`: a loopback alias `127.0.0.77` (`TEAM_ADDR` picks another 127.x address), so port 80 of
  127.0.0.1 stays free for your other local servers. If the alias already exists, the script uses
  it and leaves it in place on uninstall.
- `/etc/hosts`: one line `127.0.0.77 mission.control # claude-team`, written to a temporary file and
  moved into place (a symlinked hosts file is followed to its target). The first run saves a copy of
  the original to `/var/db/claude-team/hosts.bak`.
- `/etc/pf.anchors/claude-team`: a comment naming the owner (`# claude-team port=7777 backend=7778`)
  and `rdr pass on lo0 inet proto tcp from any to 127.0.0.77 port 80 -> 127.0.0.1 port 7778`, loaded
  into the pf sub-anchor `com.apple/claude-team`. The stock `/etc/pf.conf` already evaluates
  `rdr-anchor "com.apple/*"`, so it is not edited.
- `/var/db/claude-team/`: `boot.sh`, its log `boot.log`, the pf enable token (`pf.token`), whether
  the alias is ours, and whether pf was already enabled before the first install. `boot.sh` adds our
  alias, loads the anchor, takes a new enable reference with `pfctl -E`, saves its token and then
  releases the previous one with `pfctl -X <token>`, so re-installs do not pile up references; if
  `pfctl -E` fails it keeps the old token and exits non-zero.
- `/Library/LaunchDaemons/com.claude-team.domain.plist`: runs `boot.sh` at boot and once on install.

The redirect does not point at the dashboard port. Apple's pf also applies an `rdr` rule in reverse
to outgoing packets whose source is the redirect target, so the replies a server sends from that
address and port to a direct client are rewritten to `127.0.0.77:80` and the direct connection
never completes. The target port is therefore reachable only through the redirect, by design. The
server keeps 127.0.0.1:`TEAM_PORT` for direct use and opens a second listener for pf on the backend
port (`TEAM_BACKEND_PORT` given to the installer, default `TEAM_PORT` + 1). It reads the anchor file
every second and opens that listener only when the comment's `port=` is its own `TEAM_PORT`, so a
second workspace's dashboard on another port leaves the domain alone. It closes the listener when
the anchor goes away, and when it cannot bind the port it logs the error once, keeps serving the
main port and retries every 5 seconds. Both listeners accept the domain name in the `Host` header.

Before changing anything the script refuses when another program already listens on the backend
port (the redirect would cut it off from direct connections); pick another with `TEAM_BACKEND_PORT`.
After loading the rule it checks:
- the anchor holds the redirect (`pfctl -a com.apple/claude-team -s nat`) and pf is enabled. Only
  then does it write the hosts line, so a failed upgrade never leaves a new name behind;
- if a server listened on 127.0.0.1:`TEAM_PORT` before the install, direct connections to it still
  work (`ok: 127.0.0.1:7777 still answers direct connections`);
- `http://<name>/` answers with the dashboard's `Server: team-dashboard` header
  (`ok: http://mission.control/ answers with the dashboard`).

If the first or second check fails, the script runs `uninstall-domain.sh`: it removes the boot job
and its plist, releases our pf enable reference, flushes the anchor and deletes its file, removes
the alias if the install added it, takes the marked line out of the hosts file and deletes
`/var/db/claude-team/`. Then it checks that none of these are left, says so, and exits non-zero.
An earlier working install, for example one under another name, is removed as well, and the script
says that the local domain is now uninstalled; the dashboard stays at `http://127.0.0.1:7777`. If
the third check fails, the redirect stays: a dashboard of this version opens the backend listener by
itself within a few seconds, one started on an older version needs a restart
(`team.py dashboard stop`, then `team.py dashboard start`). For a bug report,
`sudo .claude/team/schedule/diag-domain.sh <new file>` writes pf, interface and connection details
to that file without changing anything else; it refuses an existing path or a symlink and gives the
file to the user who ran sudo.

It also warns when `/etc/pf.conf` has no `rdr-anchor "com.apple/*"` line. The session brief and
`team.py dashboard` print `http://mission.control` when the marked hosts line and an anchor owned by
the current `TEAM_PORT` exist, otherwise the 127.0.0.1 URL; `team.py dashboard status` says which of
the two addresses answers, and `stop` only stops this workspace's dashboard. `TEAM_HOST`,
`TEAM_PORT`, `TEAM_BACKEND_PORT` and `TEAM_ADDR` change the name, ports and loopback address. sudo
drops your environment, so pass them after `sudo`:
`sudo TEAM_HOST=board.test TEAM_PORT=7780 .claude/team/schedule/install-domain.sh`. Re-running the
script replaces its own line, rules and alias, including a line for a different name.

A `*.localhost` name (`sudo TEAM_HOST=team.localhost .claude/team/schedule/install-domain.sh`) also
works, but it takes port 80 of 127.0.0.1: browsers and curl resolve every `*.localhost` name to
loopback themselves and ignore `/etc/hosts`, so the script redirects 127.0.0.1:80 instead of using
an alias, and every connection to 127.0.0.1:80 then reaches the dashboard.

Undo with `sudo .claude/team/schedule/uninstall-domain.sh`. It removes the LaunchDaemon, releases
our pf enable reference with `pfctl -X <token>` (see `man pfctl`), turns pf back on with `pfctl -e`
if it was enabled before the first install and is off now, flushes the anchor, removes the alias if
the install created it, takes the marked line out of the hosts file and moves the backup to
`/etc/hosts.claude-team.bak`.

## CLI

`T` = `python3 .claude/team/team.py` from the workspace root.

```
T board [--all]                          the board
T task add "<title>" --feature F --owner <role> --model m --effort e [--status todo] [--note ...]
T task start|review|block|wait|done|drop|todo|backlog T-n [--note ...]
T task show|note T-n ...
T say|ask|answer|decision|blocker|handoff|report --as <handle> [--to <handle|role|all>] [--task T-n] "text"
T feed [-n 40] [--all]                   recent chat and events
T read <id>...                           a message in full
T who                                    running agents and the files they hold
T release [--path P] [--force]           drop your file claims
T dashboard start|stop|status|open        status says whether 127.0.0.1 and the local domain answer
T digest [--hours 24] | T stats [--days 7] | T transcripts [--hours 24]
T factcheck tokens|pass|fail|waive|status <file> [--as ...] [--claims claims.json] [--note|--reason ...]
T ritual <kind> <path> "<summary>"       record a written ritual note
T vacuum [--days 14]                     drop old tool-activity rows
python3 .claude/skills/kb/kb.py [--check|--review]   knowledge-base index
```

## Tests and demo

```
cd .claude/team
TEAM_DB=/tmp/team-test/team.db python3 -m unittest discover -s tests
```

The tests patch the database into a temporary directory and fix the clock with `TEAM_NOW`. The
domain tests run both scripts against a temporary `/etc` (`TEAM_ETC`); in that mode the LaunchDaemon
and state go under it too, `launchctl` is only printed, and `TEAM_PFCTL` / `TEAM_IFCONFIG` point at
fake `pfctl` and `ifconfig` scripts that log their calls. Without them `pfctl` and `ifconfig` are only
printed too, and `boot.sh` runs only when both fakes are set.

Demo data for the dashboard, in a scratch database (the script refuses to run without both
variables):

```
export TEAM_DB=/tmp/team-demo/team.db TEAM_RITUALS=/tmp/team-demo/rituals TEAM_PORT=7790
python3 .claude/team/dashboard/seed_demo.py
python3 .claude/team/dashboard/server.py      # http://127.0.0.1:7790
```

## License

MIT, see [LICENSE](LICENSE).
