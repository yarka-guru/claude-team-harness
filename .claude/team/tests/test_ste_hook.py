import contextlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import teamlib as T
import hook

SESSION = "sess-ste-test"
LINT = Path(__file__).resolve().parent / "ste_fixture"
AGENTS = [("eng1", "software-engineer#eng1", "software-engineer"), ("rev1", "reviewer#rev1", "reviewer")]
GOOD = "Read the log of the job. Find the cause of the failure. Write the cause and the fix in your report."
BAD = "Don't run the job; don't stop it; don't wait for the log; it's done and you can't change it."
UKRAINIAN = "Перевір лог задачі і напиши звіт сьогодні ввечері, будь ласка, коротко."
EXITS = "import sys\nsys.exit(2)\n"
EXITS_IN_CHECK = "import sys\n\n\ndef check(text, kind='brief', data_dir=None):\n    sys.exit(3)\n\n\ndef gate(result, mode):\n    return 'allow', ''\n"
RAISES = "def check(text, kind='brief', data_dir=None):\n    raise RuntimeError('linter broke')\n\n\ndef gate(result, mode):\n    return 'allow', ''\n"


class Base(unittest.TestCase):
    mode = "warn"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        env = {"TEAM_STE": self.mode, "TEAM_STE_DIR": str(LINT), "TEAM_CONFIG": str(self.dir / "config.json")}
        self.patches = [mock.patch.object(T, "DB", self.dir / "team.db"), mock.patch.object(T, "STATE", self.dir),
                        mock.patch.object(T, "FACT_DIR", self.dir / "factcheck"), mock.patch.dict(os.environ, env)]
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

    def agent(self, prompt, use_id="tu-1"):
        return self.run_hook("Agent", {"subagent_type": "reviewer", "name": "rev-x", "description": "T-1: review", "prompt": prompt},
                             use_id=use_id)

    def send(self, message, agent="eng1"):
        return self.run_hook("SendMessage", {"to": "main", "message": message, "summary": "s"}, agent)

    def post(self, verb, text, agent="eng1", extra=()):
        handle = dict((a, h) for a, h, _ in AGENTS)[agent]
        cmd = " ".join(["python3", ".claude/team/team.py", verb, "--as", handle, *extra, json.dumps(text)])
        return self.run_hook("Bash", {"command": cmd}, agent)

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

    def fake(self, body, data=True):
        where = Path(tempfile.mkdtemp(dir=self.dir))
        (where / "ste_lint.py").write_text(body)
        if data:
            (where / "avoid.tsv").write_text("word\tpos\tuse\tnote\n")
            (where / "terms.tsv").write_text("term\tkind\tcategory\tnote\n")
        return where

    def ste_events(self):
        return [json.loads(r["data"]) for r in self.rows("SELECT data FROM events WHERE kind='ste' ORDER BY id")]


class OffTest(Base):
    mode = "off"

    def test_no_output_and_no_import(self):
        with mock.patch.object(T, "ste_linter", side_effect=AssertionError("imported")) as linter:
            for out in (self.agent(BAD), self.send(BAD), self.post("ask", BAD)):
                self.assertNotIn("ste:", self.note(out))
                self.assertIsNone(self.denied(out))
            linter.assert_not_called()
        self.assertEqual(self.ste_events(), [])

    def test_mode_sources(self):
        cases = [({"TEAM_STE": "warn"}, None, "warn"), ({"TEAM_STE": " DENY "}, None, "deny"), ({"TEAM_STE": "loud"}, '{"ste": "deny"}', "off"),
                 ({}, '{"ste": "warn"}', "warn"), ({"TEAM_STE": "off"}, '{"ste": "deny"}', "off"), ({}, '{"ste": ["warn"]}', "off"),
                 ({}, "not json", "off"), ({}, None, "off"), ({}, '{"lang": "uk"}', "off")]
        for env, config, want in cases:
            cfg = self.dir / "config.json"
            if config is None:
                cfg.unlink(missing_ok=True)
            else:
                cfg.write_text(config)
            os.environ.pop("TEAM_STE", None)
            with mock.patch.dict(os.environ, env):
                self.assertEqual(T.ste_mode(), want, (env, config))


class WarnTest(Base):
    def test_agent_brief_note_and_call_continues(self):
        out = self.agent(BAD)
        self.assertIsNone(self.denied(out))
        self.assertIn("ste: ", self.note(out))
        self.assertIn("H4 [", self.note(out))
        self.assertEqual(len(self.rows("SELECT * FROM spawns WHERE tool_use_id='tu-1'")), 1)
        [e] = self.ste_events()
        self.assertEqual({k: e[k] for k in ("tool", "kind", "decision")}, {"tool": "Agent", "kind": "brief", "decision": "warn"})
        self.assertEqual(set(e), {"tool", "kind", "words", "hard", "soft", "decision", "rules"})
        self.assertEqual(e["hard"], sum(e["rules"].values()))
        self.assertEqual(e["rules"], {"H4": 5, "H5": 3})
        self.assertNotIn("don't", json.dumps(e))

    def test_sendmessage_note_and_dm_recorded(self):
        out = self.send(BAD)
        self.assertIsNone(self.denied(out))
        self.assertIn("ste: ", self.note(out))
        self.assertEqual(len(self.rows("SELECT * FROM events WHERE kind='dm'")), 1)

    def test_chat_note(self):
        for verb, extra in (("ask", ()), ("report", ("--to", "lead")), ("say", ("--kind", "handoff")), ("say", ("--kind=answer",))):
            out = self.post(verb, BAD, extra=extra)
            self.assertIsNone(self.denied(out), verb)
            self.assertIn("ste: ", self.note(out), (verb, extra))
        self.assertEqual([e["kind"] for e in self.ste_events()], ["chat"] * 4)

    def test_plain_say_not_examined(self):
        for extra in ((), ("--kind", "say")):
            self.assertNotIn("ste: ", self.note(self.post("say", BAD, extra=extra)))
        self.assertEqual(self.ste_events(), [])

    def test_good_brief_and_ukrainian(self):
        for out in (self.agent(GOOD), self.send(GOOD), self.post("handoff", GOOD)):
            self.assertNotIn("ste: ", self.note(out))
        self.assertEqual(self.ste_events(), [])
        out = self.agent(UKRAINIAN)
        self.assertIn("H7", self.note(out))
        self.assertIsNone(self.denied(out))
        self.assertEqual(self.ste_events()[0]["rules"], {"H7": 1})

    def test_note_shows_six_findings(self):
        text = " ".join(["Don't stop; it's fine."] * 6)
        note = self.note(self.agent(text))
        rows = [line for line in note.splitlines() if line.startswith("  ") and " [" in line]
        self.assertEqual(len(rows), 6)
        self.assertIn("  … and 12 more", note)
        self.assertIn("Test a draft:", note)
        self.assertTrue(note.splitlines()[0].startswith("ste: "))

    def test_object_message_not_examined(self):
        with mock.patch.object(T, "ste_linter", side_effect=AssertionError("examined")) as linter:
            out = self.send({"type": "shutdown_response", "request_id": "r1", "approve": True})
            linter.assert_not_called()
        self.assertNotIn("ste: ", self.note(out))

    def test_speed(self):
        text = " ".join(["Open the file and read the log. The job has failed; you must fix it. Ensure the `x` runs (see `y`)."] * 50)
        t = time.perf_counter()
        T.ste_check(T.connect(), {"handle": "lead", "session": SESSION, "agent_id": None, "type": "team-lead"}, "Agent", "brief", text)
        self.assertLess(time.perf_counter() - t, 0.05)


class DenyTest(Base):
    mode = "deny"

    def test_agent_denied_without_waiting_spawn(self):
        out = self.agent(BAD, use_id="tu-deny")
        self.assertIn("ste: ", self.denied(out))
        self.assertEqual(self.rows("SELECT * FROM spawns WHERE tool_use_id='tu-deny'"), [])
        self.assertEqual(self.rows("SELECT * FROM events WHERE kind='spawn'"), [])
        self.assertEqual(self.ste_events()[0]["decision"], "deny")
        out = self.agent(GOOD, use_id="tu-ok")
        self.assertIsNone(self.denied(out))
        self.assertEqual(len(self.rows("SELECT * FROM spawns WHERE tool_use_id='tu-ok'")), 1)

    def test_sendmessage_denied_without_dm(self):
        self.assertIn("ste: ", self.denied(self.send(BAD)))
        self.assertEqual(self.rows("SELECT * FROM events WHERE kind='dm'"), [])

    def test_chat_never_denied(self):
        out = self.post("decision", BAD)
        self.assertIsNone(self.denied(out))
        self.assertIn("ste: ", self.note(out))
        self.assertEqual(self.ste_events()[0]["decision"], "warn")

    def test_ukrainian_brief_denied(self):
        self.assertIn("H7", self.denied(self.agent(UKRAINIAN)))

    def test_below_threshold_is_warn(self):
        out = self.agent(GOOD + " Then stop; wait.")
        self.assertIsNone(self.denied(out))
        self.assertIn("H5", self.note(out))


class FailOpenTest(Base):
    mode = "deny"

    def test_linter_raises(self):
        fake = self.fake(RAISES)
        with mock.patch.dict(os.environ, {"TEAM_STE_DIR": str(fake)}):
            for out in (self.agent(BAD), self.send(BAD), self.post("ask", BAD), self.post("report", GOOD)):
                self.assertIsNone(self.denied(out))
                self.assertNotIn("ste: ", self.note(out))
        log = (self.dir / "ste-errors.log").read_text()
        self.assertEqual(log.count("RuntimeError: linter broke"), 1)
        self.assertFalse((self.dir / "factcheck-errors.log").exists())
        guards = self.rows("SELECT text, data FROM events WHERE kind='guard'")
        self.assertEqual(len(guards), 1)
        self.assertEqual(json.loads(guards[0]["data"])["gate"], "ste")
        self.assertEqual(len(self.rows("SELECT * FROM spawns")), 1)

    def test_bad_linter_results(self):
        bodies = ['def check(text, kind="brief", data_dir=None):\n    return {"words": 50, "hard": ["H1"], "soft": []}\n\n\ndef gate(result, mode):\n    return "warn", "ste: x"\n',
                  'def check(text, kind="brief", data_dir=None):\n    return {"words": 50, "hard": [{"rule": "H1"}], "soft": []}\n\n\ndef gate(result, mode):\n    return "warn", 123\n',
                  'def check(text, kind="brief", data_dir=None):\n    return None\n\n\ndef gate(result, mode):\n    return None\n']
        for body in bodies:
            fake = self.fake(body)
            with mock.patch.dict(os.environ, {"TEAM_STE_DIR": str(fake)}):
                for out in (self.agent(BAD), self.send(BAD), self.post("ask", BAD), self.post("report", BAD)):
                    self.assertIsNone(self.denied(out))
        self.assertFalse((self.dir / "factcheck-errors.log").exists())
        self.assertTrue((self.dir / "ste-errors.log").exists())
        self.assertLessEqual(len(self.rows("SELECT * FROM events WHERE kind='guard'")), 1)

    def test_system_exit_does_not_stop_the_call(self):
        for body in (EXITS, EXITS_IN_CHECK):
            fake = self.fake(body)
            with mock.patch.dict(os.environ, {"TEAM_STE_DIR": str(fake)}):
                for out in (self.agent(BAD), self.send(BAD), self.post("ask", BAD)):
                    self.assertIsNone(self.denied(out))
                    self.assertNotIn("ste: ", self.note(out))
        self.assertIn("SystemExit: 3", (self.dir / "ste-errors.log").read_text())

    def test_system_exit_in_a_real_hook_process(self):
        fake = self.fake(EXITS)
        inp = {"hook_event_name": "PreToolUse", "session_id": SESSION, "tool_name": "SendMessage", "tool_use_id": "tu-x", "cwd": str(self.dir),
               "agent_id": "eng1", "tool_input": {"to": "main", "message": BAD, "summary": "s"}}
        env = dict(os.environ, TEAM_DB=str(self.dir / "team.db"), TEAM_STE="deny", TEAM_STE_DIR=str(fake), PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, str(ROOT / "hook.py")], input=json.dumps(inp), capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_no_linter_in_dir(self):
        empty = self.dir / "empty"
        empty.mkdir()
        for where in (empty, self.dir / "missing", self.fake("def check(:\n")):
            with mock.patch.dict(os.environ, {"TEAM_STE_DIR": str(where)}):
                for out in (self.agent(BAD), self.send(BAD), self.post("ask", BAD)):
                    self.assertIsNone(self.denied(out), where)
                    self.assertNotIn("ste: ", self.note(out))
        self.assertFalse((self.dir / "factcheck-errors.log").exists())
        log = (self.dir / "ste-errors.log").read_text()
        self.assertEqual(log.count("the STE check is off"), 3)
        self.assertEqual(self.rows("SELECT * FROM events WHERE kind='guard'"), [])

    def test_identity_guard_still_first(self):
        cmd = 'python3 .claude/team/team.py ask --as someone-else "' + BAD + '"'
        self.assertIn("post as yourself", self.denied(self.run_hook("Bash", {"command": cmd}, "eng1")))


class CodexRoundTest(Base):
    mode = "deny"

    def test_c1_off_identity_check_is_lazy(self):
        cmd = "python3 team.py ask hello; echo " + "x" * 2000000
        for mode in ("off", "warn"):
            with mock.patch.dict(os.environ, {"TEAM_STE": mode}):
                t = time.perf_counter()
                self.assertIn("--as", self.denied(self.run_hook("Bash", {"command": cmd}, "eng1")))
                self.assertLess(time.perf_counter() - t, 0.5, mode)

    def test_c2_slow_linter_has_a_limit(self):
        handler = signal.getsignal(signal.SIGALRM)
        fake = self.fake("import time\n\n\ndef check(text, kind='brief', data_dir=None):\n    time.sleep(5)\n\n\ndef gate(result, mode):\n    return 'deny', 'x'\n")
        with mock.patch.dict(os.environ, {"TEAM_STE_DIR": str(fake)}):
            t = time.perf_counter()
            out = self.send(BAD)
            took = time.perf_counter() - t
        self.assertIsNone(self.denied(out))
        self.assertLess(took, T.STE_LIMIT + 0.5)
        self.assertIn("TimeoutError", (self.dir / "ste-errors.log").read_text())
        self.assertIs(signal.getsignal(signal.SIGALRM), handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_c3_linter_stdout_does_not_reach_hook_output(self):
        body = ("print('import debug')\n\n\ndef check(text, kind='brief', data_dir=None):\n    print('check debug')\n"
                "    return {'words': 9, 'hard': [{'rule': 'H5'}], 'soft': []}\n\n\ndef gate(result, mode):\n    print('gate debug')\n"
                "    return 'warn', 'ste: 9 words\\n  1:1: H5 [8.1] x'\n")
        with mock.patch.dict(os.environ, {"TEAM_STE_DIR": str(self.fake(body))}):
            for out in (self.agent(BAD), self.send(BAD), self.post("ask", BAD)):
                self.assertIn("ste: 9 words", self.note(out))

    def test_c4_missing_data_turns_the_check_off(self):
        nodata = self.fake((LINT / "ste_lint.py").read_text(), data=False)
        for out in (self.agent(BAD), self.send(BAD)):
            self.assertIsNotNone(self.denied(out))
        with mock.patch.dict(os.environ, {"TEAM_STE_DIR": str(nodata)}):
            for out in (self.agent(BAD, use_id="tu-2"), self.send(BAD)):
                self.assertIsNone(self.denied(out))
        self.assertIn("avoid.tsv, terms.tsv missing in", (self.dir / "ste-errors.log").read_text())

    def test_c5_error_field_is_logged(self):
        body = "def check(text, kind='brief', data_dir=None):\n    return {'words': 0, 'hard': [], 'soft': [], 'error': 'OSError: data broke'}\n\n\ndef gate(result, mode):\n    return 'allow', ''\n"
        with mock.patch.dict(os.environ, {"TEAM_STE_DIR": str(self.fake(body))}):
            self.assertIsNone(self.denied(self.send(BAD)))
        self.assertIn("linter error: OSError: data broke", (self.dir / "ste-errors.log").read_text())


class OrderTest(Base):
    def test_bash_gates_run_before_ste(self):
        with mock.patch.object(T, "bash_facts", return_value="fact gate said no"), \
                mock.patch.object(T, "ste_check", side_effect=AssertionError("ste before gates")) as ste:
            self.assertEqual(self.denied(self.post("report", BAD)), "fact gate said no")
            ste.assert_not_called()
        with mock.patch.object(hook, "on_bash", return_value="git guard said no"), \
                mock.patch.object(T, "ste_check", side_effect=AssertionError("ste before gates")) as ste:
            self.assertEqual(self.denied(self.post("report", BAD)), "git guard said no")
            ste.assert_not_called()

    def test_command_parsed_once(self):
        real = hook.quoted_segments
        with mock.patch.object(hook, "quoted_segments", side_effect=real) as seg:
            self.post("report", BAD)
        self.assertEqual(seg.call_count, 1)

    def test_lead_command_not_parsed_when_off(self):
        real = hook.quoted_segments
        cmd = 'python3 .claude/team/team.py report --as lead "' + BAD + '"'
        for mode, calls in (("off", 0), ("warn", 1)):
            with mock.patch.dict(os.environ, {"TEAM_STE": mode}), mock.patch.object(hook, "quoted_segments", side_effect=real) as seg:
                self.run_hook("Bash", {"command": cmd})
            self.assertEqual(seg.call_count, calls, mode)
        with mock.patch.object(hook, "quoted_segments", side_effect=real) as seg:
            self.run_hook("Bash", {"command": cmd.replace(BAD, "x " * 100001)})
        self.assertEqual(seg.call_count, 0)

    def test_agent_event_gets_card_from_description(self):
        con = T.connect()
        con.execute("INSERT INTO tasks(id,seq,title,status) VALUES('T-7',7,'x','doing')")
        con.close()
        self.run_hook("Agent", {"subagent_type": "reviewer", "description": "T-7: review", "prompt": BAD})
        self.assertEqual([r["task"] for r in self.rows("SELECT task FROM events WHERE kind='ste'")], ["T-7"])


class ChatFormsHookTest(Base):
    def test_hook_reads_the_three_forms(self):
        for cmd in ('/usr/bin/python3 ~/x/team.py report "' + BAD + '"', 'env LANG=C python3 ~/x/team.py report "' + BAD + '"'):
            self.assertIn("--as software-engineer#eng1", self.denied(self.run_hook("Bash", {"command": cmd}, "eng1")), cmd)
        for cmd in ('/usr/bin/python3 ~/x/team.py report --as software-engineer#eng1 "' + BAD + '"',
                    'env TEAM_AS=software-engineer#eng1 python3 ~/x/team.py report "' + BAD + '"'):
            out = self.run_hook("Bash", {"command": cmd}, "eng1")
            self.assertIsNone(self.denied(out), cmd)
            self.assertIn("ste:", self.note(out), cmd)


class PatternSpeedTest(unittest.TestCase):
    def test_new_patterns_are_linear(self):
        code = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
import hook, teamlib as T
fills = ["'" * 2000, "’" * 2000, " " * 2000, "-" * 2000, "not " * 500, "none, " * 400, ":" * 2000, "*" * 2000, "`" * 2000, '"' * 2000,
         "\\\\" * 2000, "(" * 2000, "PASS/" * 400, "no " * 700, "x:" * 1000, "a - " * 500, "TBD; " * 400, "\\\\\\n" * 1000]
calls = [lambda x: hook.parse_verdict("Verdict: pass not " + x), lambda x: hook.parse_verdict("Verdict: PASS " + x), hook.parse_verdict,
         lambda x: hook.user_asks("**Що від тебе**\\nПізніше:\\n- x\\n" + x), lambda x: list(hook.chat_posts("python3 team.py ask --as a " + x)),
         hook.done_said, lambda x: T.scope_items("## In scope\\n- host: " + x + "\\n"), lambda x: T.scope_approval("2026-01-15, «" + x + "»"),
         T.scope_lines_of]
worst = 0.0
for f in calls:
    for x in fills:
        t = time.perf_counter()
        f(x)
        worst = max(worst, time.perf_counter() - t)
print(json.dumps(worst))
"""
        out = subprocess.run([sys.executable, "-c", code, str(ROOT)], capture_output=True, text=True, timeout=60, check=True).stdout
        self.assertLess(json.loads(out), 0.1)


class SlowLinterTest(Base):
    def test_one_timeout_skips_the_other_texts_of_the_call(self):
        fake = self.fake("import time\n\n\ndef check(text, kind='brief', data_dir=None):\n    time.sleep(5)\n\n\ndef gate(result, mode):\n    return 'deny', 'x'\n")
        cmd = " && ".join(f'python3 ~/x/team.py {verb} --as software-engineer#eng1 "{BAD} {i}"' for i, verb in enumerate(("ask", "report", "handoff")))
        with mock.patch.dict(os.environ, {"TEAM_STE_DIR": str(fake)}):
            for use_id in ("tu-1", "tu-2"):
                t = time.perf_counter()
                out = self.run_hook("Bash", {"command": cmd}, "eng1", use_id=use_id)
                took = time.perf_counter() - t
                self.assertIsNone(self.denied(out))
                self.assertGreater(took, T.STE_LIMIT - 0.3, use_id)
                self.assertLess(took, T.STE_LIMIT + 0.5, use_id)
        self.assertIn("TimeoutError", (self.dir / "ste-errors.log").read_text())
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))


class ChatTextTest(unittest.TestCase):
    def test_tokens(self):
        cases = [('python3 team.py ask --as a --to b --task T-1 "one; two"', ["one; two"]),
                 ("python3 team.py say --as a --kind report hello world", ["hello world"]),
                 ("python3 team.py say --as=a --kind=blocker --to=b stuck", ["stuck"]),
                 ("python3 team.py say --as a hi", [None]), ("python3 team.py say --as a --kind say hi", [None]),
                 ('python3 team.py ask --as a "x" && python3 team.py report --as a "y"', ["x", "y"]),
                 ("python3 team.py board", []), ("echo team.py ask x", []),
                 ('python3 team.py ask --as a "x y" 2> /dev/null', ["x y"]), ('python3 team.py ask --as a "x" >/tmp/o 2>/dev/null', ["x"]),
                 ('python3 team.py ask --as a "<file> is missing"', ["<file> is missing"])]
        for cmd, want in cases:
            self.assertEqual([hook.chat_text(r) for r in hook.chat_posts(cmd)], want, cmd)

    def test_quoted_redirect_interpreter_path_and_env_prefix(self):
        cases = [('python3 team.py ask --as a ">"', [">"]), ('python3 team.py ask --as a "2>&1"', ["2>&1"]),
                 ("python3 team.py ask --as a '>/tmp/out' is the file", [">/tmp/out is the file"]),
                 ('python3 team.py ask --as a "x" 2>/dev/null >/tmp/o', ["x"]), ('python3 team.py ask --as a x\\>y', ["x>y"]),
                 ('/usr/bin/python3 team.py ask --as a "x"', ["x"]), ('/opt/tools/bin/python3.13 ~/t/team.py report --as a "y"', ["y"]),
                 ('env TEAM_AS=a python3 team.py ask "x"', ["x"]), ('TEAM_AS=a LANG=C python3 team.py ask "x"', ["x"]),
                 ('env -u X /usr/bin/python3 team.py report --as a "y"', ["y"]), ('python3 team.py ask --as a "open quote', []),
                 ('python3 team.py report \\\n  --as a "x"', ["x"]), ('python3 team.py report --as a \\\n "x" \\\n 2>/dev/null', ["x"]),
                 ('python3 team.py ask --as a "x" 2>"/dev/null"', ["x"]), ("python3 team.py ask --as a \"x\" >'/tmp/o'", ["x"]),
                 ('python3 team.py ask --as a "x" 2> "/dev/null"', ["x"]), ('python3 team.py ask --as a "x" ">" y', ["x > y"])]
        for cmd, want in cases:
            self.assertEqual([hook.chat_text(r) for r in hook.chat_posts(cmd)], want, cmd)
        self.assertIsNone(hook.chat_identity('env TEAM_AS=eng python3 team.py ask "x"', "eng"))
        self.assertIsNone(hook.chat_identity('TEAM_AS=eng /usr/bin/python3 team.py ask "x"', "eng"))
        self.assertIn("--as eng", hook.chat_identity('env TEAM_AS=other python3 team.py ask "x"', "eng"))
        self.assertIn("--as eng", hook.chat_identity('/usr/bin/python3 team.py ask "x"', "eng"))
        self.assertIsNone(hook.chat_identity('env TEAM_AS=other python3 team.py ask --as eng "x"', "eng"))


class CliTest(unittest.TestCase):
    def test_team_ste(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, TEAM_DB=str(Path(d) / "team.db"), TEAM_STE_DIR=str(LINT))
            run = lambda *a, stdin=None, e=env: subprocess.run([sys.executable, str(ROOT / "team.py"), "ste", *a], input=stdin,
                                                               capture_output=True, text=True, env=e, timeout=20)
            p = run("-", stdin=GOOD)
            self.assertEqual((p.returncode, p.stdout.strip()), (0, "ste: 21 words, 3 sentences, 0 hard, 0 soft"))
            draft = Path(d) / "draft.md"
            draft.write_text(BAD)
            p = run(str(draft))
            self.assertEqual(p.returncode, 1)
            self.assertIn(f"{draft}:1:", p.stdout)
            self.assertEqual(run("--kind", "chat", "-", stdin=UKRAINIAN).returncode, 0)
            self.assertEqual(run("-", stdin=UKRAINIAN).returncode, 1)
            self.assertEqual(run("-", stdin=GOOD, e=dict(env, TEAM_STE_DIR=d)).returncode, 2)
            self.assertEqual(run().returncode, 2)


if __name__ == "__main__":
    unittest.main()
