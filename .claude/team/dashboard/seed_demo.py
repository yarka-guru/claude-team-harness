import os
import sys
import time
from pathlib import Path

if not os.environ.get("TEAM_DB") or not os.environ.get("TEAM_RITUALS"):
    sys.exit("set TEAM_DB and TEAM_RITUALS to scratch paths; the demo never touches the real team DB")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import teamlib

T0 = time.time()
CLOCK = [T0]
teamlib.now = lambda: CLOCK[0]

S1 = "3c9e1a7b-2d4f-4e6a-8b1c-5f7d9e2a4b6c"
S2 = "7e2b4d6f-8a1c-4b3e-9d5f-1a3c5e7b9d2f"
LEAD1 = teamlib.lead_id(S1)
LEAD2 = teamlib.lead_id(S2)
WEB = "/work/recipe-web"
API = "/work/recipe-api"
DEPLOY = "/work/recipe-deploy"

AGENTS = {
    "lead": (LEAD1, "team-lead", "opus", "high", S1),
    "lead2": (LEAD2, "team-lead", "opus", "high", S2),
    "researcher-search": ("b4e81c27d05a", "researcher", "sonnet", "medium", S1),
    "explore-ui": ("b9a37e1f64c2", "Explore", "haiku", "low", S1),
    "eng-search": ("b2d65f08a91e", "software-engineer", "opus", "high", S1),
    "sre-ci": ("b7c13a9e52f4", "platform-sre", "opus", "high", S1),
    "delivery-index": ("b5f29d4c18a7", "delivery-engineer", "sonnet", "high", S1),
    "reviewer-search": ("b8e46b2a0c93", "reviewer", "opus", "high", S1),
    "reviewer-ci": ("b1a72c5e9f06", "reviewer", "opus", "xhigh", S1),
    "codex-2nd": ("b3d94f7a26e1", "codex", "haiku", "low", S1),
    "triage-uploads": ("b6c58e3d71b0", "software-engineer", "sonnet", "medium", S2),
}


def at(minutes_ago):
    CLOCK[0] = T0 - minutes_ago * 60


def who(handle):
    aid, atype, model, effort, session = AGENTS[handle]
    return {"agent_id": aid, "actor_type": atype, "session": session}


def say(handle, kind, text, target="all", task=None, data=None):
    return teamlib.emit(con, kind, handle, text, target=target, task=task, data=data, **who(handle))


def tool(handle, name, target, task=None):
    teamlib.emit(con, "tool", handle, target, target=name, task=task, **who(handle))
    aid = AGENTS[handle][0]
    con.execute("UPDATE agents SET last_ts=?, last_tool=?, last_target=?, tools=tools+1 WHERE agent_id=?",
                (CLOCK[0], name, target, aid))


def register(handle, description, task=None):
    aid, atype, model, effort, session = AGENTS[handle]
    con.execute(
        "INSERT OR REPLACE INTO agents(agent_id,session,handle,name,type,model,effort,description,task,status,started,last_ts,tools) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (aid, session, "lead" if atype == "team-lead" else handle, None if atype == "team-lead" else handle, atype, model, effort, description, task,
         "running", CLOCK[0], CLOCK[0]),
    )


def spawn(handle, description, brief, task=None, isolation=None, background=False, lead="lead"):
    aid, atype, model, effort, session = AGENTS[handle]
    teamlib.emit(con, "spawn", "lead", description, target=handle, task=task,
                 data={"type": atype, "model": model, "effort": effort, "isolation": isolation,
                       "background": background, "brief": brief[:1500]}, **who(lead))
    con.execute("INSERT INTO spawns(tool_use_id,session,name,type,description,model,effort,task,ts,agent_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"toolu_{aid}", session, handle, atype, description, model, effort, task, CLOCK[0], aid))
    register(handle, description, task)
    teamlib.emit(con, "start", handle, description, task=task, **who(handle))


def stop(handle, text, result, task=None):
    aid = AGENTS[handle][0]
    row = teamlib.agent_row(con, aid)
    con.execute("UPDATE agents SET status='done', stopped=?, result=? WHERE agent_id=?", (CLOCK[0], result, aid))
    teamlib.emit(con, "stop", handle, text, task=task,
                 data={"duration_s": int(CLOCK[0] - row["started"]), "tools": row["tools"], "result": result},
                 **who(handle))


def claim(handle, repo, worktree, branch, rel, task=None, minutes=0):
    aid, atype, model, effort, session = AGENTS[handle]
    con.execute(
        "INSERT OR REPLACE INTO claims(path,agent_id,actor,session,repo,rel,worktree,branch,task,since,touched) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (f"{worktree}/{rel}", aid, handle, session, f"{repo}/.git", rel, worktree, branch, task,
         CLOCK[0] - minutes * 60, CLOCK[0]),
    )


def task(title, status, feature=None, owner=None, note=None, session=S1, source="lead"):
    otype = AGENTS[owner][1] if owner in AGENTS else None
    model = AGENTS[owner][2] if owner in AGENTS and otype != "team-lead" else None
    effort = AGENTS[owner][3] if owner in AGENTS and otype != "team-lead" else None
    aid = AGENTS[owner][0] if owner in AGENTS else None
    return teamlib.task_add(con, title, "lead", status=status, feature=feature, owner=owner, owner_type=otype,
                            agent_id=aid, model=model, effort=effort, session=session, source=source, note=note)


def move(tid, status, note=None, actor="lead"):
    teamlib.task_move(con, tid, status, actor, note=note, session=AGENTS[actor][4])


def ritual(kind, day, text):
    p = teamlib.RITUALS / kind / f"{day}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


for suffix in ("", "-wal", "-shm"):
    Path(str(teamlib.DB) + suffix).unlink(missing_ok=True)
con = teamlib.connect()

today = time.strftime("%Y-%m-%d", time.localtime(T0))
yesterday = time.strftime("%Y-%m-%d", time.localtime(T0 - 86400))
two_days = time.strftime("%Y-%m-%d", time.localtime(T0 - 2 * 86400))

at(26 * 60)
teamlib.emit(con, "session", "lead", "session started", session="0d1e2f30-old", agent_id="lead:0d1e2f30", actor_type="team-lead")
t_old1 = teamlib.task_add(con, "Recipe photos: switch thumbnails to WebP", "lead", status="doing", feature="photos",
                          owner="eng-photos", owner_type="software-engineer", model="sonnet", effort="medium",
                          session="0d1e2f30-old")
t_old2 = teamlib.task_add(con, "List the recipe API endpoints the mobile app calls", "lead", status="doing",
                          feature="recipe-search", owner="explore-api", owner_type="Explore", model="haiku",
                          effort="low", session="0d1e2f30-old")
at(25 * 60)
teamlib.task_move(con, t_old1, "done", "lead", note="thumbnails 40% smaller, screenshots attached", session="0d1e2f30-old")
teamlib.task_move(con, t_old2, "done", "lead", note="11 endpoints, 3 unused", session="0d1e2f30-old")
p = ritual("retro", yesterday, f"""# Retro — {yesterday}

## What worked
- One worktree per feature in `recipe-web` and `recipe-api`: no file conflicts.
- Codex as a second reviewer caught a missing null check the first review passed.

## What did not
1. A brief said "make search fast" with no number; the engineer guessed.
2. Two cards sat in review over lunch with nobody accepting them.

## Actions
| Action | Owner | When |
|---|---|---|
| Every brief names a measurable target | lead | {today} |
| Accept review cards before starting new work | lead | {today} |
| Run flaky tests 200 times before calling them fixed | platform-sre | always |

```text
merged: 4 PR · reviewed: 5 · reverted: 0
```
""")
teamlib.emit(con, "ritual", "lead", "retro written", session="0d1e2f30-old", agent_id="lead:0d1e2f30",
             actor_type="team-lead", data={"kind": "retro", "path": str(p)})
ritual("reflect", yesterday, f"""# Reflection — {yesterday}

The lead **answered questions the brief should have answered**. Next time:

- put the acceptance test in the brief;
- long CI logs → `codex`;
- see [an outside example](https://example.com/notes) and `teamlib.py`.

> A question from an agent is a missing line in the brief.
""")
ritual("planning", two_days, f"""# Planning — {two_days}

## Goals for the week
- [ ] Recipe search with ingredient and cooking-time filters
- [ ] Stop the flaky checkout test
- [x] WebP thumbnails
""")

at(200)
p = ritual("daily", today, f"""# Daily — {today}

## Yesterday
- Thumbnails are WebP now, pages load faster.
- API audit: 11 endpoints, 3 unused.

## Today
- **recipe-search**: search API and a results page with filters.
- **flaky-ci**: find why `test_checkout_totals` fails now and then.

## Risks
- The prod index migration is the user's step.
- Two agents in `recipe-api`: separate worktrees, no shared files.
""")
ritual("daily", yesterday, f"# Daily — {yesterday}\n\n- WebP thumbnails\n- Recipe API audit\n")
ritual("planning", today, f"""# Planning — {today}

1. Split the request into two features.
2. Each feature gets its own worktree and reviewer.
3. Codex reviews every diff as well.

### Risks
- `charts/api/values.yaml` is touched by both SRE and delivery → one owner.
""")

at(180)
teamlib.emit(con, "session", "lead", "session started", **who("lead"))
register("lead", "team lead")
teamlib.emit(con, "ritual", "lead", "daily written", data={"kind": "daily", "path": str(p)}, **who("lead"))
at(179)
teamlib.emit(con, "request", "user",
             "Two things in parallel: 1) recipe search with ingredient and cooking-time filters; "
             "2) find and fix the flaky checkout test in CI. Report when both pass review.",
             session=S1, actor_type="user", target="lead")
at(178)
say("lead", "decision", "Two features in parallel: recipe-search (software-engineer + delivery-engineer) and flaky-ci "
    "(platform-sre). Reviewer + codex check each diff. One worktree per repo and feature.")
t_res = task("Research search: Postgres full-text vs a search service", "doing", "recipe-search", "researcher-search")
t_ci = task("Fix flaky test_checkout_totals in CI", "todo", "flaky-ci", "sre-ci")
t_index = task("Search index settings in the deploy chart", "todo", "recipe-search", "delivery-index")
t_dash = task("Dashboard panel: search latency", "backlog", "recipe-search")
t_alert = task("Alert on search error rate", "backlog", "recipe-search")
t_api = task("Search API: query parser + types", "todo", "recipe-search", "eng-search")
t_ui = task("Search page: results with filters", "todo", "recipe-search", "eng-search")
t_e2e = task("Search page: e2e + screenshots at 3 viewports", "todo", "recipe-search")
t_saved = task("Saved searches", "backlog", "recipe-search")
t_syn = task("Ingredient synonyms", "backlog", "recipe-search")
t_apply = task("Prod migration for the search index", "backlog", "recipe-search", "lead")

at(177)
spawn("researcher-search", "Search options",
      "Goal: pick how recipe search works for 1.2M recipes.\n\nScope: compare Postgres full-text search with a separate "
      "search service: latency, ranking, operating cost. Out of scope: any change.\n\n"
      "Knowledge: .claude/knowledge/INDEX.md, entries tagged services.\n\n"
      "Report: recommendation + schema change + risks, max 15 lines.", task=t_res, background=True)
for i, (n, tg) in enumerate([("Read", ".claude/knowledge/INDEX.md"), ("WebFetch", "postgresql.org/docs/current/textsearch-indexes.html"),
                             ("Grep", "tsvector in recipe-api"), ("Read", "recipe-api/db/schema.sql")]):
    at(176 - i)
    tool("researcher-search", n, tg, t_res)
at(175)
spawn("explore-ui", "Find list and filter components",
      "Find existing list, filter and empty-state components in recipe-web (src/components, src/pages). List paths and props. "
      "Read-only.", task=t_api)
tool("explore-ui", "Grep", "Filter|EmptyState in src/")
at(174)
tool("explore-ui", "Read", "src/pages/recipes/RecipeList.tsx")
at(172)
stop("explore-ui", "3 reusable components: ResultList, ChipFilter, EmptyState",
     "Found:\n- src/components/ResultList.tsx (virtualized)\n- src/components/filters/ChipFilter.tsx\n"
     "- src/components/EmptyState.tsx\nNo search page yet. RecipeList.tsx filters on the client only.", t_api)

at(170)
spawn("eng-search", "Search page",
      "Goal: a search page in recipe-web with results and filters (ingredients, max cooking time).\n\n"
      "Why: users scroll the whole list to find a recipe; the API has no search yet.\n\n"
      f"Scope: {t_api} API client + types, then {t_ui} results + filters. Reuse ResultList, ChipFilter, EmptyState "
      f"(see the explore-ui report). Out of scope: saved searches ({t_saved}).\n\nConstraints: worktree "
      "recipe-web-proj-140 on branch proj-140-search; no push; zero comments in code; screenshots at "
      "desktop/tablet/phone before review.\n\nTarget: results in under 300 ms at p95 on the staging data.\n\n"
      "Report: files changed, lint/test output, screenshot paths, anything unverified with the reason.",
      task=t_api, isolation="worktree", background=True)
move(t_api, "doing")
at(169)
spawn("sre-ci", "Flaky checkout test",
      "Goal: test_checkout_totals passes 200 runs in a row in CI.\n\nScope: recipe-api tests and the CI workflow; reproduce "
      "first, then fix. Do NOT change the prod database.\n\n"
      "Constraints: worktree recipe-api-proj-152, branch proj-152-flaky-checkout. Any change to shared CI images → "
      "stop and raise a blocker.\n\nReport: root cause with evidence, diff summary, the 200-run result.",
      task=t_ci, isolation="worktree", background=True)
move(t_ci, "doing")
at(168)
spawn("delivery-index", "Search index settings",
      "Goal: the API chart gets the settings the search index needs (work_mem, a migration job).\n\n"
      "Scope: recipe-deploy charts/api/values.yaml and the migration job template. Coordinate with sre-ci on the "
      "test database settings.\n\nConstraints: never print secret values; read keys with jq paths only.\n\nReport: diff, "
      "rendered helm template for the job, rollout plan.", task=t_index, isolation="worktree", background=True)
move(t_index, "doing")

at(166)
tool("eng-search", "Bash", "git worktree add ../recipe-web-proj-140 -b proj-140-search", t_api)
at(165)
tool("sre-ci", "Read", "recipe-api-proj-152/tests/test_checkout.py", t_ci)
at(164)
say("sre-ci", "ask", "@delivery-index does charts/api/values.yaml also set the database for the CI test job, or only "
    "for staging and prod? I may need a separate test database.", target="delivery-index", task=t_ci)
at(163)
tool("delivery-index", "Bash", "helm template api ./charts/api | yq '.. | select(has(\"DATABASE_URL\"))'", t_index)
at(162)
say("delivery-index", "answer", "Only staging and prod. CI reads tests/docker-compose.yml. I own charts/api/values.yaml; "
    "you own the tests and the workflow.", target="sre-ci", task=t_index)
claim("delivery-index", DEPLOY, f"{DEPLOY}-proj-152", "proj-152-index-settings", "charts/api/values.yaml", t_index)
at(160)
tool("eng-search", "Write", "src/api/search.ts", t_api)
claim("eng-search", WEB, f"{WEB}-proj-140", "proj-140-search", "src/api/search.ts", t_api)
at(158)
say("eng-search", "ask", "Search returns up to 1.2M rows. Page on the server, or load everything and filter in the "
    "browser?", target="lead", task=t_api)
at(156)
say("lead", "answer", "Server side, 30 per page, and keep the query in the URL so a search can be shared.",
    target="eng-search", task=t_api)
at(152)
tool("sre-ci", "Edit", "tests/conftest.py", t_ci)
claim("sre-ci", API, f"{API}-proj-152", "proj-152-flaky-checkout", "tests/conftest.py", t_ci)
at(151)
teamlib.emit(con, "conflict", "sre-ci", "Edit denied: recipe-deploy/charts/api/values.yaml is held by delivery-index "
             "(worktree recipe-deploy-proj-152, 11 min)", target="delivery-index", task=t_ci,
             data={"path": f"{DEPLOY}-proj-152/charts/api/values.yaml", "holder": "delivery-index"}, **who("sre-ci"))
at(150)
stop("researcher-search", "Postgres full-text with a GIN index; no search service yet",
     "Recommendation: Postgres full-text search.\n\n- tsvector column on recipes(title, ingredients)\n- GIN index\n"
     "- websearch_to_tsquery for user input\n\nAt 1.2M recipes the index is about 350 MB and p95 stays under 120 ms on staging. "
     "A separate search service only pays off with typo tolerance, which nobody asked for.\n\nRisk: building the index locks writes "
     "unless it is built concurrently.", t_res)
move(t_res, "done", "Postgres full-text, GIN")
at(149)
say("lead", "say", "Research is in: Postgres full-text with a GIN index. @eng-search @delivery-index plan for that.",
    target="all", task=t_api)

at(140)
tool("eng-search", "Bash", "npm run lint && npm test -- search", t_api)
at(138)
say("eng-search", "handoff", f"{t_api} search client ready for review: src/api/search.ts, src/types/search.ts. "
    "Lint clean, 12 tests pass.", target="lead", task=t_api)
move(t_api, "review")
at(137)
spawn("reviewer-search", "Review the search client",
      "Review recipe-web-proj-140 (src/api/search.ts, src/types/search.ts) against the recipe-api search endpoint. Check "
      "paging, empty and error states, types against real responses. Verdict PASS/FAIL with file:line findings.", task=t_api)
for i, (n, tg) in enumerate([("Bash", "git -C recipe-web-proj-140 diff main --stat"), ("Read", "src/api/search.ts"),
                             ("Read", "recipe-api/app/routes/search.py")]):
    at(136 - i)
    tool("reviewer-search", n, tg, t_api)
at(130)
say("reviewer-search", "report", "FAIL — 2 findings.\n1. src/api/search.ts:31 sends `cook_time`, the API reads "
    "`max_minutes` → the time filter is ignored.\n2. src/types/search.ts:12 types `ingredients` as a string; the API "
    "returns an array → chips render as one long label.", target="lead", task=t_api, data={"verdict": "FAIL"})
move(t_api, "doing", "reviewer FAIL: filter name, ingredients type")
at(129)
teamlib.emit(con, "dm", "lead", f"Reviewer FAIL on {t_api}, both findings hold:\n"
             "1. src/api/search.ts:31 — the API reads `max_minutes`, not `cook_time`.\n"
             "2. src/types/search.ts:12 — `ingredients` is an array; map it to chips.\n\n"
             "I checked both in recipe-api/app/routes/search.py. Fix both, add a test for the time filter, "
             f"then hand off again. Leave the page alone until then — {t_ui} waits for this.",
             target="eng-search", task=t_api, **who("lead"))
at(124)
tool("eng-search", "Edit", "src/api/search.ts", t_api)
at(122)
tool("eng-search", "Edit", "src/types/search.ts", t_api)
claim("eng-search", WEB, f"{WEB}-proj-140", "proj-140-search", "src/types/search.ts", t_api)
at(121)
tool("lead", "Edit", "recipe-web/src/api/search.ts")
claim("lead", WEB, WEB, "main", "src/api/search.ts")
teamlib.emit(con, "overlap", "lead", "recipe-web src/api/search.ts is changed in two worktrees: "
             "eng-search (proj-140-search) and lead (main)", target="eng-search",
             data={"rel": "src/api/search.ts", "repo": "recipe-web",
                   "holders": [{"actor": "eng-search", "branch": "proj-140-search"}, {"actor": "lead", "branch": "main"}]},
             **who("lead"))
at(120)
say("lead", "say", "I opened the file on main by mistake and closed it without saving. It is yours, @eng-search.",
    target="eng-search")
con.execute("DELETE FROM claims WHERE agent_id=?", (LEAD1,))
at(116)
tool("eng-search", "Bash", "npm test -- search", t_api)
say("eng-search", "handoff", "Both findings fixed, plus a test for the time filter. 13 tests pass.",
    target="reviewer-search", task=t_api)
move(t_api, "review", "both findings fixed")
at(112)
tool("reviewer-search", "Read", "src/api/search.ts", t_api)
at(110)
say("reviewer-search", "report", "PASS — both findings fixed and covered by tests; nothing new.",
    target="lead", task=t_api, data={"verdict": "PASS"})
stop("reviewer-search", "PASS after one FAIL round",
     "Round 1: FAIL (filter name, ingredients type).\nRound 2: PASS. src/api/search.ts:31 sends max_minutes, "
     "ingredients is string[], search.test.ts covers the time filter.", t_api)
move(t_api, "done", "review PASS")
move(t_ui, "doing")
con.execute("UPDATE agents SET task=? WHERE agent_id=?", (t_ui, AGENTS["eng-search"][0]))

at(100)
tool("sre-ci", "Bash", "pytest tests/test_checkout.py -k totals --count 200 -p no:randomly", t_ci)
at(96)
tool("delivery-index", "Bash", "git checkout -- tests in recipe-api-proj-152", t_index)
teamlib.emit(con, "guard", "delivery-index", "recipe-api-proj-152 (proj-152-flaky-checkout): git operation blocked — "
             "sre-ci editing here", target="sre-ci", task=t_index,
             data={"command": "git checkout -- tests", "worktree": f"{API}-proj-152"}, **who("delivery-index"))
at(95)
say("delivery-index", "say", "Wrong directory on my side; my changes are in recipe-deploy-proj-152.", target="sre-ci", task=t_index)
at(90)
tool("sre-ci", "Bash", "pytest tests/test_checkout.py --count 200", t_ci)
at(88)
say("sre-ci", "blocker", "Root cause: test_checkout_totals and test_coupons share one database and run in parallel; "
    "3 of 200 runs fail. The clean fix gives every worker its own database, which changes the CI compose file for "
    "all pipelines. Stopping for the lead's call.", target="lead", task=t_ci)
move(t_ci, "blocked", "fix touches every pipeline")
at(84)
tool("lead", "Bash", "git -C recipe-api-proj-152 diff main -- tests/docker-compose.yml")
at(82)
say("lead", "decision", "One database per test worker is the right fix and only CI uses that compose file. Go ahead.",
    target="sre-ci", task=t_ci)
move(t_ci, "doing", "per-worker test databases approved")
at(80)
teamlib.emit(con, "guard", "lead", "fact-check: blocked slack_send_message_draft — Fact-check gate: `slack_send_message_draft` "
             "carries 2 checkable facts and this exact text has no PASS in the last 24h: number \"3 of 200\", weekday \"Thursday\".",
             target="slack_send_message_draft", **who("lead"),
             data={"gate": "factcheck", "hash": "5b1e9c0d2a7f4e36", "tokens": 2, "tool": "mcp__slack__slack_send_message_draft"})
at(78)
teamlib.emit(con, "needs_user", "lead", "Your decision: give every CI test worker its own database? It changes the CI "
             "compose file for all pipelines.",
             data={"message": "Approve one database per CI test worker? Only the CI compose file changes."}, **who("lead"))
at(76)
teamlib.emit(con, "answer", "user", "Yes, go ahead.", session=S1, actor_type="user", target="lead")
at(70)
tool("delivery-index", "Bash", "helm template api ./charts/api --show-only templates/migrate-job.yaml", t_index)
at(66)
say("delivery-index", "handoff", "Chart ready: work_mem 64MB for the migration job, the index is built concurrently, "
    "the job runs once per release.", target="lead", task=t_index)
move(t_index, "review")
stop("delivery-index", "chart ready, handed off for review",
     "Diff: recipe-deploy charts/api/values.yaml +9 -2, templates/migrate-job.yaml new.\n"
     "Rendered job: one pod, CREATE INDEX CONCURRENTLY.\nRollout: staging first, prod after the user runs the migration.", t_index)
con.execute("DELETE FROM claims WHERE agent_id=?", (AGENTS["delivery-index"][0],))
at(64)
spawn("reviewer-ci", "Review the index chart and the CI fix",
      "Review recipe-deploy-proj-152 (charts/api) and recipe-api-proj-152 (tests, CI compose) together: the index is "
      "built concurrently, no secret printed, the CI change does not touch prod. Verdict PASS/FAIL with file:line.",
      task=t_index)
at(63)
spawn("codex-2nd", "Second opinion on the index migration",
      "Independent review of the same two diffs. Focus: table locks during the index build, the migration on a "
      "1.2M-row table. Report findings with evidence only.", task=t_index, background=True)
move(t_apply, "waiting_user", "Run the prod migration: `! make -C ~/work/recipe-api migrate-prod`", actor="lead")
move(t_dash, "blocked", f"needs search metrics from {t_api} on staging")
move(t_syn, "dropped", "nobody asked for typo tolerance yet")

at(50)
tool("eng-search", "Write", "src/pages/search/SearchPage.tsx", t_ui)
claim("eng-search", WEB, f"{WEB}-proj-140", "proj-140-search", "src/pages/search/SearchPage.tsx", t_ui, 0)
at(44)
teamlib.emit(con, "session", "lead", "session started", **who("lead2"))
register("lead2", "team lead")
at(43)
teamlib.emit(con, "request", "user", "Look at the open image-upload issues in recipe-web and tell me what we can fix this week.",
             session=S2, actor_type="user", target="lead")
t_up = task("Triage image-upload issues", "todo", "uploads", "triage-uploads", session=S2)
at(42)
spawn("triage-uploads", "Triage image-upload issues",
      "List open issues labelled uploads in example-org/recipe-web: reproduce each on staging, group by cause, "
      "estimate the fix. Read-only: no comments, no labels.\n\nReport: table issue | cause | fix | size.",
      task=t_up, background=True, lead="lead2")
teamlib.task_move(con, t_up, "doing", "lead", session=S2)
for i, (n, tg) in enumerate([("Bash", "gh issue list -R example-org/recipe-web --label uploads"),
                             ("Bash", "gh issue view 231"), ("Bash", "gh issue view 233"), ("Bash", "gh issue view 236")]):
    at(40 - i * 2)
    tool("triage-uploads", n, tg, t_up)
at(30)
say("triage-uploads", "report", "4 issues, 2 causes.\n#231 HEIC photos rejected — no converter\n#233 413 on files over 8 MB "
    "— proxy limit\n#236 progress bar stuck — same proxy limit\n#238 duplicate photos after retry — client retries without an id",
    target="lead", task=t_up)
at(28)
for i, (n, tg) in enumerate([("Read", "charts/api/values.yaml"), ("Read", "templates/migrate-job.yaml"),
                             ("Grep", "CONCURRENTLY")]):
    at(28 - i * 3)
    tool("reviewer-ci", n, tg, t_index)
at(21)
tool("codex-2nd", "Bash", "codex exec --sandbox read-only \"review the index migration diffs\"", t_index)
at(20)
say("codex-2nd", "report", "1 finding: the migration job has no timeout; a concurrent index build on 1.2M rows can run "
    "for minutes and the release waits on it. Suggest activeDeadlineSeconds and a separate job.", target="lead", task=t_index)
at(18)
teamlib.emit(con, "dm", "lead", "codex says the migration job can hold the release for minutes. Please check this on the "
             "staging data and tell me whether it holds:\n- how long the concurrent build takes on staging\n"
             "- whether the release waits for the job\n- whether a timeout would leave a half-built index",
             target="reviewer-ci", task=t_index, **who("lead"))
at(12)
tool("sre-ci", "Edit", "tests/docker-compose.yml", t_ci)
claim("sre-ci", API, f"{API}-proj-152", "proj-152-flaky-checkout", "tests/docker-compose.yml", t_ci, 0)
at(9)
tool("eng-search", "Edit", "src/pages/search/SearchFilters.tsx", t_ui)
claim("eng-search", WEB, f"{WEB}-proj-140", "proj-140-search", "src/pages/search/SearchFilters.tsx", t_ui, 0)
at(8)
say("eng-search", "say", "Results and filters render; taking screenshots at three widths now.", target="lead", task=t_ui)
at(6)
tool("codex-2nd", "Bash", "psql $STAGING -c 'select count(*) from recipes'", t_index)
at(5)
tool("triage-uploads", "Bash", "gh issue view 238 --json body", t_up)
at(3)
teamlib.task_move(con, t_up, "waiting_user", "lead", note="Product: raise the upload limit to 20 MB, or keep 8 MB and shrink photos in the browser?", session=S2)
teamlib.emit(con, "needs_user", "lead", "Waiting for you: raise the upload limit to 20 MB, or keep 8 MB and shrink photos "
             "in the browser (#233, #236)?", data={"message": "Upload limit: 20 MB, or 8 MB with in-browser resizing? "
                                                  "It decides the fix for #233 and #236."}, task=t_up, **who("lead2"))
at(1.2)
tool("reviewer-ci", "Read", "templates/migrate-job.yaml:24", t_index)
at(0.6)
tool("sre-ci", "Bash", "pytest tests/test_checkout.py --count 200 -n 4", t_ci)
at(0.3)
tool("eng-search", "Bash", "chrome --headless --screenshot --window-size=390,844", t_ui)
tool("lead", "Agent", "reviewer-ci")

for handle in ("explore-ui", "researcher-search"):
    con.execute("DELETE FROM claims WHERE agent_id=?", (AGENTS[handle][0],))
con.close()
print(f"demo db: {teamlib.DB}\nrituals: {teamlib.RITUALS}")
