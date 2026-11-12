import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import teamlib as T
import hook

SESSION = "sess-factcheck-test"
NOW = "2027-03-03T10:00:00Z"
REF = datetime(2027, 3, 3, 10, 0)
DRAFT = "mcp__plugin_slack_slack__slack_send_message_draft"
WRONG = ("From this week the backup job prunes old database snapshots every Sunday at 03:00 UTC, "
         "the first run is Sunday, March 14.")
RIGHT = WRONG.replace("March 14", "March 7")
CLI = "python3 ~/project/.claude/team/team.py"
AGENTS = [("rev1", "reviewer#rev1", "reviewer"), ("rev2", "reviewer#rev2", "reviewer"), ("eng1", "software-engineer#eng1", "software-engineer"),
          ("cdx1", "codex#cdx1", "codex")]


def claim(token, source="crontab -l -u backup", observed="0 0 3 * * 0 /usr/local/bin/prune-snapshots --keep 7", kind="primary"):
    return {"token": token, "claim": token, "source": source, "observed": observed, "source_kind": kind}


SCHEDULE = [claim("Sunday"), claim("03:00 UTC")]
FIRST = claim("March 7", "python3: first firing of '0 0 3 * * 0' after 2027-03-03T10:00Z", "2027-03-07 03:00:00 UTC, Sunday")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patches = [mock.patch.object(T, "DB", self.dir / "team.db"), mock.patch.object(T, "STATE", self.dir),
                        mock.patch.object(T, "FACT_DIR", self.dir / "factcheck"), mock.patch.dict(os.environ, {"TEAM_NOW": NOW})]
        for p in self.patches:
            p.start()
        con = T.connect()
        for aid, handle, atype in AGENTS:
            con.execute("INSERT INTO agents(agent_id,session,handle,type,status,started,last_ts,cursor,linked) VALUES(?,?,?,?,?,?,?,?,?)",
                        (aid, SESSION, handle, atype, "running", T.now(), T.now(), 0, "meta"))
        con.close()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run_hook(self, tool, ti, agent=None, cwd=None):
        inp = {"hook_event_name": "PreToolUse", "session_id": SESSION, "tool_name": tool, "tool_input": ti, "cwd": str(cwd or self.dir),
               "tool_use_id": "tu"}
        if agent:
            inp["agent_id"] = agent
        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(inp))), contextlib.redirect_stdout(buf):
            hook.main()
        out = buf.getvalue()
        return json.loads(out) if out else {}

    def reason(self, out):
        h = out.get("hookSpecificOutput") or {}
        return h.get("permissionDecisionReason") if h.get("permissionDecision") == "deny" else None

    def bash(self, cmd, agent=None, cwd=None):
        return self.reason(self.run_hook("Bash", {"command": cmd}, agent, cwd))

    def cli(self, *args, env=None):
        e = dict(os.environ, TEAM_DB=str(self.dir / "team.db"), TEAM_NOW=NOW, CLAUDE_CODE_SESSION_ID=SESSION)
        e.pop("TEAM_AS", None)
        e.update(env or {})
        return subprocess.run([sys.executable, str(ROOT / "team.py"), "factcheck", *args], capture_output=True, text=True, env=e)

    def write(self, name, text):
        p = self.dir / name
        p.write_text(text)
        return p

    def claims(self, name, items):
        return str(self.write(name, json.dumps(items)))

    def sql(self, q, *a):
        con = T.connect()
        try:
            return con.execute(q, a).fetchall()
        finally:
            con.close()

    def guards(self):
        return [json.loads(r["data"] or "{}") for r in self.sql("SELECT data FROM events WHERE kind='guard'")]

    def draft(self, text, agent=None):
        return self.reason(self.run_hook(DRAFT, {"channel_id": "C1", "message": text}, agent))

    def passed(self, text, items, who="reviewer#rev1", name="t.txt"):
        f = self.write(name, text)
        return self.cli("pass", str(f), "--as", who, "--claims", self.claims(name + ".json", items))


class GateTest(Base):
    def test_wrong_first_run_flow(self):
        why = self.draft(WRONG)
        self.assertIn('date "March 14"', why)
        self.assertIn("calendar check already fails", why)
        saved = T.FACT_DIR / f"{T.fact_hash(WRONG)}.txt"
        self.assertEqual(saved.read_text(), WRONG)
        self.assertTrue(any(g.get("hash") == T.fact_hash(WRONG) for g in self.guards()))
        r = self.cli("pass", str(saved), "--as", "reviewer#rev1", "--claims", self.claims("p.json", SCHEDULE))
        self.assertEqual(r.returncode, 1)
        self.assertIn('date "March 14" is covered by no valid primary claim', r.stderr)
        r = self.cli("pass", str(saved), "--as", "reviewer#rev1", "--claims", self.claims("f.json", SCHEDULE + [claim("March 14")]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("fires first on 2027-03-07 03:00:00 UTC", r.stderr)
        self.assertTrue(self.draft(WRONG))
        self.assertTrue(self.draft(RIGHT))
        fixed = T.FACT_DIR / f"{T.fact_hash(RIGHT)}.txt"
        r = self.cli("pass", str(fixed), "--as", "reviewer#rev1", "--claims", self.claims("g.json", SCHEDULE + [FIRST]))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.draft(RIGHT))
        self.assertIsNone(self.draft("  " + RIGHT.replace(" every", "\n every") + "\n"))
        self.assertTrue(self.draft(RIGHT.replace("03:00", "04:00")))
        self.assertTrue(self.draft(RIGHT, agent="rev1"))

    def test_expiry_and_fail_after_pass(self):
        self.assertEqual(self.passed(RIGHT, SCHEDULE + [FIRST]).returncode, 0)
        self.assertIsNone(self.draft(RIGHT))
        self.sql("UPDATE factchecks SET ts=?", T.now() - T.FACT_TTL - 60)
        self.assertTrue(self.draft(RIGHT))
        self.assertEqual(self.passed(RIGHT, SCHEDULE + [FIRST], name="again.txt").returncode, 0)
        self.assertIsNone(self.draft(RIGHT))
        self.assertEqual(self.cli("fail", str(self.dir / "again.txt"), "--as", "reviewer#rev2", "--note", "date wrong").returncode, 0)
        self.assertTrue(self.draft(RIGHT))

    def test_no_tokens_ungated(self):
        self.assertIsNone(self.draft("ок, дякую"))
        self.assertIsNone(self.reason(self.run_hook("mcp__plugin_slack_slack__slack_send_message", {"channel_id": "D1", "message": "ок, дякую"})))
        out = subprocess.run([sys.executable, str(ROOT / "team.py"), "factcheck", "tokens", "-"], input="ок, дякую", capture_output=True, text=True,
                             env=dict(os.environ, TEAM_DB=str(self.dir / "team.db"))).stdout
        self.assertIn("no checkable facts", out)

    def test_too_long_denied_until_waived(self):
        text = "PROJ-58 " + "x " * (T.FACT_MAX // 2 + 10)
        why = self.draft(text)
        self.assertIn("over the 100000 limit", why)
        self.assertIn("Split the text", why)
        f = self.write("big.md", text)
        self.assertIn("over the 100000 limit", self.bash(f"gh pr comment 3 --body-file {f}"))
        self.assertIn("longer than", self.cli("pass", str(f), "--as", "reviewer#rev1", "--claims", "c.json").stderr)
        self.assertEqual(self.cli("waive", str(f), "--as", "lead", "--reason", "log dump").returncode, 0)
        self.assertIsNone(self.draft(text))
        self.assertIsNone(self.bash(f"gh pr comment 3 --body-file {f}"))
        self.assertIn("over the 100000 limit", self.draft("ок " * (T.FACT_MAX // 2)))

    def test_fails_open(self):
        with mock.patch.object(T, "fact_tokens", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.draft(RIGHT))
        self.assertIn("RuntimeError: boom", (self.dir / "factcheck-errors.log").read_text())
        self.assertTrue(any(g.get("error") for g in self.guards()))

    def test_mcp_fields(self):
        edit = "mcp__claude_ai_Atlassian_Rovo__editJiraIssue"
        self.assertTrue(self.reason(self.run_hook(edit, {"issueIdOrKey": "PROJ-2", "fields": {"description": "Pruned 12.5 GiB"}})))
        self.assertIsNone(self.reason(self.run_hook(edit, {"issueIdOrKey": "PROJ-2", "fields": {"labels": ["v3.4.1"]}})))
        tr = "mcp__claude_ai_Atlassian_Rovo__transitionJiraIssue"
        self.assertIsNone(self.reason(self.run_hook(tr, {"issueIdOrKey": "PROJ-2", "transition": {"id": "31"}})))
        self.assertTrue(self.reason(self.run_hook(tr, {"issueIdOrKey": "PROJ-2", "transition": {"id": "31"},
                                                       "update": {"comment": [{"add": {"body": "Deployed v2.8.0"}}]}})))
        self.assertTrue(self.reason(self.run_hook("mcp__claude_ai_Atlassian_Rovo__addCommentToJiraIssue", {"issueIdOrKey": "PROJ-2", "commentBody": "Done in #57"})))
        self.assertTrue(self.reason(self.run_hook("mcp__claude_ai_Atlassian_Rovo__addWorklogToJiraIssue",
                                                  {"issueIdOrKey": "PROJ-2", "timeSpent": "2h", "commentBody": "Checked 7 snapshots"})))
        self.assertTrue(self.reason(self.run_hook("mcp__x__createJiraIssue", {"summary": "s", "additional_fields": {"customfield_1": "keep 30 days"}})))
        self.assertIsNone(self.reason(self.run_hook("mcp__x__createJiraIssue", {"summary": "s", "additional_fields": {"labels": ["v0.9.2"],
                                                                                                                   "priority": {"name": "P1"}}})))
        self.assertTrue(self.reason(self.run_hook("mcp__claude_ai_Atlassian_Rovo__updateConfluencePage",
                                                  {"pageId": "1", "body": "ok", "versionMessage": "bump to v2.8.0"})))
        adf = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Pruned 12.5 GiB"}]}]}
        self.assertIn('number "12.5 GiB"', self.reason(self.run_hook("mcp__x__createJiraIssue", {"summary": "s", "description": adf})))
        for name in ("slack_read_channel", "slack_search_public", "slack_add_reaction"):
            self.assertIsNone(self.reason(self.run_hook(f"mcp__plugin_slack_slack__{name}", {"channel_id": "C1", "query": "PROJ-58 Mar 9"})))

    def test_stats(self):
        self.draft(RIGHT)
        self.bash(f"{CLI} factcheck pass x.txt --as software-engineer#eng1 --claims c", "eng1")
        f = self.write("n.txt", RIGHT)
        self.cli("fail", str(f), "--as", "reviewer#rev1", "--note", "date wrong")
        self.cli("waive", str(f), "--as", "lead", "--reason", "test")
        env = dict(os.environ, TEAM_DB=str(self.dir / "team.db"))
        out = json.loads(subprocess.run([sys.executable, str(ROOT / "team.py"), "stats", "--days", "1"], capture_output=True, text=True, env=env).stdout)
        self.assertEqual(out["factcheck"], {"pass": 0, "fail": 1, "waived": 1, "blocked": 1})


class ClaimsTest(Base):
    def test_exact_coverage(self):
        text = "Pruned 4 GiB in PROJ-21 across the 5 replicas"
        r = self.passed(text, [claim(text)])
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not one of the facts", r.stderr)
        r = self.passed(text, [claim("PROJ-21"), claim("4 GiB")])
        self.assertIn('number "5 replicas" is covered by no valid primary claim', r.stderr)
        self.assertEqual(self.passed(text, [claim("PROJ-21"), claim("4 GiB"), claim("5 replicas")]).returncode, 0)

    def test_derived_or_empty_source_refused(self):
        r = self.passed(RIGHT, SCHEDULE + [claim("March 7", "PR #88 body", "first run March 7", "derived")])
        self.assertIn("only primary sources count", r.stderr)
        r = self.passed(RIGHT, SCHEDULE + [claim("March 7", "", "x")])
        self.assertIn("empty source", r.stderr)

    def test_cron_first_run_not_firing(self):
        text = "Pruning runs on cron `0 0 3 * * 0`; the first run is Saturday, March 6."
        r = self.passed(text, [claim("0 0 3 * * 0"), claim("Saturday"), claim("March 6")])
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not a firing day of cron `0 0 3 * * 0`", r.stderr)

    def test_weekday_date_contradiction(self):
        r = self.passed("Restore drill: Monday, March 20 at 14:00 UTC.", [claim("Monday"), claim("March 20"), claim("14:00 UTC")])
        self.assertEqual(r.returncode, 1)
        self.assertIn('"Monday" next to "March 20": 2027-03-20 is a Saturday', r.stderr)

    def test_pass_checks_identity_and_authorship(self):
        f = self.write("n.txt", RIGHT)
        c = self.claims("c.json", SCHEDULE + [FIRST])
        self.assertIn("explicit --as", self.cli("pass", str(f), "--claims", c, env={"TEAM_AS": "reviewer#rev1"}).stderr)
        self.assertIn("only a reviewer or codex", self.cli("pass", str(f), "--as", "software-engineer#eng1", "--claims", c).stderr)
        self.run_hook("Write", {"file_path": str(self.dir / "draft.md"), "content": RIGHT}, "rev2")
        self.assertIn("wrote or edited", self.cli("pass", str(f), "--as", "reviewer#rev2", "--claims", c).stderr)
        self.assertEqual(self.cli("pass", str(f), "--as", "codex#cdx1", "--claims", c).returncode, 0)

    def test_waive_needs_explicit_lead(self):
        f = self.write("n.txt", RIGHT)
        self.assertIn("explicit --as lead", self.cli("waive", str(f), "--reason", "x").stderr)
        self.assertIn("explicit --as lead", self.cli("waive", str(f), "--as", "reviewer#rev1", "--reason", "x").stderr)
        self.assertEqual(self.cli("waive", str(f), "--as", "lead", "--reason", "internal").returncode, 0)
        self.assertIsNone(self.draft(RIGHT))
        self.assertTrue(any(g.get("waived") == T.fact_hash(RIGHT) for g in self.guards()))


class WhoTest(Base):
    def test_who_may_pass(self):
        f = self.write("n.txt", RIGHT)
        cmd = lambda who: f"{CLI} factcheck pass {f} --as {who} --claims c.json"
        self.assertIn("only a reviewer or codex", self.bash(cmd("lead")))
        self.assertIn("only a reviewer or codex", self.bash(cmd("software-engineer#eng1"), "eng1"))
        self.assertIn("as yourself", self.bash(cmd("reviewer#rev2"), "rev1"))
        self.assertIsNone(self.bash(cmd("reviewer#rev1"), "rev1"))
        self.assertIsNone(self.bash(f"cd {self.dir} && {CLI} factcheck pass n.txt --as codex#cdx1 --claims c.json", "cdx1"))
        self.assertIn("only a reviewer or codex", self.bash(f"bash -c '{cmd('lead')}'"))
        self.assertIn("only a reviewer or codex", self.bash(f"env FOO=1 {cmd('software-engineer#eng1')}", "eng1"))
        self.assertIn("as yourself", self.bash(f"TEAM_AS=reviewer#rev1 {CLI} factcheck pass {f} --claims c.json", "eng1"))

    def test_attribution_forms(self):
        self.assertIn("cannot attribute", self.bash("python3 -m team factcheck waive /tmp/b.txt --as lead --reason x", "eng1",
                                                    ROOT))
        self.assertIn("shell variable", self.bash("T=~/project/.claude/team/team.py; python3 $T factcheck pass f --as reviewer#rev1 --claims c", "rev1"))
        self.assertIsNone(self.bash("python3 -c \"print('factcheck', 'pass')\"", "eng1"))
        self.assertIsNone(self.bash("python3 -c \"print('use team.py factcheck pass after review')\"", "eng1"))
        self.assertIsNone(self.bash("python3 - <<'EOF'\ns = 'INSERT INTO factchecks(hash) VALUES(1)'\nEOF", "eng1"))
        self.assertIn("ledger", self.bash(f"sqlite3 {self.dir}/team.db \"insert into factchecks(hash,verdict) values('x','PASS')\"", "rev1"))
        self.assertIn("explicit `--as lead`", self.bash(f"{CLI} factcheck waive /tmp/b.txt --reason x"))
        self.assertIsNone(self.bash(f"{CLI} factcheck waive /tmp/b.txt --as lead --reason x"))
        self.assertIn("only the lead", self.bash(f"{CLI} factcheck waive /tmp/b.txt --as lead --reason x", "rev1"))

    def test_author_cannot_pass(self):
        own = self.dir / "own.txt"
        self.run_hook("Write", {"file_path": str(own), "content": RIGHT}, "rev2")
        own.write_text(RIGHT)
        self.assertIn("you wrote or edited", self.bash(f"{CLI} factcheck pass {own} --as reviewer#rev2 --claims c.json", "rev2"))
        self.draft(RIGHT)
        saved = T.FACT_DIR / f"{T.fact_hash(RIGHT)}.txt"
        self.assertIn("you wrote or edited", self.bash(f"{CLI} factcheck pass {saved} --as reviewer#rev2 --claims c.json", "rev2"))
        self.assertIsNone(self.bash(f"{CLI} factcheck pass {saved} --as reviewer#rev1 --claims c.json", "rev1"))
        edited = self.write("edited.txt", WRONG)
        self.run_hook("Edit", {"file_path": str(edited), "old_string": "March 14", "new_string": "March 7"}, "rev1")
        edited.write_text(RIGHT)
        self.assertIn("you wrote or edited", self.bash(f"{CLI} factcheck pass {saved} --as reviewer#rev1 --claims c.json", "rev1"))


class ReadOnlyTest(Base):
    def test_unparseable_read_only_allowed(self):
        for agent in ("rev1", None, "eng1"):
            self.assertIsNone(self.bash("grep -n \"gh pr create\" x | awk '{print $1}", agent))
            self.assertIsNone(self.bash("grep -n \"factcheck pass\" hook.py # it's", agent))
            self.assertIsNone(self.bash("python3 - <<'EOF'\nprint('gh pr create --title PROJ-6 --body 11')\nEOF\nbash -n /dev/null", agent))
            self.assertIsNone(self.bash("echo python3 team.py factcheck waive body.txt --reason example", agent))
            self.assertIsNone(self.bash("cat > /tmp/notes.md <<'EOF'\nUse gh pr create --title PROJ-2 then wait 4 days\nEOF", agent))
            self.assertIsNone(self.bash("python3 -c \"print('gh pr create --title PROJ-2 --body 5')\"", agent))
            self.assertIsNone(self.bash("rg -n 'gh\\s+pr\\s+(create|edit)' ~/project/.claude/", agent))
        self.assertIsNone(self.bash("gh pr create --help --body-file /does-not-exist"))
        self.assertIsNone(self.bash("gh pr view 57 --json body"))
        self.assertIsNone(self.bash("gh api repos/example-org/x/pulls/46/comments --jq '.[].body'"))
        self.assertIsNone(self.bash("gh pr review 57 --approve --body 'LGTM'"))
        self.assertIsNone(self.bash("gh pr edit 57 --add-label bug"))
        self.assertIsNone(self.bash("echo 'the high road through github' && ls"))

    def test_unparseable_writes_still_denied(self):
        self.assertIn("does not parse", self.bash("gh pr create --title 'PROJ-2 --body x"))
        self.assertIn("heredoc", self.bash("bash <<'EOF'\ngh pr comment 3 --body '30 days'\nEOF"))
        self.assertIn("does not parse", self.bash("grep x 'y ; python3 ~/project/.claude/team/team.py factcheck pass f --as reviewer#rev1", "rev1"))


class GhTest(Base):
    def test_body_sources(self):
        body = self.write("body.md", "Pruning keeps 7 snapshots per database; see PROJ-58.")
        self.assertIn('jira "PROJ-58"', self.bash(f"gh pr create --title 'PROJ-58: pruning' --body-file {body}"))
        self.assertIn("--body-file <literal path>", self.bash(f'gh pr create --title x --body "$(cat {body})"'))
        self.assertIn("not a regular file", self.bash("gh pr comment 57 --body-file - <<'EOF'\n7 snapshots\nEOF"))
        self.assertIn("not a regular file", self.bash("gh pr comment 57 --body-file /dev/stdin"))
        self.assertIn("not a regular file", self.bash("gh pr comment 57 --body-file /dev/fd/0"))
        fifo = self.dir / "pipe"
        os.mkfifo(fifo)
        self.assertIn("not a regular file", self.bash(f"gh pr comment 57 --body-file {fifo}"))
        big = self.write("big.md", "x" * (T.FACT_FILE_MAX + 1))
        self.assertIn("over 1000000 bytes: split the text", self.bash(f"gh pr comment 57 --body-file {big}"))
        self.assertIn("also used elsewhere", self.bash("cat > b.md <<'EOF'\n7 snapshots\nEOF\ngh pr comment 57 --body-file b.md"))
        self.assertIn("shell variable", self.bash("S=/x; gh pr create --title t --body-file $S/b.md"))
        self.assertIn("notes-file", self.bash("gh release create v1 --notes \"$NOTES\""))
        self.assertIn("takes the text", self.bash("gh release create v1 --generate-notes"))
        self.assertIn("takes the text", self.bash("gh pr create --fill"))
        self.assertIsNone(self.bash("gh pr comment 57 --body \"see \\`kubectl\\` output\""))
        self.assertIsNone(self.bash("gh pr comment 57 --body 'Use $HOME for it'"))
        self.assertIn('number "30 days"', self.bash("gh pr comment 57 --body 'Use $HOME for 30 days'"))

    def test_syntax_variants_gated(self):
        for cmd in ["gh --repo o/r pr comment 3 --body '30 days'", "gh -R o/r pr comment 3 --body '30 days'", "gh pr comment 3 -b'30 days'",
                    "gh pr comment 3 -b'30 days'", "env -i gh pr comment 3 --body '30 days'", "timeout 30 gh pr comment 3 --body '30 days'",
                    "echo 3 | xargs -I{} gh pr comment {} --body '30 days'", "nohup gh pr comment 3 --body='30 days'",
                    "gh pr close 3 --comment '30 days'", "gh issue close 3 -c '30 days'", "gh pr merge 3 --body '30 days'",
                    "gh pr merge 3 --subject 'PROJ-2: x'", "/opt/homebrew/bin/gh pr comment 3 --body '30 days'"]:
            self.assertTrue(self.bash(cmd), cmd)

    def test_gh_api(self):
        self.assertIn('version "v2.8.0"', self.bash("gh api -X POST repos/o/r/issues/46/comments -f body='Deployed v2.8.0'"))
        self.assertIn('version "v2.8.0"', self.bash("gh api repos/o/r/pulls/46/comments/5/replies -f body='Deployed v2.8.0'"))
        self.assertIn('version "v2.8.0"', self.bash("gh api --method PATCH repos/o/r/issues/46 -f title='Ship v2.8.0'"))
        f = self.write("c.md", "Pruned 7 snapshots")
        self.assertIn('number "7 snapshots"', self.bash(f"gh api repos/o/r/issues/46/comments -F body=@{f}"))
        self.assertIn('number "30 days"', self.bash("gh api graphql -f query='mutation { addComment(input: {body: \"30 days\"}) { clientMutationId } }'"))
        self.assertIn("--input", self.bash("gh api -X POST repos/o/r/issues/46/comments --input body.json"))
        self.assertIsNone(self.bash("gh api graphql -f query='query { viewer { login } }'"))
        self.assertIsNone(self.bash("gh api -X POST repos/o/r/issues/46/labels -f 'labels[]=bug'"))
        self.assertIsNone(self.bash("gh api -X DELETE repos/o/r/issues/comments/5"))

    def test_pr_title_flow(self):
        body = self.write("body.md", "The cache warms up after 4 cold requests.")
        cmd = f"gh pr create --title 'PROJ-64: warm the cache' --body-file {body}"
        why = self.bash(cmd)
        self.assertIn('jira "PROJ-64"', why)
        saved = T.FACT_DIR / f"{T.fact_hash('PROJ-64: warm the cache' + chr(10) + body.read_text())}.txt"
        self.assertIn(str(saved), why)
        self.assertEqual(saved.read_text(), "PROJ-64: warm the cache\nThe cache warms up after 4 cold requests.")
        c = self.claims("c.json", [claim("PROJ-64", "jira getJiraIssue PROJ-64", "warm the cache"),
                                   claim("4 cold", "git show HEAD:app/cache.py | grep WARM_REQUESTS", "WARM_REQUESTS = 4")])
        r = self.cli("pass", str(saved), "--as", "reviewer#rev1", "--claims", c)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.bash(cmd))
        self.assertTrue(self.bash(cmd.replace("PROJ-64:", "PROJ-65:")))


class ExistingGuardsTest(Base):
    def test_git_guard_edit_claim_chat_identity(self):
        repo = (self.dir / "repo").resolve()
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        held = "/opt/_never_created/held.py"
        self.sql("INSERT INTO claims(path,agent_id,actor,session,repo,rel,worktree,branch,task,since,touched) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 str(repo / "a.py"), "eng1", "software-engineer#eng1", SESSION, str(repo / ".git"), "a.py", str(repo), "main", None, T.now(), T.now())
        self.sql("INSERT INTO claims(path,agent_id,actor,session,repo,rel,worktree,branch,task,since,touched) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 held, "eng1", "software-engineer#eng1", SESSION, None, None, None, None, None, T.now(), T.now())
        self.assertIn("Team guard", self.bash("git checkout main", "rev1", repo))
        self.assertIn("Team guard", self.bash(f"cd {repo} && git stash", "rev1"))
        self.assertIn("Team guard", self.bash(f"git -C {repo} reset --hard", "rev1"))
        self.assertIsNone(self.bash("git checkout -b foo", "rev1", repo))
        self.assertIsNone(self.bash("git stash list", "rev1", repo))
        self.assertIsNone(self.bash("git checkout main", "eng1", repo))
        self.assertIsNone(self.bash("git commit -m 'PROJ-64: fix 2 bugs, first run Sunday, March 14'", "rev1", repo))
        self.assertIn("Team claim conflict", self.reason(self.run_hook("Edit", {"file_path": held, "old_string": "a", "new_string": "b"}, "rev1")))
        self.assertIsNone(self.reason(self.run_hook("Edit", {"file_path": held, "old_string": "a", "new_string": "b"}, "eng1")))
        self.assertIsNone(self.reason(self.run_hook("Edit", {"file_path": held, "old_string": "a", "new_string": "b"})))
        self.assertIsNone(self.bash(f"{CLI} say --as reviewer#rev1 'checking 2 things'", "rev1"))
        self.assertIn("add `--as reviewer#rev1`", self.bash(f"{CLI} say 'hi'", "rev1"))
        self.assertIn("post as yourself", self.bash(f"{CLI} say --as codex#cdx1 'hi'", "rev1"))

    def test_gate_overhead(self):
        con = T.connect()
        me = T.identity(con, {"session_id": SESSION})
        cmds = ["ls -la", "git status && git log --oneline -5", "gh pr view 57 --json body", "grep -rn 'github' . | head", "python3 -m pytest -q"]
        t = time.perf_counter()
        for _ in range(20):
            for c in cmds:
                T.fail_open(con, me, T.bash_facts, c, str(self.dir))
        per = (time.perf_counter() - t) / (20 * len(cmds))
        self.assertLess(per, 0.005)
        self.assertEqual(self.passed(RIGHT, SCHEDULE + [FIRST]).returncode, 0)
        t = time.perf_counter()
        for _ in range(20):
            self.assertIsNone(T.fail_open(con, me, T.mcp_facts, DRAFT, {"channel_id": "C1", "message": RIGHT}))
        self.assertLess((time.perf_counter() - t) / 20, 0.02)
        con.close()
        t = time.perf_counter()
        self.bash("ls -la", "rev1")
        self.assertLess(time.perf_counter() - t, 0.1)


class ScheduleTest(Base):
    def test_schedule_and_first_run_in_nearby_sentences(self):
        for text in ["Heads-up: the backup service on staging prunes old snapshots every Sunday at 03:00 UTC, "
                     "the first run is Sunday, March 14.",
                     "The backup job prunes old snapshots every Sunday at 03:00 UTC. The first run is Sunday, March 14.",
                     "The first run is Sunday, March 14. After that it runs every Sunday at 03:00 UTC.",
                     "Бекап-сервіс чиститиме знімки щонеділі о 03:00 UTC. Перший запуск — неділя, 14 березня.",
                     "Pruning uses cron `0 3 * * 0`. The first run is Sunday, March 14.",
                     "Знімки чистимо щонеділі о 03:00 UTC; перший запуск 14.03."]:
            self.assertIn("fires first on 2027-03-07", " ".join(T.fact_calendar(text, REF)), text)
        far = ("We deploy on March 10. The office is closed on Friday. Bring a laptop. "
               "The pruning runs every Sunday at 03:00 UTC, first run Sunday, March 14.")
        self.assertIn("fires first on 2027-03-07", " ".join(T.fact_calendar(far, REF)))
        self.assertTrue(T.fact_calendar("Demos are on Tuesdays. The office is open. Bring a laptop. The first run is Tuesday, March 16.", REF))

    def test_later_start_accepted(self):
        for text in ["From Monday, March 8 the pruning runs every Sunday at 03:00 UTC, first run Sunday, March 14.",
                     "Starting 2027-03-08, pruning runs every Sunday at 03:00 UTC; first run Sunday, March 14.",
                     "After the upgrade on 08.03 the pruning runs every Sunday at 03:00 UTC, first run Sunday, March 14.",
                     "Після оновлення 8 березня чистка щонеділі о 03:00 UTC, перший запуск 14 березня.",
                     "With the 2.8.0 release on March 8, pruning runs every Sunday; the first run is Sunday, March 14.",
                     "We deploy the pruning policy on Monday, March 8. It runs every Sunday at 03:00 UTC, so the first run is Sunday, March 14."]:
            self.assertEqual(T.fact_calendar(text, REF), [], text)
        self.assertEqual(self.passed("From Monday, March 8 the pruning runs every Sunday at 03:00 UTC, first run Sunday, March 14.",
                                     [claim("Monday"), claim("March 8"), claim("Sunday"), claim("03:00 UTC"), claim("March 14")]).returncode, 0)

    def test_yearless_dm_never_refuses_alone(self):
        for text in ["from 4.10, Thursday rollout", "We moved the workers to 24.04, Friday's build is green.",
                     "Latency зросла з 30.02 до 31.07 мс після апдейту.", "Release on Wed, the 10.03 build.", "Зробимо до 31.04."]:
            self.assertEqual(T.fact_calendar(text, REF), [], text)
        self.assertIn(("date", "10.03"), [(t["kind"], t["text"]) for t in T.fact_tokens("Release on Wed 10.03.")])
        self.assertEqual(T.fact_calendar("Release on Wed, 2027-03-09.", REF), ['"Wed" next to "2027-03-09": 2027-03-09 is a Tuesday'])
        self.assertEqual(T.fact_calendar("Зробимо до 31.04.2027.", REF), ['"31.04.2027" is not a real calendar date'])

    def test_pr_review_comment_flag(self):
        for cmd in ["gh pr review 46 --comment --body 'PROJ-2 deployed Mar 9'", "gh pr review 46 --comment -b 'PROJ-2 deployed Mar 9'",
                    "gh pr review 46 -c --body 'PROJ-2 deployed Mar 9'", "gh pr review 46 --body 'PROJ-2 deployed Mar 9' --comment",
                    "gh pr close 46 --comment 'PROJ-2 deployed Mar 9'", "gh issue reopen 46 -c 'PROJ-2 deployed Mar 9'"]:
            self.assertIn('jira "PROJ-2"', self.bash(cmd), cmd)
        f = self.write("r.md", "PROJ-2 deployed Mar 9")
        self.assertIn('jira "PROJ-2"', self.bash(f"gh pr review 46 --comment --body-file {f}"))

    def test_graphql_mutations(self):
        self.assertIsNone(self.bash("gh api graphql -f query='mutation { resolveReviewThread(input:{threadId:\"PRRT_kwDOabc\"}) { thread { id } } }'"))
        self.assertIsNone(self.bash("gh api graphql -f query='mutation { markPullRequestReadyForReview(input:{pullRequestId:\"PR_x\"}) { clientMutationId } }'"))
        self.assertIsNone(self.bash("gh api graphql -f query='query { repository(owner:\"x\",name:\"mutation\") { name } }'"))
        cmd = "gh api graphql -f query='mutation { addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:\"PRRT_x\", body:\"Fixed in v2.8.0\"}) { comment { id } } }'"
        why = self.bash(cmd)
        self.assertIn('version "v2.8.0"', why)
        saved = why.split("saved at ")[1].split(" ")[0]
        self.assertEqual(Path(saved).read_text(), "PRRT_x\nFixed in v2.8.0")
        c = self.claims("c.json", [claim("v2.8.0", "gh release view v2.8.0 -R example-org/app", "v2.8.0 published 2027-02-15")])
        self.assertEqual(self.cli("pass", saved, "--as", "reviewer#rev1", "--claims", c).returncode, 0)
        self.assertIsNone(self.bash(cmd))
        self.assertIn('number "30 days"', self.bash("gh api graphql -f query='mutation($b: String!) { addComment(input:{subjectId:\"x\", body:$b}) { clientMutationId } }' -f body='keep 30 days'"))
        self.assertIn('number "30 days"', self.bash("gh api repos/o/r/issues/23/comments -f 'body=Use $HOME for 30 days'"))

    def test_help_as_value_is_text(self):
        self.assertIn('number "30 days"', self.bash("gh pr create --title '30 days' --body '--help'"))
        self.assertIn('number "30 days"', self.bash("gh pr comment 3 --body '30 days' -b --help"))
        self.assertIsNone(self.bash("gh pr comment --help"))

    def test_read_only_forms_allowed(self):
        self.assertIsNone(self.bash("T=~/project/.claude/team/team.py; python3 $T factcheck status", "eng1"))
        self.assertIsNone(self.bash("T=~/project/.claude/team/team.py; python3 $T factcheck tokens /tmp/x.txt", "eng1"))
        self.assertIsNone(self.bash(f"sqlite3 -readonly {self.dir}/team.db \"select verdict, count(*) from factchecks group by 1\"", "eng1"))
        self.assertIn("ledger", self.bash(f"sqlite3 {self.dir}/team.db \"update factchecks set verdict='PASS'\"", "eng1"))
        self.assertIn("ledger", self.bash(f"sqlite3 {self.dir}/team.db \"DELETE FROM factchecks\"", "eng1"))
        self.assertIn("shell variable", self.bash("T=~/project/.claude/team/team.py; python3 $T factcheck fail f --as software-engineer#eng1 --note x", "eng1"))

    def test_heredoc_piped_to_shell(self):
        self.assertIn("heredoc", self.bash("cat <<'EOF' | bash\ngh pr comment 3 --body 'PROJ-2 shipped Mar 9'\nEOF"))
        self.assertIn("heredoc", self.bash("cat <<'EOF' | sh -s\ngh pr comment 3 --body 'PROJ-2 shipped Mar 9'\nEOF"))
        self.assertIsNone(self.bash("cat <<'EOF' | grep gh\ngh pr comment 3 --body 'PROJ-2 shipped Mar 9'\nEOF"))


class CalendarTest(Base):
    FLAG = [
        "The backup job prunes old snapshots every Sunday at 03:00 UTC. For each database we keep the 7 most recent daily "
        "snapshots, plus any snapshot taken in the last 30 days. The first run is Sunday, March 14.",
        "Pruning every Sunday at 03:00 UTC. Keeps 7 snapshots. First run Sunday, March 14.",
        "Бекап-сервіс чиститиме знімки щонеділі о 03:00 UTC. Для кожної бази лишаємо 7 найновіших знімків і все, що зроблено "
        "за останні 30 днів. Перший запуск — неділя, 14 березня.",
        "The backup job prunes old snapshots every Sunday at 03:00 UTC and "
        + "it keeps the newest snapshots of every database and anything taken recently, " * 3
        + "the first run is Sunday, March 14.",
        "Нагадую: дедлайн у четвер 12.03.",
        "Release on Wed 9.03.",
        "Pruning runs every Sunday at 03:00 UTC; the first run is scheduled for Sunday, 14.03.",
    ]
    OK = [
        "The pruning runs every Sunday at 03:00 UTC, and we also verify backups daily; the first run is Sunday, March 7.",
        "First audit of the replicas is Tuesday, March 9. Snapshots run daily at 01:30 UTC.",
        "Standup daily at 09:45; next sync is Friday, March 5.",
        "Every Thursday we meet. Next sync with the vendor is on Thursday, March 4.",
        "next pass over the runbook on Friday, March 5; the pruning runs every Sunday.",
        "We moved the workers to 24.04, Friday's build is green.",
        "from 4.10, Thursday rollout",
        "Release on Tue 9.03.",
        "Нагадую: дедлайн у п'ятницю 12.03.",
        "Pruning runs every Sunday at 03:00 UTC; the first run is scheduled for Sunday, 7.03.",
        "The backup job prunes old snapshots every Sunday at 03:00 UTC. For each database we keep the 7 most recent daily "
        "snapshots, plus any snapshot taken in the last 30 days. The first run is Sunday, March 7.",
    ]

    def test_calendar_flags(self):
        for text in self.FLAG:
            self.assertTrue(T.fact_calendar(text, REF), text)

    def test_calendar_passes(self):
        for text in self.OK:
            self.assertEqual(T.fact_calendar(text, REF), [], text)

    def test_any_matching_schedule(self):
        text = "The pruning runs every Sunday at 03:00 UTC, and we also verify backups daily; the first run is Sunday, March 14."
        self.assertIn("fires first on", " ".join(T.fact_calendar(text, REF)))

    def test_graphql_fields(self):
        q = "gh api graphql -f query='mutation($m: String!) { addComment(input:{subjectId:\"x\", body:$m}) { clientMutationId } }'"
        self.assertIn('number "30 days"', self.bash(q + " -f msg='keep 30 days'"))
        self.assertIsNone(self.bash(q + " -f msg='done'"))
        f = self.write("q.graphql", 'mutation { addComment(input:{subjectId:"x", body:"Fixed in v2.8.0"}) { clientMutationId } }')
        self.assertIn('version "v2.8.0"', self.bash(f"gh api graphql -F query=@{f}"))
        self.assertIn("shell substitution", self.bash(f'gh api graphql -f query="$(cat {f})"'))
        self.assertIsNone(self.bash("gh api graphql -f query='mutation { resolveReviewThread(input:{threadId:\"PRRT_x\"}) { thread { id } } }' -F n=3"))

    def test_ttl_is_24h(self):
        self.assertEqual(T.FACT_TTL, 24 * 3600)
        self.assertEqual(self.passed(RIGHT, SCHEDULE + [FIRST]).returncode, 0)
        self.sql("UPDATE factchecks SET ts=?", T.now() - 24 * 3600 + 120)
        self.assertIsNone(self.draft(RIGHT))
        self.sql("UPDATE factchecks SET ts=?", T.now() - 24 * 3600 - 120)
        self.assertTrue(self.draft(RIGHT))


class TokenTest(unittest.TestCase):
    def kinds(self, text):
        return [(t["kind"], t["text"]) for t in T.fact_tokens(text)]

    def test_tokens(self):
        got = self.kinds("Pruned 12.5 GiB (7 snapshots, 40%) in 30 days; 7 найновіших; PROJ-75, #57, v2.8.0, 3.4.1, до 14.03, 14.03.2027, "
                         "14 березня, 2027-03-07 03:00 UTC, у неділю, cron `0 0 3 * * 0`, https://github.com/example-org/x/pull/46, "
                         "UTF-8, T-44, node-03, debian-12.5")
        for want in [("number", "12.5 GiB"), ("number", "7 snapshots"), ("number", "40%"), ("number", "30 days"), ("number", "7 найновіших"),
                     ("jira", "PROJ-75"), ("pr", "#57"), ("version", "v2.8.0"), ("version", "3.4.1"), ("date", "14.03"),
                     ("date", "14.03.2027"), ("date", "14 березня"), ("date", "2027-03-07"), ("time", "03:00 UTC"), ("weekday", "неділю"),
                     ("cron", "0 0 3 * * 0"), ("pr", "https://github.com/example-org/x/pull/46")]:
            self.assertIn(want, got)
        self.assertFalse([t for t in got if t[1] in ("UTF-8", "T-44", "03", "12.5", "8", "44")])
        self.assertEqual(self.kinds("ок, дякую"), [])

    def test_noise(self):
        self.assertEqual(self.kinds("See #57 https://github.com/o/r/pull/57 got 200 with retries"),
                         [("pr", "https://github.com/o/r/pull/57"), ("number", "200")])
        self.assertEqual(self.kinds("Python 3.11 and 31.04"), [("date", "3.11"), ("date", "31.04")])
        self.assertEqual(T.fact_calendar("до 31.04", REF), [])

    def test_calendar_false_refusals(self):
        for text in ["We check the logs daily. The new pruning runs every Sunday at 03:00 UTC, first run Sunday, March 7.",
                     "Standups are on Mondays. The pruning runs every Sunday at 03:00 UTC; first run Sunday, March 7.",
                     "The first run will be on Sunday, March 14.", "Upgraded the worker image to Python 3.13, Sunday rollout.",
                     "We deploy on March 10. Pruning every Sunday at 03:00 UTC, first run Sunday, March 14.",
                     "cron `30 0 3 * * 0`, first run Sunday, March 7"]:
            self.assertEqual(T.fact_calendar(text, REF), [], text)

    def test_calendar_catches(self):
        text = "Runs every Sunday at 03:00 UTC; first run Sunday, March 14; audit on March 14."
        self.assertIn("fires first on 2027-03-07 03:00:00 UTC", " ".join(T.fact_calendar(text, REF)))
        self.assertIn("fires first", " ".join(T.fact_calendar("Схема `0 0 3 * * 0`, перший запуск 14 березня (неділя).", REF)))
        self.assertEqual(T.first_fire(T.cron_parse("30 0 3 * * 0".split()), datetime(2027, 3, 7, 3, 0, 10)), datetime(2027, 3, 7, 3, 0, 30))
        self.assertEqual(T.first_fire(T.cron_parse("0 3 * * 0".split()), datetime(2027, 3, 7, 3, 0, 10)), datetime(2027, 3, 14, 3, 0))

    def test_linear_time(self):
        for text in ["-" * 64000 + "!", ".".join(["a"] * 20000) + "/" + "b" * 20000, ".".join(str(i % 10) for i in range(20000)),
                     "org/repo-name.x#" * 5000, "Sunday, March 14 Tuesday 2027-03-07 " * 1000,
                     "Перший запуск у неділю 14 березня о 03:00 UTC, щонеділі чистимо 7 знімків. " * 600]:
            t = time.perf_counter()
            T.fact_tokens(text)
            T.fact_calendar(text, REF)
            self.assertLess(time.perf_counter() - t, 1.0, text[:30])


if __name__ == "__main__":
    unittest.main()
