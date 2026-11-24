import contextlib
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))
import teamlib as T
import hook
import team
import server as S

SESSION = "sess-security-test"
AGENTS = [("eng1", "software-engineer#eng1", "software-engineer")]
TEMPLATE = (ROOT / "security" / "scope-template.md").read_text()
ORDINARY = "Goal: Fix the label of the board.\nProcedure:\n1. Read team.py.\n2. Run the tests.\nReport: The result and the test output."
SCOPED = "Goal: Run the proof of concept on the local stand.\nSecurity scope: keep/T-7/scope.md. Do not contact hosts outside localhost.\nReport: The result."
OWNER = "Owner of the target: the user, own repo on own laptop."
APPROVED = "Approved by the user: 2026-01-15, «так, запускай»"
IN_SCOPE = "## In scope\n- localhost:8080, the local install of the app\n"


def scope(owner=OWNER, approved=APPROVED, in_scope=IN_SCOPE):
    return f"# Security scope: T-7 test\n\n{owner}\n{approved}\n\n{in_scope}\n## Permitted actions\n- Read-only: reading config\n"


class Base(unittest.TestCase):
    config = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.keep = self.dir / "keep"
        env = {"TEAM_STE": "off", "TEAM_CONFIG": str(self.dir / "config.json")}
        self.patches = [mock.patch.object(T, "DB", self.dir / "team.db"), mock.patch.object(T, "STATE", self.dir),
                        mock.patch.object(T, "FACT_DIR", self.dir / "factcheck"), mock.patch.object(T, "KEEP", self.keep),
                        mock.patch.dict(os.environ, env)]
        for p in self.patches:
            p.start()
        os.environ.pop("TEAM_SECURITY", None)
        if self.config is not None:
            (self.dir / "config.json").write_text(json.dumps(self.config))
        con = T.connect()
        for aid, handle, atype in AGENTS:
            con.execute("INSERT INTO agents(agent_id,session,handle,type,status,started,last_ts,cursor,linked) VALUES(?,?,?,?,?,?,?,?,?)",
                        (aid, SESSION, handle, atype, "running", T.now(), T.now(), 0, "meta"))
        for i in range(7):
            T.task_add(con, f"card {i + 1}", "lead")
        con.close()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def write_scope(self, text, tid="T-7"):
        path = self.keep / tid / "scope.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_hook(self, tool, ti, agent=None, use_id="tu-1"):
        inp = {"hook_event_name": "PreToolUse", "session_id": SESSION, "tool_name": tool, "tool_input": ti, "cwd": str(self.dir),
               "tool_use_id": use_id}
        if agent:
            inp["agent_id"] = agent
        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(inp))), contextlib.redirect_stdout(buf):
            hook.main()
        out = buf.getvalue()
        return json.loads(out) if out else {}

    def agent(self, prompt, desc="T-7: run", atype="general-purpose", use_id="tu-1"):
        return self.run_hook("Agent", {"subagent_type": atype, "name": "x", "description": desc, "prompt": prompt}, use_id=use_id)

    def send(self, message, agent="eng1"):
        return self.run_hook("SendMessage", {"to": "main", "message": message, "summary": "s"}, agent)

    def denied(self, out):
        h = out.get("hookSpecificOutput") or {}
        return h.get("permissionDecisionReason") if h.get("permissionDecision") == "deny" else None

    def note(self, out):
        h = out.get("hookSpecificOutput") or {}
        return "" if h.get("permissionDecision") else h.get("additionalContext") or ""

    def rows(self, sql, args=()):
        con = T.connect()
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def events(self):
        return [(r["task"], json.loads(r["data"])) for r in self.rows("SELECT task, data FROM events WHERE kind='security' ORDER BY id")]

    def reset(self):
        self.rows("DELETE FROM events")
        self.rows("DELETE FROM spawns")

    def spawns(self):
        return len(self.rows("SELECT * FROM spawns")) + len(self.rows("SELECT * FROM events WHERE kind='spawn'"))

    def assert_denied(self, out, *parts, rule=None, task="T-7"):
        reason = self.denied(out)
        self.assertIsNotNone(reason, out)
        head, _, rest = reason.partition("\n")
        self.assertTrue(reason.startswith("Security gate: ") or (T.SCOPE_PART.match(head) and rest.startswith("Security gate: ")), reason)
        for p in parts:
            self.assertIn(p, reason)
        self.assertEqual(self.spawns(), 0)
        [(tid, data)] = self.events()
        self.assertEqual((tid, data["decision"], data["rule"]), (task, "deny", rule))
        return reason


class ScopeLineTest(Base):
    def test_no_card_id(self):
        reason = self.assert_denied(self.agent(SCOPED.replace("T-7", "the card"), desc="run"), "no id of a card on the board",
                                    str(self.keep / "T-<n>" / "scope.md"), str(T.SCOPE_TEMPLATE), rule="no-card", task=None)
        self.assertTrue(reason.endswith("before this security run."), reason)

    def test_absent_file(self):
        self.assert_denied(self.agent(SCOPED), "is absent", str(self.keep / "T-7" / "scope.md"), str(T.SCOPE_TEMPLATE), rule="no-file")

    def test_card_of_the_spawn_from_the_brief(self):
        self.assert_denied(self.agent(SCOPED, desc="run"), str(self.keep / "T-7" / "scope.md"), rule="no-file")
        self.reset()
        self.assert_denied(self.agent(SCOPED.replace("T-7", "T-99"), desc="run"), "no id of a card on the board", "in the description",
                           rule="no-card", task=None)

    def test_one_card_for_the_spawn_and_the_gate(self):
        self.write_scope(scope())
        reason = self.assert_denied(self.agent("Goal: Run the exploit on localhost.\nCard: T-7.\nReport: x", desc="PoC run",
                                               atype="software-engineer"), "T-7", rule="no-part")
        self.assertIn(T.SCOPE_FORMS, reason)
        self.reset()
        out = self.agent("Goal: Run the PoC.\nSecurity scope: keep/T-7/scope.md.", desc="PoC run")
        self.assertIsNone(self.denied(out))
        self.assertEqual(self.rows("SELECT task FROM spawns")[0]["task"], "T-7")

    def test_template_copy(self):
        self.write_scope(TEMPLATE)
        self.assert_denied(self.agent(SCOPED), "Approved by the user:", "template placeholder", rule="not-approved")

    def test_approval_states(self):
        for approved in ("", "Approved by the user:", "Approved by the user:   ", 'Approved by the user: "not yet" - then no test starts',
                         "Approved by the user: Not yet, asked at 10:00", "Approved by the user: <date and the words of the user>"):
            with self.subTest(approved=approved):
                self.write_scope(scope(approved=approved))
                self.rows("DELETE FROM events")
                self.assert_denied(self.agent(SCOPED), "Approved by the user:", rule="not-approved")

    def test_owner_placeholder(self):
        self.write_scope(scope(owner="Owner of the target: <who owns it: the user | employer, local copy only>. Stop and ask ..."))
        self.assert_denied(self.agent(SCOPED), "Owner of the target:", rule="owner")

    def test_in_scope_without_item(self):
        for part in ("## In scope\n- <host, URL, repo - one line each, exact names>\n", "## In scope\n\n", "## In scope\n- \n-  <x>\n", ""):
            with self.subTest(part=part):
                self.write_scope(scope(in_scope=part))
                self.rows("DELETE FROM events")
                self.assert_denied(self.agent(SCOPED), "In scope", rule="in-scope")

    def test_valid_scope_passes(self):
        self.write_scope(scope())
        for desc, atype in (("T-7: run", "general-purpose"), ("T-7: review", "reviewer")):
            out = self.agent(SCOPED, desc=desc, atype=atype)
            self.assertIsNone(self.denied(out))
            self.assertNotIn("security", self.note(out))
        self.assertEqual(self.events(), [])
        self.assertEqual(len(self.rows("SELECT * FROM spawns")), 1)

    def test_markdown_part_and_bold_keys(self):
        self.write_scope(scope(owner="**Owner of the target:** the user", approved="**Approved by the user:** 2026-01-15, «так»"))
        out = self.agent(SCOPED.replace("Security scope:", "- **Security scope:**"))
        self.assertIsNone(self.denied(out))
        self.assertEqual(self.events(), [])

    def test_scope_line_of_another_card(self):
        self.write_scope(scope(), "T-3")
        for desc in ("run", "T-5: run"):
            with self.subTest(desc=desc):
                self.rows("DELETE FROM events")
                self.assert_denied(self.agent("Goal: Run the PoC of T-5.\n\nSecurity scope: keep/T-3/scope.md.", desc=desc),
                                   "T-3", "T-5", str(self.keep / "T-5" / "scope.md"), rule="other-card", task="T-5")
        self.assertEqual(T.scope_value("Owner of the target:\n\nApproved by the user: yes", "Owner of the target"), "")

    def test_card_of_the_spawn_comes_first(self):
        self.write_scope(scope(), "T-3")
        out = self.agent("Goal: Run the PoC.\nInputs: the notes of T-5.\nSecurity scope: keep/T-3/scope.md.", desc="T-3: run")
        self.assertIsNone(self.denied(out))
        self.assertEqual(self.events(), [])
        out = self.agent("T-3: run the PoC.\nSecurity scope: the file of the card.", desc="run", use_id="tu-2")
        self.assertIsNone(self.denied(out))
        self.assertEqual(self.events(), [])

    def test_send_message_with_bad_scope(self):
        self.write_scope(scope(approved="Approved by the user: not yet"))
        out = self.send("Continue on T-7.\nSecurity scope: keep/T-7/scope.md.")
        self.assertIn("Approved by the user:", self.denied(out))
        [(tid, data)] = self.events()
        self.assertEqual((tid, data["tool"], data["decision"]), ("T-7", "SendMessage", "deny"))
        self.assertEqual(self.rows("SELECT * FROM events WHERE kind='dm'"), [])


class FindingsTest(Base):
    def test_approval_needs_a_date(self):
        for approved in ("Approved by the user: TODO", "Approved by the user: TBD", "Approved by the user: ?", "Approved by the user: -",
                         "Approved by the user: n/a", "Approved by the user: yes", "Approved by the user: 15.01", "Approved by the user: 2026-13-45"):
            with self.subTest(approved=approved):
                self.write_scope(scope(approved=approved))
                self.rows("DELETE FROM events")
                reason = self.assert_denied(self.agent(SCOPED), "Approved by the user:", rule="not-approved")
                self.assertRegex(reason, "does not have the shape|absent or empty|not valid")
        for approved in ("Approved by the user: 2026-01-15, «так»", 'Approved by the user: 15.01.2026 "yes"'):
            with self.subTest(approved=approved):
                self.write_scope(scope(approved=approved))
                self.rows("DELETE FROM events")
                self.assertIsNone(self.denied(self.agent(SCOPED)))
                self.assertEqual(self.events(), [])

    def test_owner_must_name_the_user_or_employer_copy(self):
        for owner in ("Owner of the target: TBD", "Owner of the target: ?", "Owner of the target: -", "Owner of the target: n/a",
                      "Owner of the target: Acme prod", "Owner of the target: employer", "Owner of the target: employer, local copy only",
                      "Owner of the target: the users' production cluster", "Owner of the target: the user's employer, live prod cluster",
                      "Owner of the target: the user and Acme prod", "Owner of the target: employer, local copy of prod and the live prod API"):
            with self.subTest(owner=owner):
                self.write_scope(scope(owner=owner))
                self.rows("DELETE FROM events")
                self.assert_denied(self.agent(SCOPED), "Owner of the target:", rule="owner")
        for owner in ("Owner of the target: The user, own laptop", "Owner of the target: employer, local copy",
                      "Owner of the target: employer, local copy (api checkout)", "Owner of the target: the user - own laptop",
                      "Owner of the target: the user; repo x", "Owner of the target: **the user**", "Owner of the target: The User"):
            with self.subTest(owner=owner):
                self.write_scope(scope(owner=owner))
                self.rows("DELETE FROM events")
                self.assertIsNone(self.denied(self.agent(SCOPED)))

    def test_checkbox_items(self):
        for part in ("## In scope\n- [ ] <host>\n", "## In scope\n- [ ]\n", "## In scope\n- [x] <host>\n", "## In scope\n* [X]   \n"):
            with self.subTest(part=part):
                self.write_scope(scope(in_scope=part))
                self.rows("DELETE FROM events")
                self.assert_denied(self.agent(SCOPED), "In scope", rule="in-scope")
        self.write_scope(scope(in_scope="## In scope\n- [x] localhost:8080\n"))
        self.rows("DELETE FROM events")
        self.assertIsNone(self.denied(self.agent(SCOPED)))

    def test_blank_brief_on_a_scope_card(self):
        self.write_scope(scope())
        for prompt in (" ", "", "\n\t"):
            with self.subTest(prompt=prompt):
                self.rows("DELETE FROM events")
                self.assert_denied(self.agent(prompt, desc="T-7: continue"), "Security scope:", rule="no-part")
        out = self.agent(" ", desc="T-7: review", atype="reviewer", use_id="tu-9")
        self.assertIsNone(self.denied(out))
        self.assertIn("security: the card T-7", self.note(out))

    def test_crlf_scope_file(self):
        self.write_scope(scope().replace("\n", "\r\n"))
        self.assertIsNone(self.denied(self.agent(SCOPED)))
        self.assertEqual(self.events(), [])
        self.write_scope(scope(approved="Approved by the user: not yet").replace("\n", "\r\n"))
        self.reset()
        self.assert_denied(self.agent(SCOPED, use_id="tu-2"), "does not have the shape", rule="not-approved")

    def test_more_security_words(self):
        for i, text in enumerate(("Run a security audit against localhost:8080.", "Pentest the app.", "Do a penetration test of the stand.",
                                  "Run a vulnerability scan.", "Plan a pen-test.")):
            with self.subTest(text=text):
                out = self.agent(f"Goal: {text}", desc="run", use_id=f"tu-{i}")
                self.assertIn("security: the text asks for security work", self.note(out))

    def test_unknown_heading_ends_a_quiet_part(self):
        for i, text in enumerate(("Constraints: localhost only.\nSteps:\nRun an exploit.", "Out of scope: prod.\n## Steps\n1. Fuzz the parser.",
                                  "Constraints:\n- No prod.\n**Plan:** Write the PoC.")):
            with self.subTest(text=text):
                out = self.agent(text, desc="run", use_id=f"tu-{i}")
                self.assertIn("security: the text asks for security work", self.note(out))
        out = self.agent("Goal: Fix.\nConstraints:\n- No exploit.\n  Do not fuzz.\n1. No port scan.", desc="run", use_id="tu-9")
        self.assertEqual(self.note(out), "")

    def test_scope_line_in_a_fenced_block(self):
        text = "Goal: Fix the spelling in this documentation example:\n```text\nSecurity scope: ...\n```\nReport: The diff."
        out = self.agent(text, desc="Document the field")
        self.assertIsNone(self.denied(out))
        self.assertEqual(self.events(), [])
        self.write_scope(scope())
        self.reset()
        self.assert_denied(self.agent("Goal: Run the PoC.\n~~~\nSecurity scope: keep/T-7/scope.md\n~~~", use_id="tu-2"), "Security scope:",
                           rule="no-part")

    def test_filled_template_passes(self):
        text = TEMPLATE.replace("<T-n> <one-line task>", "T-7 local repro")
        fill = {"Owner of the target:": "the user, own repo on own laptop", "Approved by the user:": "2026-01-15, «так, запускай»"}
        lines = []
        for line in text.splitlines():
            key = next((k for k in fill if line.startswith(k)), None)
            lines.append(f"{key} {fill[key]}{line[line.index('>') + 1:]}" if key else line)
        filled = T.PLACEHOLDER.sub("localhost:8080", "\n".join(lines))
        self.assertNotIn("<", filled)
        self.write_scope(filled)
        out = self.agent(SCOPED)
        self.assertIsNone(self.denied(out))
        self.assertEqual(self.events(), [])
        self.write_scope(TEMPLATE)
        self.assertIsNotNone(self.denied(self.agent(SCOPED, use_id="tu-2")))


class ReviewTest(Base):
    def time_hook(self, call):
        t0 = time.monotonic()
        out = call()
        return time.monotonic() - t0, out

    def test_long_space_lines_are_fast(self):
        for i, text in enumerate(("Done" + " " * 20000 + "\nReport: ok", "Goal: Fix.\n" + ("cell" + " " * 20000 + "\n") * 6 + "Report: x",
                                  "# a" + " " * 20000 + "b\nConstraints" + " " * 20000 + "x", "Approved by the user" + " " * 20000 + "x",
                                  "Security" + " " * 20000 + "scope:" + " " * 20000)):
            for name, call in (("Agent", lambda: self.agent(text, desc="T-2: fix", use_id=f"tu-{i}")), ("SendMessage", lambda: self.send(text))):
                with self.subTest(case=i, tool=name):
                    secs, out = self.time_hook(call)
                    self.assertLess(secs, 1.0)
                    self.assertIsNone(self.denied(out))
        t0 = time.monotonic()
        for line in ("Done" + " " * 20000, "# a" + " " * 20000 + "b", "x" + " " * 20000 + ":"):
            T.PART_HEAD.match(line)
            T.scope_value(line, "Approved by the user")
            T.SCOPE_PART.search(line)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_big_brief_is_fast(self):
        secs, out = self.time_hook(lambda: self.agent("Goal: Fix.\n" + ("Read the file and fix the label of the board. " * 45000), desc="T-2: fix"))
        self.assertLess(secs, 1.0)
        self.assertIsNone(self.denied(out))

    def test_send_message_card_from_the_scope_line_then_the_sender(self):
        self.write_scope(scope())
        out = self.send("Continue the run with the second probe.\nSecurity scope: keep/T-7/scope.md. Do not contact hosts outside localhost.")
        self.assertIsNone(self.denied(out))
        self.assertEqual(self.events(), [])
        self.rows("UPDATE agents SET task='T-7' WHERE agent_id='eng1'")
        out = self.send("Continue with the second probe.\nSecurity scope: the file of my card.")
        self.assertIsNone(self.denied(out))
        self.rows("UPDATE agents SET task=NULL WHERE agent_id='eng1'")
        reason = self.assert_denied(self.send("Continue.\nSecurity scope: the file of the card."), "no id of a card on the board", "the \"Security scope:\" line",
                                    rule="no-card", task=None)
        self.assertNotIn("description", reason)

    def test_unreadable_scope_file(self):
        path = self.write_scope(scope())
        with mock.patch("builtins.open", side_effect=PermissionError(13, "Permission denied")):
            reason = T.scope_problem(path)
        self.assertEqual(reason[0], "unreadable")
        os.chmod(path, 0)
        try:
            if os.access(path, os.R_OK):
                self.skipTest("the user can read a file with mode 000")
            self.assert_denied(self.agent(SCOPED), "cannot read", rule="unreadable")
        finally:
            os.chmod(path, 0o600)

    def test_approval_must_start_with_the_date(self):
        for v in ("asked 2026-01-15, no answer yet", "pending since 2026-01-15", "no (2026-01-15)", "- not yet, asked 15.01.2026", "ні, 15.01.2026",
                  "refused on 2026-01-15", "2026-01-15 pending", "2026-01-15, ще ні", "2026-01-15: refused", "2026-01-15 no answer", "No.",
                  "~~not yet~~ 2026-01-15 так", "2026-01-15 not-yet", "2026-01-15, ні", "2026-01-15", "15.01.2026",
                  "2026-01-15 the user said: stay local, go"):
            with self.subTest(approved=v):
                self.write_scope(scope(approved="Approved by the user: " + v))
                self.reset()
                self.assert_denied(self.agent(SCOPED), "Approved by the user:", rule="not-approved")
        for v in ("2026-01-15, «так, запускай»", '15.01.2026 "go"', "2026-01-15, “yes”", "2026-01-15 «так, лише стенд»",
                  "2026-01-15, «так, але не на живих системах»"):
            with self.subTest(approved=v):
                self.write_scope(scope(approved="Approved by the user: " + v))
                self.reset()
                self.assertIsNone(self.denied(self.agent(SCOPED)))

    def test_scope_part_forms(self):
        self.write_scope(scope())
        for i, line in enumerate(("1. Security scope: keep/T-7/scope.md", "- Security scope: keep/T-7/scope.md",
                                  "**Security scope:** keep/T-7/scope.md", "> Security scope: keep/T-7/scope.md", "Security scope : keep/T-7/scope.md",
                                  "SECURITY SCOPE: keep/T-7/scope.md", "* **Security scope**: keep/T-7/scope.md")):
            with self.subTest(line=line):
                out = self.agent(f"Goal: Run the PoC.\n{line}\nReport: x", use_id=f"tu-{i}")
                self.assertIsNone(self.denied(out))
        self.assertEqual(self.events(), [])
        self.write_scope(scope(approved="Approved by the user: not yet"))
        for i, line in enumerate(("1. Security scope: keep/T-7/scope.md", "> Security scope: x", "- Security scope:")):
            with self.subTest(line=line):
                self.reset()
                self.assert_denied(self.agent(f"Goal: Run the PoC.\n{line}\nReport: x", use_id=f"tu-x{i}"), rule="not-approved")
        self.reset()
        self.assertIsNone(self.denied(self.agent("Goal: Read the Security scope part of the rules.\n## Security scope of the hook", desc="T-2: x")))

    def test_send_message_words_on_a_scope_card(self):
        self.write_scope(scope())
        for i, (text, agent) in enumerate((("Now run the exploit on T-7 against localhost.", None), ("Run the PoC now.", "eng1"))):
            with self.subTest(text=text):
                if agent:
                    self.rows("UPDATE agents SET task='T-7' WHERE agent_id='eng1'")
                self.reset()
                out = self.send(text, agent=agent)
                self.assertIsNone(self.denied(out))
                self.assertIn("on the card T-7, which has the scope file", self.note(out))
                [(tid, data)] = self.events()
                self.assertEqual((tid, data["rule"]), ("T-7", "words"))

    def test_symlink_scope_file(self):
        good = self.write_scope(scope(), "T-3")
        link = self.keep / "T-7" / "scope.md"
        link.parent.mkdir(parents=True)
        for target in (good, Path("../T-3/scope.md"), Path("/dev/zero")):
            with self.subTest(target=str(target)):
                link.unlink(missing_ok=True)
                os.symlink(target, link)
                self.reset()
                self.assert_denied(self.agent(SCOPED), "symbolic link", rule="symlink")
        link.unlink()
        link.parent.rmdir()
        os.symlink(good.parent, link.parent)
        self.reset()
        self.assert_denied(self.agent(SCOPED), "symbolic link", rule="symlink")
        self.assertEqual(T.scope_label("T-7"), "security")

    def test_codex_note(self):
        self.write_scope(scope())
        for i, prompt in enumerate((ORDINARY, SCOPED)):
            with self.subTest(prompt=prompt[:20]):
                self.reset()
                out = self.agent(prompt, desc="T-7: review", atype="codex", use_id=f"tu-{i}")
                self.assertIsNone(self.denied(out))
                self.assertIn("Do not give its scope file", self.note(out))
                self.assertIn("transcripts of the card to codex", self.note(out))
                [(tid, data)] = self.events()
                self.assertEqual((tid, data["rule"], data["decision"]), ("T-7", "codex", "note"))

    def test_new_security_words(self):
        for i, text in enumerate(("Run a guardrail test of the bot.", "Do a sandbox probe.", "Try to escape the container.", "Scan the stand with nmap.",
                                  "Scan the ports of the stand.", "Try a container escape.")):
            with self.subTest(text=text):
                out = self.agent(f"Goal: {text}", desc="T-2: x", use_id=f"tu-{i}")
                self.assertIn("security: the text asks for security work", self.note(out))

    def test_empty_and_fenced_items(self):
        for part in ("## In scope\n- none\n", "## In scope\n- TBD\n", "## In scope\n- n/a\n", "## In scope\n- None.\n- TODO\n",
                     "## In scope\n```\n- localhost\n```\n", "## In scope\n~~~\n- localhost:8080\n~~~\n- <host>\n"):
            with self.subTest(part=part):
                self.write_scope(scope(in_scope=part))
                self.reset()
                self.assert_denied(self.agent(SCOPED), "In scope", rule="in-scope")
        self.write_scope(scope(in_scope="## In scope\n```\n- localhost\n```\n- localhost:8080\n"))
        self.reset()
        self.assertIsNone(self.denied(self.agent(SCOPED)))

    def test_only_the_lead_writes_the_scope_file(self):
        path = self.write_scope(scope())
        for i, (tool, ti) in enumerate((("Write", {"file_path": str(path), "content": "x"}), ("Edit", {"file_path": str(path), "old_string": "a", "new_string": "b"}),
                                        ("Write", {"file_path": str(self.keep / "T-9" / "scope.md"), "content": "x"}),
                                        ("Write", {"file_path": str(self.keep / "T-7" / "sub" / ".." / "scope.md"), "content": "x"}),
                                        ("MultiEdit", {"file_path": str(path), "edits": []}))):
            with self.subTest(tool=tool, i=i):
                self.reset()
                reason = self.denied(self.run_hook(tool, ti, "eng1"))
                self.assertIn("only the lead writes the scope file", reason or "")
                [(tid, data)] = self.events()
                self.assertEqual(data["rule"], "write")
        self.reset()
        for tool, ti in (("Write", {"file_path": str(self.keep / "T-7" / "notes.md"), "content": "x"}),
                         ("Write", {"file_path": str(self.dir / "scope.md"), "content": "x"})):
            self.assertIsNone(self.denied(self.run_hook(tool, ti, "eng1")))
        self.assertIsNone(self.denied(self.run_hook("Write", {"file_path": str(path), "content": "x"})))
        with mock.patch.dict(os.environ, {"TEAM_SECURITY": "warn"}):
            out = self.run_hook("Write", {"file_path": str(path), "content": "x"}, "eng1")
        self.assertIsNone(self.denied(out))
        self.assertIn("only the lead writes the scope file", self.note(out))
        self.assertEqual(self.events()[-1][1]["decision"], "note")


class R4Test(Base):
    def test_d1_no_truncation(self):
        self.write_scope(scope(approved="Approved by the user: 2026-01-15, «так»" + " " * 400 + "refused"))
        self.assert_denied(self.agent(SCOPED), "Approved by the user:", rule="not-approved")
        self.reset()
        self.assert_denied(self.agent("x\n" * 150000 + "Security scope: keep/T-6/scope.md", desc="T-6: run"), rule="no-file", task="T-6")
        self.reset()
        out = self.agent("Goal: Fix.\n" + "word " * 62000 + "\nRun the exploit.\n", desc="T-2: fix")
        self.assertIn("(exploit)", self.note(out))
        for i, (name, call) in enumerate((("Agent", lambda t: self.agent(t, desc="T-2: fix", use_id=f"tu-a{i}")), ("SendMessage", self.send))):
            for j, line in enumerate(("x" + " " * 100000, "Goal" + " " * 100000 + "x", "# a" + " " * 100000 + "b", "- " + " " * 100000 + "x",
                                      ">" * 100000 + "security scope:", "proof" + " " * 100000 + "of concept", "<" * 100000, "x" + " " * 100000 + ":")):
                with self.subTest(tool=name, case=j):
                    t0 = time.monotonic()
                    call("\n".join([line] * 30))
                    self.assertLess(time.monotonic() - t0, 1.0)

    def test_d2_scope_file_limits(self):
        self.write_scope(scope() + ("- pad\n" * 12000))
        self.assert_denied(self.agent(SCOPED), "larger than 64 KB", rule="too-long")
        self.reset()
        self.write_scope(scope() + "- " + "x" * 1001 + "\n")
        self.assert_denied(self.agent(SCOPED), "longer than 1000", rule="too-long")
        self.reset()
        self.write_scope(scope() + "- " + "x" * 990 + "\n" + "- pad\n" * 9000)
        self.assertIsNone(self.denied(self.agent(SCOPED)))

    def test_d3_colon_is_necessary(self):
        self.write_scope(scope())
        self.assert_denied(self.agent("Goal: Run the PoC.\n## Security scope\nkeep/T-7/scope.md"), rule="no-part")
        self.reset()
        for i, (prompt, desc) in enumerate((("Goal: Add the check.\nInputs: the parts:\n- Goal\n- Security scope\n- Report\nReport: the diff.", "T-2: x"),
                                            ("Goal: Write the README.\n## Security scope\nThe lead writes the file.", "T-2: docs"))):
            out = self.agent(prompt, desc=desc, use_id=f"tu-{i}")
            self.assertIsNone(self.denied(out))
        self.assertIsNone(self.denied(self.send("Check the parts:\n1. Goal\n2. Security scope\n3. Report", agent=None)))
        self.assertEqual(self.events(), [])

    def test_d4_card_only_from_the_scope_line(self):
        self.write_scope(scope())
        out = self.agent("Security scope: the file of this card.\nReport: compare with T-3.", desc="T-7: run")
        self.assertIsNone(self.denied(out))
        self.assertEqual(self.events(), [])
        self.assertIsNone(self.denied(self.send("Continue.\nSecurity scope: keep/T-7/scope.md\nReport: compare with T-3.", agent=None)))

    def test_d5_approval_shape(self):
        for v in ("2026-01-15, no", "2026-01-15, denied", "2026-01-15, «відмовив»", "2026-01-15, «так» and more", "2026-01-15, «Not yet»",
                  "2026-01-15, «no, not now»", "2026-01-15, «pending»", "2026-01-15, «Refused»", "2026-01-15, «ще ні»", "2026-01-15, «Ні»",
                  "2026-01-15, «відмова»", "2026-01-15, «»", "2026-01-15, «  »", "«так» 2026-01-15", "2026-01-15, «так", "2026-01-15, так»",
                  '2026-01-15, "Denied"', "2026-01-15, “no”"):
            with self.subTest(approved=v):
                self.write_scope(scope(approved="Approved by the user: " + v))
                self.reset()
                self.assert_denied(self.agent(SCOPED), "Approved by the user:", rule="not-approved")
        for v in ("2026-01-15, «так, але не на живих системах»", "2026-01-15 «так»", '15.01.2026, "go"', "2026-01-15,“yes, nothing else”",
                  "2026-01-15, «only you run it»"):
            with self.subTest(approved=v):
                self.write_scope(scope(approved="Approved by the user: " + v))
                self.reset()
                self.assertIsNone(self.denied(self.agent(SCOPED)))

    def test_d6_checkbox_at_any_position(self):
        for part in ("## In scope\n-" + " " * 9 + "[x] <host>\n", "## In scope\n- [x] [ ] <a>\n", "## In scope\n- TODO [ ]\n", "## In scope\n-\t[X]\n",
                     "## In scope\n- <a> [x] <b>\n", "## In scope\n-    none [x]\n"):
            with self.subTest(part=part):
                self.write_scope(scope(in_scope=part))
                self.reset()
                self.assert_denied(self.agent(SCOPED), "In scope", rule="in-scope")
        self.write_scope(scope(in_scope="## In scope\n-          [x] localhost:8080\n"))
        self.reset()
        self.assertIsNone(self.denied(self.agent(SCOPED)))

    def test_d7_send_message_card_on_the_board(self):
        self.write_scope(scope(), "T-99")
        self.assert_denied(self.send("Continue.\nSecurity scope: keep/T-99/scope.md.\nRun the PoC.", agent=None), "no id of a card on the board",
                           rule="no-card", task=None)
        self.reset()
        self.write_scope(scope())
        self.assertIsNone(self.denied(self.send("Continue.\nSecurity scope: keep/T-7/scope.md.", agent=None)))

    def test_d8_words_across_lines_and_columns(self):
        for i, text in enumerate(("Goal: Run a prompt\ninjection against localhost.", "Goal: Run a port\n   scan of localhost.",
                                  "Goal: " + "a" * 410 + " Run the exploit.", "Goal: proof\tof\n concept", "Goal: escape the\ncontainer")):
            with self.subTest(text=text[:30]):
                out = self.agent(text, desc="T-2: x", use_id=f"tu-{i}")
                self.assertIn("security: the text asks for security work", self.note(out))
        out = self.agent("Goal: Run a prompt.\n\nInjection of the config is done.", desc="T-2: x", use_id="tu-9")
        self.assertEqual(self.note(out), "")

    def test_d9_write_path_forms(self):
        path = self.write_scope(scope())
        k = str(self.keep)
        outside = self.dir / "outside.md"
        outside.write_text(scope())
        link = self.keep / "T-8" / "scope.md"
        link.parent.mkdir()
        os.symlink(outside, link)
        kl = self.dir / "kl"
        os.symlink(self.keep, kl)
        for i, raw in enumerate((k + "/T-7/SCOPE.md", k + "/t-7/scope.md", k[:-4] + "KEEP/T-7/scope.md", k + "/T-7/Scope.MD", str(link),
                                 k + "//T-7//scope.md/", k + "/../keep/T-7/scope.md", str(kl) + "/T-8/scope.md", str(kl) + "/T-7/scope.md")):
            with self.subTest(path=raw):
                self.reset()
                reason = self.denied(self.run_hook("Write", {"file_path": raw, "content": "x"}, "eng1"))
                self.assertIn("only the lead writes the scope file", reason or "")
        self.assertEqual(path.read_text(), scope())

    def test_d10_bash_writes(self):
        self.write_scope(scope())
        k = str(self.keep)
        for cmd in (f"printf 'x' > {k}/T-7/scope.md", f"cp /tmp/x {k[:-4]}KEEP/t-7/SCOPE.md", f"sed -i '' s/a/b/ {k}/T-7/scope.md",
                    f"echo x | tee {k}/T-7/scope.md", f"python3 -c 'open(\"{k}/T-7/scope.md\", \"w\")'", f"rm {k}/T-7/scope.md",
                    f"ln -sf /tmp/x {k}/T-7/scope.md", f"touch {k}/T-7/scope.md", f"chmod 644 {k}/T-7/scope.md", f"mv /tmp/x {k}/T-9/scope.md",
                    "cd /x && dd if=/tmp/x of=keep/T-7/scope.md", "truncate -s 0 keep/T-7/scope.md", "install -m 600 /tmp/x keep/T-7/scope.md",
                    "perl -pi -e s/a/b/ keep/T-7/scope.md", "ruby -e 1 keep/T-7/scope.md", "node x.js keep/T-7/scope.md", "sed -ni p keep/T-7/scope.md"):
            with self.subTest(cmd=cmd[:60]):
                self.reset()
                out = self.run_hook("Bash", {"command": cmd}, "eng1")
                self.assertIsNone(self.denied(out))
                self.assertIn("Only the lead writes the scope file", self.note(out))
                [(tid, data)] = self.events()
                self.assertEqual((tid, data["rule"], data["decision"]), ("T-9" if "T-9" in cmd else "T-7", "write", "note"))
        self.reset()
        for cmd in (f"cat {k}/T-7/scope.md", f"grep Approved {k}/T-7/scope.md", f"head -5 {k}/T-7/scope.md", f"ls -la {k}/T-7/scope.md",
                    f"wc -l {k}/T-7/scope.md", f"stat {k}/T-7/scope.md", "sed -n 1p keep/T-7/scope.md"):
            with self.subTest(cmd=cmd[:60]):
                self.assertIsNone(self.denied(self.run_hook("Bash", {"command": cmd}, "eng1")))
        self.assertIsNone(self.denied(self.run_hook("Bash", {"command": f"cp /tmp/x {k}/T-7/scope.md"})))
        self.assertEqual(self.events(), [])


class R5Test(Base):
    def test_c1_bash_check_only_notes(self):
        self.write_scope(scope())
        self.write_scope(scope(), "T-3")
        k = str(self.keep)
        r1 = (f"grep -rn 'Approved' {k}/ > /tmp/x.txt", f"grep -rn 'Approved' {k}/T-7/scope.md > /tmp/x.txt", "git diff > x.diff",
              f"git diff -- {k}/T-7/scope.md > x.diff", f"cat {k}/T-7/scope.md", f"cat {k}/T-7/scope.md 2>/dev/null",
              f"cat {k}/T-7/scope.md 2>&1 | head -20", f"ls -la {k}/T-7/ && wc -l {k}/T-7/scope.md", f"diff -u {k}/T-7/scope.md {k}/T-3/scope.md",
              "python3 .claude/team/team.py say --as software-engineer#eng1 --kind report \"The run stayed inside keep/T-7/scope.md\"",
              f"python3 -c \"print(open('{k}/T-7/scope.md').read())\"", f"cp {k}/T-7/scope.md /tmp/scope-copy.md", f"test -f {k}/T-7/scope.md && echo yes",
              f"[ -s {k}/T-7/scope.md ] && echo filled", f"awk '/Approved/' {k}/T-7/scope.md", f"sed -n 1,5p {k}/T-7/scope.md",
              "cat > /tmp/brief.md <<'EOF'\nGoal: Run the PoC.\nSecurity scope: keep/T-7/scope.md\nEOF", "echo 'see keep/T-7/scope.md' >> /tmp/notes.md",
              f"printf x > {k}/T-7/scope.md")
        for cmd in r1:
            with self.subTest(cmd=cmd[:60]):
                self.reset()
                out = self.run_hook("Bash", {"command": cmd}, "eng1")
                self.assertIsNone(self.denied(out))
                self.assertEqual(self.rows("SELECT COUNT(*) AS c FROM events WHERE kind='security' AND json_extract(data,'$.decision')='deny'")[0]["c"], 0)
                self.assertLessEqual(len(self.events()), 1)
        self.reset()
        out = self.run_hook("Bash", {"command": f"cp /tmp/x {k}/T-7/scope.md"}, "eng1")
        self.assertIn("Only the lead writes the scope file", self.note(out))
        [(tid, data)] = self.events()
        self.assertEqual((tid, data["tool"], data["rule"], data["decision"]), ("T-7", "Bash", "write", "note"))

    def test_c2_card_of_an_agent_call_with_a_scope_part(self):
        self.write_scope(scope())
        for i, (desc, prompt) in enumerate((("run", "Goal: " + "Read source. " * 30 + "\nSecurity scope: T-7"),
                                            ("run", "Goal: Read source.\nInputs: T-3 notes\nSecurity scope: T-7"),
                                            ("run", "T-7: Read source.\nInputs: T-3 notes\nSecurity scope: keep/T-7/scope.md"),
                                            ("T-7: run", "Goal: Read T-3 and T-5.\nSecurity scope: the file of the card"))):
            with self.subTest(i=i):
                self.reset()
                out = self.agent(prompt, desc=desc, use_id=f"tu-{i}")
                self.assertIsNone(self.denied(out))
                self.assertEqual(self.events(), [])
                self.assertEqual(self.rows("SELECT task FROM spawns")[0]["task"], "T-7")
        self.reset()
        self.assert_denied(self.agent("T-3: read.\nSecurity scope: T-7", desc="run", use_id="tu-9"), "names the card T-7", "the card T-3",
                           rule="other-card", task="T-3")
        self.reset()
        self.assertIsNone(self.denied(self.agent("Goal: Fix the label.\nThe note of T-5 has the old label.", desc="fix", use_id="tu-10")))
        self.assertEqual(self.rows("SELECT task FROM spawns")[0]["task"], "T-5")

    def test_c3_prefix_sequences(self):
        self.write_scope(scope(approved="Approved by the user: not yet"))
        for i, line in enumerate(("- [ ] Security scope: T-7", "> > Security scope: T-7", "- [x] **Security scope:** T-7", "> - [ ] Security scope: T-7",
                                  "1. > Security scope: T-7", "* [X] Security scope: T-7", ">>Security scope: T-7")):
            with self.subTest(line=line):
                self.reset()
                self.assert_denied(self.agent(f"Goal: Run the PoC.\n{line}", desc="T-7: run", use_id=f"tu-{i}"), rule="not-approved")
        t0 = time.monotonic()
        for line in ("*" * 100000 + "Security scope:", "> " * 50000 + "x", "[ ]" * 30000 + "x", "- " * 50000 + "Security scope", "1." * 50000):
            self.assertIsNone(T.SCOPE_PART.match(line) if not line.endswith(":") else None)
            T.SCOPE_PART.match(line)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_c4_more_refusal_words(self):
        for v in ('"Declined"', '"Refuse this run"', "«rejected»", "«Stop»", "«stopped it»", "«don't run it»", "«Don’t»", "«do not run»",
                  "«заборонено»", "«не цього разу»", "«Не»", "«відмовляюсь»", "«declining»", "«refusal»"):
            with self.subTest(v=v):
                self.write_scope(scope(approved="Approved by the user: 2026-01-15, " + v))
                self.reset()
                self.assert_denied(self.agent(SCOPED), "does not record an approval", rule="not-approved")
        for v in ("«добре, вперед»", "«стенд готовий — так»", "«так, але не живий сервер»", "«done, go»"):
            with self.subTest(v=v):
                self.write_scope(scope(approved="Approved by the user: 2026-01-15, " + v))
                self.reset()
                self.assertIsNone(self.denied(self.agent(SCOPED)))

    def test_c5_items_without_punctuation(self):
        for part in ("## In scope\n- [ ] (TBD)\n", "## In scope\n- (none)\n", "## In scope\n- `n/a`\n", "## In scope\n- N.A.\n", "## In scope\n- [TODO]\n",
                     "## In scope\n- \"TBD\" <host>\n", "## In scope\n- ___\n"):
            with self.subTest(part=part):
                self.write_scope(scope(in_scope=part))
                self.reset()
                self.assert_denied(self.agent(SCOPED), "In scope", rule="in-scope")
        self.write_scope(scope(in_scope="## In scope\n- (localhost:8080)\n"))
        self.reset()
        self.assertIsNone(self.denied(self.agent(SCOPED)))

    def test_c6_symlink_then_dotdot(self):
        self.write_scope(scope())
        sub = self.keep / "T-7" / "subdir"
        sub.mkdir()
        link = self.dir / "link"
        os.symlink(sub, link)
        for raw in (str(link) + "/../scope.md", str(link) + "/../SCOPE.md"):
            with self.subTest(raw=raw):
                self.reset()
                reason = self.denied(self.run_hook("Write", {"file_path": raw, "content": "x"}, "eng1"))
                self.assertIn("only the lead writes the scope file", reason or "")
        self.assertIsNone(self.denied(self.run_hook("Write", {"file_path": str(link) + "/notes.md", "content": "x"}, "eng1")))

    def test_c7_single_quotes_and_period(self):
        for v in ("2026-01-15, 'так'", "2026-01-15, ‘yes’", "2026-01-15, «так».", '2026-01-15, "go".', "2026-01-15 'так'."):
            with self.subTest(v=v):
                self.write_scope(scope(approved="Approved by the user: " + v))
                self.reset()
                self.assertIsNone(self.denied(self.agent(SCOPED)))
        for v in ("2026-01-15, «так»..", "2026-01-15, «так». x", "2026-01-15, 'no'", "2026-01-15, 'так", "2026-01-15, «так»!"):
            with self.subTest(v=v):
                self.write_scope(scope(approved="Approved by the user: " + v))
                self.reset()
                self.assert_denied(self.agent(SCOPED), "Approved by the user:", rule="not-approved")


class ScopeFileTest(Base):
    def test_brief_without_part_is_denied(self):
        self.write_scope(scope())
        for atype in ("general-purpose", "software-engineer", "platform-sre"):
            with self.subTest(atype=atype):
                self.rows("DELETE FROM events")
                self.assert_denied(self.agent(ORDINARY, atype=atype), "T-7", str(self.keep / "T-7" / "scope.md"), "Security scope:", rule="no-part")

    def test_readers_get_a_note(self):
        self.write_scope(scope())
        for i, atype in enumerate(sorted(T.SECURITY_READERS)):
            with self.subTest(atype=atype):
                out = self.agent(ORDINARY, atype=atype, use_id=f"tu-{i}")
                self.assertIsNone(self.denied(out))
                self.assertIn("do not give its scope file" if atype == "codex" else "security: the card T-7 has the scope file", self.note(out).lower()
                              if atype == "codex" else self.note(out))
        self.assertEqual(sorted(d["rule"] for _, d in self.events()), sorted("codex" if t == "codex" else "reader" for t in T.SECURITY_READERS))
        self.assertEqual(len(self.rows("SELECT * FROM spawns")), len(T.SECURITY_READERS))

    def test_send_message_without_part_passes(self):
        self.write_scope(scope())
        out = self.send("T-7: the run finished. The report is in the chat.")
        self.assertIsNone(self.denied(out))
        self.assertEqual(self.events(), [])


class WordsTest(Base):
    def test_security_words_get_a_note(self):
        for i, text in enumerate(("Write a PoC for the bug.", "Fuzz the parser.", "Run a port scan of the stand.",
                                  "Try a prompt injection on the bot.", "Try a sandbox escape.", "Open a reverse shell.",
                                  "Write a proof of concept.", "Test the jailbreak.", "Exploit the hole.")):
            with self.subTest(text=text):
                out = self.agent(f"Goal: {text}\nReport: The result.", use_id=f"tu-{i}")
                self.assertIsNone(self.denied(out))
                self.assertIn("security: the text asks for security work", self.note(out))
                self.assertIn(str(T.SCOPE_TEMPLATE), self.note(out))
        self.assertEqual({(tid, d["rule"], d["decision"]) for tid, d in self.events()}, {("T-7", "words", "note")})

    def test_words_in_quiet_parts_are_ignored(self):
        text = ("Goal: Fix the label.\nOut of scope: Do not run an exploit.\nDo not fuzz.\nInputs: team.py.\n"
                "Constraints:\n- No port scan.\n- No reverse shell.\nReport: The result.")
        out = self.agent(text)
        self.assertEqual(self.note(out), "")
        self.assertEqual(self.events(), [])
        out = self.agent(text + "\nProcedure: Write the PoC.", use_id="tu-2")
        self.assertIn("(poc)", self.note(out))

    def test_ordinary_brief_gets_no_note(self):
        for out in (self.agent(ORDINARY), self.agent(ORDINARY, desc="no card", use_id="tu-2"), self.send(ORDINARY)):
            self.assertIsNone(self.denied(out))
            self.assertEqual(self.note(out), "")
        self.assertEqual(self.events(), [])

    def test_similar_words_are_not_security(self):
        out = self.agent("Goal: Explain the exploration of the pocket data. Report: The point.")
        self.assertEqual(self.note(out), "")

    def test_send_message_note(self):
        out = self.send("Write the PoC for T-7 on the stand.")
        self.assertIn("security: the text asks", self.note(out))
        [(tid, data)] = self.events()
        self.assertEqual((tid, data["tool"]), ("T-7", "SendMessage"))


class WarnModeTest(Base):
    config = {"lang": "uk", "security": "warn"}

    def test_denials_become_notes(self):
        out = self.agent(SCOPED)
        self.assertIsNone(self.denied(out))
        self.assertIn("security (warn mode, not blocked): Security gate: the scope file", self.note(out))
        self.assertEqual(len(self.rows("SELECT * FROM spawns")), 1)
        self.write_scope(scope())
        out = self.agent(ORDINARY, use_id="tu-2")
        self.assertIsNone(self.denied(out))
        self.assertIn("has no part \"Security scope:\"", self.note(out))
        self.assertEqual([(d["rule"], d["decision"], d["mode"]) for _, d in self.events()],
                         [("no-file", "note", "warn"), ("no-part", "note", "warn")])


class ModeTest(Base):
    def test_mode_sources(self):
        cases = [({}, None, "deny"), ({}, '{"lang": "uk"}', "deny"), ({}, '{"security": "warn"}', "warn"), ({}, '{"security": "off"}', "off"),
                 ({"TEAM_SECURITY": "off"}, '{"security": "deny"}', "off"), ({"TEAM_SECURITY": " WARN "}, None, "warn"),
                 ({"TEAM_SECURITY": "deny"}, '{"security": "off"}', "deny"), ({}, '{"security": ["off"]}', "deny"), ({}, "not json", "deny")]
        for env, config, want in cases:
            cfg = self.dir / "config.json"
            if config is None:
                cfg.unlink(missing_ok=True)
            else:
                cfg.write_text(config)
            os.environ.pop("TEAM_SECURITY", None)
            with mock.patch.dict(os.environ, env):
                self.assertEqual(T.security_mode(), want, (env, config))

    def test_off_mode_checks_nothing(self):
        self.write_scope(scope(approved="Approved by the user: not yet"))
        with mock.patch.dict(os.environ, {"TEAM_SECURITY": "off"}), mock.patch.object(T, "scope_problem") as problem, \
                mock.patch.object(T, "security_words") as words, mock.patch.object(T, "security_event") as event:
            for out in (self.agent(SCOPED), self.agent(ORDINARY, use_id="tu-2"), self.agent("Goal: Fuzz it.", desc="x", use_id="tu-3")):
                self.assertIsNone(self.denied(out))
                self.assertNotIn("security", self.note(out))
        problem.assert_not_called()
        words.assert_not_called()
        event.assert_not_called()
        self.assertEqual(self.events(), [])

    def test_fails_open(self):
        with mock.patch.object(T, "scope_problem", side_effect=RuntimeError("broken")):
            out = self.agent(SCOPED)
        self.assertIsNone(self.denied(out))
        self.assertEqual(len(self.rows("SELECT * FROM spawns")), 1)
        self.assertIn("security_check", (self.dir / "security-errors.log").read_text())
        self.assertFalse((self.dir / "factcheck-errors.log").exists())
        [g] = self.rows("SELECT text, data FROM events WHERE kind='guard'")
        self.assertEqual(json.loads(g["data"])["gate"], "security")
        self.assertIn("security_check", g["text"])
        self.assertNotIn("fact", g["text"].lower())

    def test_other_tools_do_not_run_the_gate(self):
        with mock.patch.object(T, "security_check", return_value=None) as gate:
            for tool, ti in (("Bash", {"command": "echo exploit"}), ("Read", {"file_path": str(self.dir / "x")}),
                             ("Grep", {"pattern": "fuzz"}), ("Write", {"file_path": str(self.dir / "y"), "content": "exploit"})):
                self.assertIsNone(self.denied(self.run_hook(tool, ti, "eng1")))
            gate.assert_not_called()
            self.run_hook("SendMessage", {"to": "main", "message": "x"}, "eng1")
            self.agent(ORDINARY, use_id="tu-2")
            self.assertEqual([c.args[2] for c in gate.call_args_list], ["SendMessage", "Agent"])
        self.assertEqual(self.events(), [])


class H3Test(Base):
    def test_a_two_card_ids_in_the_scope_line(self):
        self.write_scope(scope())
        self.write_scope(scope(), "T-3")
        for line in ("Security scope: keep/T-7/scope.md and keep/T-3/scope.md.", "Security scope: T-7, T-3.", "- **Security scope:** T-7 or T-3"):
            with self.subTest(line=line):
                self.reset()
                reason = self.assert_denied(self.agent("Goal: Run the PoC.\n" + line), "T-7", "T-3", "one card id", rule="many-cards")
                self.assertTrue(reason.startswith(line.strip()), reason)
        self.reset()
        out = self.send("Continue the PoC.\nSecurity scope: T-7 and T-3.")
        self.assertIn("one card id", self.denied(out))
        self.reset()
        self.assertIsNone(self.denied(self.agent("Goal: Run the PoC.\nSecurity scope: keep/T-7/scope.md, the T-7 file.")))

    def test_b_more_refusal_words(self):
        for v in ("«nope»", "«Пізніше»", "«потім, після обіду»", "«later»", "«not yet»", "«ще ні»", "«Nope, not today»"):
            with self.subTest(v=v):
                self.write_scope(scope(approved="Approved by the user: 2026-01-15, " + v))
                self.reset()
                self.assert_denied(self.agent(SCOPED), "does not record an approval", rule="not-approved")
        for v in ("«так, звіт потім»", "«вперед, пізніше гляну»"):
            with self.subTest(v=v):
                self.write_scope(scope(approved="Approved by the user: 2026-01-15, " + v))
                self.reset()
                self.assertIsNone(self.denied(self.agent(SCOPED)))

    def test_c_placeholder_values_are_not_filled_items(self):
        for part in ("## In scope\n- host: TBD\n", "## In scope\n- TODO\n", "## In scope\n- <host>\n", "## In scope\n- ?\n",
                     "## In scope\n- host: <name>\n", "## In scope\n- target: ?\n- repo: todo\n", "## In scope\n- URL: ...\n"):
            with self.subTest(part=part):
                self.write_scope(scope(in_scope=part))
                self.reset()
                self.assert_denied(self.agent(SCOPED), "In scope", rule="in-scope")
        for part in ("## In scope\n- host: localhost:8080\n", "## In scope\n- host: TBD\n- repo: ~/src/app, branch main\n",
                     "## In scope\n- localhost:8080, the local install of the app\n"):
            with self.subTest(part=part):
                self.write_scope(scope(in_scope=part))
                self.reset()
                self.assertIsNone(self.denied(self.agent(SCOPED)))

    def test_f1_more_placeholder_forms(self):
        for item in ("- **Host:** TBD", "- host — TBD", "- host – TBD", "- host - TBD", "- host = TBD", "- host:TBD", "- host: TBD (later)",
                     "- host: to be decided", "- host: To be defined.", "- `host`: unknown", "- __host__: n/a"):
            with self.subTest(item=item):
                self.write_scope(scope(in_scope="## In scope\n" + item + "\n"))
                self.reset()
                self.assert_denied(self.agent(SCOPED), "In scope", rule="in-scope")
        for part in ("## In scope\n- https://localhost:8080\n", "## In scope\n- localhost: the app on port 8080\n", "## In scope\n- 127.0.0.1:8080\n",
                     "## In scope\n- Target: none other than localhost:8080\n", "## In scope\n- Local stand:\n  localhost:8080, the app\n",
                     "## In scope\n- **Host:** localhost\n", "## In scope\n- host - the local stand\n"):
            with self.subTest(part=part):
                self.write_scope(scope(in_scope=part))
                self.reset()
                self.assertIsNone(self.denied(self.agent(SCOPED)))
        self.write_scope(scope(in_scope="## In scope\n- Local stand:\n- host: TBD\n"))
        self.reset()
        self.assert_denied(self.agent(SCOPED), "In scope", rule="in-scope")

    def test_r3_names_and_values_after_a_placeholder(self):
        for part in ("- todo-app on localhost", "- tbd-service", "- NA-1 kind cluster", "- unknown-hosts list in ~/src/app",
                     "- Hosts: none, only local files in ~/src/app", "- docker: n/a; kind cluster demo", "- Local stand: N/A, only repo ~/src/app"):
            with self.subTest(part=part):
                self.write_scope(scope(in_scope="## In scope\n" + part + "\n"))
                self.reset()
                self.assertIsNone(self.denied(self.agent(SCOPED)))
        for part in ("- host: TBD", "- host: TBD (later)", "- host: TBD, repo: TBD", "- host: none; repo: n/a", "- host: TBD - later"):
            with self.subTest(part=part):
                self.write_scope(scope(in_scope="## In scope\n" + part + "\n"))
                self.reset()
                self.assert_denied(self.agent(SCOPED), "In scope", rule="in-scope")

    def test_f2_event_text_starts_with_the_gate_reason(self):
        out = self.agent("Goal: Run the PoC.\nSecurity scope: keep/T-7/scope.md.\nReport: x")
        self.assertTrue(self.denied(out).startswith("Security scope: keep/T-7/scope.md."))
        [text] = [r["text"] for r in self.rows("SELECT text FROM events WHERE kind='security'")]
        self.assertTrue(text.startswith("Security gate: the scope file"), text)

    def test_g3_card_ids_from_all_scope_lines(self):
        self.write_scope(scope())
        self.write_scope(scope(), "T-3")
        self.assert_denied(self.agent("Goal: Run the PoC.\nSecurity scope: keep/T-7/scope.md.\nNotes.\nSecurity scope: keep/T-3/scope.md."),
                           "T-3", "T-7", "one card id", rule="many-cards")
        self.reset()
        self.assertIsNone(self.denied(self.agent("Goal: Run the PoC.\nSecurity scope: keep/T-7/scope.md.\n- Security scope: T-7 only.")))

    def test_g4_refusal_after_punctuation_and_more_words(self):
        for v in ("«— ні»", "«(no)»", "«...ні»", "« – nope»", "«згодом»", "«поки ні»", "«зачекай»", "«wait»", "«Hold on»", "«nah»", "«Wait, not now»"):
            with self.subTest(v=v):
                self.write_scope(scope(approved="Approved by the user: 2026-01-15, " + v))
                self.reset()
                self.assert_denied(self.agent(SCOPED), "does not record an approval", rule="not-approved")
        for v in ("«— так, запускай»", "«(yes) go»", "«waiting is over, go»"):
            with self.subTest(v=v):
                self.write_scope(scope(approved="Approved by the user: 2026-01-15, " + v))
                self.reset()
                self.assertIsNone(self.denied(self.agent(SCOPED)))

    def test_d_deny_starts_with_the_scope_line(self):
        line = "  - **Security scope:** keep/T-7/scope.md. Do not contact hosts outside localhost."
        reason = self.assert_denied(self.agent("Goal: Run the PoC.\n" + line + "\nReport: x"), "is absent", rule="no-file")
        self.assertEqual(reason.split("\n")[0], line.strip())
        self.reset()
        reason = self.assert_denied(self.agent("Goal: Run the PoC.\nSecurity scope: the card file.", desc="run"), rule="no-card", task=None)
        self.assertEqual(reason.split("\n")[0], "Security scope: the card file.")
        self.reset()
        self.write_scope(scope())
        reason = self.assert_denied(self.agent("Goal: Run the exploit.\nReport: x", atype="software-engineer"), rule="no-part")
        self.assertTrue(reason.startswith("Security gate: "), reason)

    def test_e_dashboard_chat_shows_security_events(self):
        html = (ROOT / "dashboard" / "index.html").read_text()
        kinds = html[html.index("const WARN_KINDS"):].split("\n", 1)[0]
        self.assertIn('"security"', kinds)
        self.assertEqual(html.count('"warn.security":'), 2)


class BoardTest(Base):
    def test_board_and_dashboard_mark(self):
        con = T.connect()
        other = T.task_add(con, "ordinary card", "lead")
        con.close()
        self.write_scope(scope())
        buf = io.StringIO()
        con = T.connect()
        try:
            with contextlib.redirect_stdout(buf):
                team.cmd_board(con, types.SimpleNamespace(all=True))
        finally:
            con.close()
        lines = {line.split()[0]: line for line in buf.getvalue().splitlines() if line.startswith("  ")}
        self.assertIn("[security]", lines["T-7"] if "T-7" in lines else "")
        self.assertNotIn("[security]", lines[other])
        tasks = {t["id"]: t for t in S.snapshot()["tasks"]}
        self.assertEqual((tasks["T-7"]["security"], tasks[other]["security"]), ("security", None))
        self.assertEqual(S.task_detail("T-7")["task"]["security"], "security")
        self.assertIsNone(S.task_detail(other)["task"]["security"])


if __name__ == "__main__":
    unittest.main()
