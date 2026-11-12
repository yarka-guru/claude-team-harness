import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import teamlib as T
import hook

SESSION = "sess-lang-test"


def board(lang, db):
    env = dict(os.environ, TEAM_DB=str(db))
    env.pop("TEAM_LANG", None)
    if lang:
        env["TEAM_LANG"] = lang
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "team.py"), *a], capture_output=True, text=True, env=env, check=True).stdout
    return run


class BoardLangTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "team.db"

    def tearDown(self):
        self.tmp.cleanup()

    def fill(self, run):
        run("task", "add", "Write the parser")
        run("task", "add", "Wait for approval", "--status", "waiting_user")
        run("task", "add", "Ship it", "--status", "review")

    def test_english_default(self):
        run = board(None, self.db)
        self.fill(run)
        out = run("board")
        for heading in ("== Waiting for you (1)", "== To do (1)", "== In review (1)"):
            self.assertIn(heading, out)
        self.assertNotRegex(out, "[А-Яа-яіїєґ]")

    def test_ukrainian(self):
        run = board("uk", self.db)
        self.fill(run)
        out = run("board")
        for heading in ("== Чекає на тебе (1)", "== До роботи (1)", "== На прийомці (1)"):
            self.assertIn(heading, out)

    def test_unknown_lang_falls_back_to_english(self):
        run = board("de", self.db)
        self.fill(run)
        self.assertIn("== To do (1)", run("board"))


class AskHeadingTest(unittest.TestCase):
    NEGATIONS = ["зараз нічого", "нічого термінового", "нічого нового", "нічого не потрібно від тебе", "від тебе нічого не треба", "Від тебе нічого.",
                 "Поки що нічого", "не треба нічого, все змерджено", "— нічого", "Nothing right now", "Nothing at the moment", "nothing else",
                 "None right now.", "No action required", "Nothing from you this time.", "—"]
    ITEMS = ["Confirm nothing is missing", "Перевір, що нічого не зламалось", "Nothing (but run `! make migrate-prod` when you are back)",
             "No action needed; just confirm the 20 MB limit", "Нічого термінового, але глянь PR #47", "nothing else?"]

    def test_both_headings(self):
        for text in ["Done.\n\n**What I need from you**\n- approve the plan", "Готово.\n\n**Що від тебе**\n- апрув плану",
                     "What I need from you: run `! make deploy`", "Що від тебе: запусти `! make deploy`",
                     "## What I need from you\n1. merge PR #47", "**Your step:** merge the PR", "Від тебе: апрув PR #47",
                     "Звіт.\n\n**На тобі**\n- відповісти на питання про ліміт", "**Що від тебе**\n\nапрув PR #47"]:
            self.assertTrue(hook.user_asks(text), text)

    def test_english_heading_forms(self):
        for text in ["**What I need from you** — approve PR #47", "**What I need from you** – approve PR #47", "What I need from you - approve PR #47",
                     "**What I need from you (2):**\n- approve\n- answer the question", "### What I need from you now\n- approve the plan",
                     "3. **What I need from you**\n- approve the plan", "**What I need from you.**\n- approve the plan",
                     "What I need from you:\n- approve", "**Your steps**\n- merge the PR", "Your step (today): merge the PR"]:
            self.assertTrue(hook.user_asks(text), text)

    def test_english_sentences_are_not_headings(self):
        for text in ["What I need from you is a quick look when you have time.", "Your step is already done.", "Your step 2 failed because the token expired.",
                     "Your steps are listed above.", "What I needed from you yesterday is done.", "What I need from your team: a review.",
                     "Your step: none.", "I wrote down what I need from you below in the PR."]:
            self.assertIsNone(hook.user_asks(text), text)

    def test_negation_is_a_prefix_rule(self):
        for head in ("**What I need from you**", "**Що від тебе**"):
            for x in self.NEGATIONS:
                self.assertIsNone(hook.user_asks(f"{head}\n- {x}"), (head, x))
            for x in self.ITEMS:
                self.assertTrue(hook.user_asks(f"{head}\n- {x}"), (head, x))
        for text in ["What I need from you: nothing", "**What I need from you:** —", "**Що від тебе:** нічого не треба, все готово",
                     "**Що від тебе (нічого термінового)**\n- деталі нижче", "**Що від тебе**\n- наразі нічого\n- поки нічого"]:
            self.assertIsNone(hook.user_asks(text), text)
        self.assertTrue(hook.user_asks("**Що від тебе**\n- наразі нічого\n- глянь звіт за тиждень"))

    def test_negated_answer_stops_the_block(self):
        for text in ["**What I need from you:** nothing.\n\nNext I will run the tests and report.",
                     "**Що від тебе:** нічого.\n\nДалі запущу тести і відзвітую.",
                     "**What I need from you**\nNothing.\n\nDecisions:\n- kept the old API — fewer changes",
                     "**Що від тебе**\nНічого.\n\nРішення:\n- лишив старий API — менше змін",
                     "**What I need from you**\n- Nothing.\nI will ping you when CI is green.",
                     "## What I need from you\n\nNothing right now.\n\nThe release goes out after lunch."]:
            self.assertIsNone(hook.user_asks(text), text)
        got = hook.user_asks("**What I need from you**\n- approve the plan\n\nI will start the migration after that.")
        self.assertIn("approve the plan", got)
        self.assertNotIn("migration", got)

    def test_subheadings_do_not_end_the_block(self):
        for text, want in [("**What I need from you**\n**Now**\n- approve PR #47\n**Later**\n- run the migration", "run the migration"),
                           ("**Що від тебе**\n**Зараз**\n- апрув PR #47\n**Пізніше**\n- запусти міграцію", "запусти міграцію")]:
            got = hook.user_asks(text)
            self.assertIn("PR #47", got)
            self.assertIn(want, got)
        got = hook.user_asks("**What I need from you**\n- approve PR #47\n**Risks**\n- the migration is slow")
        self.assertNotIn("migration", got)

    def test_ukrainian_heading_forms(self):
        for text in ["Від тебе я отримав апрув учора, тому змерджив.", "Від тебе залежить лише вибір назви.", "На тобі було два PR, обидва змерджені.",
                     "На тобі нічого не висить, усе змерджено.\n\nЗвіт: 3 картки закрито."]:
            self.assertIsNone(hook.user_asks(text), text)
        for text in ["**Від тебе**\n- апрув PR #47", "На тобі: відповісти про ліміт", "### На тобі\n- вибрати назву", "Твій крок: апрув",
                     "Що лишилось тобі: змерджити PR"]:
            self.assertTrue(hook.user_asks(text), text)

    def test_fences_quotes_and_indented_code(self):
        for text in ["Example of a report:\n```\n**What I need from you**\n- approve\n```\nAll done.", "```\n**Що від тебе**\n- апрув\n```",
                     "````\n```\n**What I need from you**\n- approve\n```\n````\nDone.", "The hook looks for:\n> **What I need from you**\n> - approve\n\nDone.",
                     "> **Що від тебе**\n> - апрув", "Example:\n\n    **What I need from you**\n    - approve\n\nDone."]:
            self.assertIsNone(hook.user_asks(text), text)
        got = hook.user_asks("Report.\n\n**What I need from you**\n```\n! make -C ~/work/app migrate-prod\n```\n\n**Decisions**\n- x — y")
        self.assertIn("! make -C ~/work/app migrate-prod", got)
        self.assertNotIn("**Decisions**", got)
        self.assertTrue(hook.user_asks("**Що від тебе**\n```\n! make deploy\n```"))
        self.assertTrue(hook.user_asks("**What I need from you**\n~~~\n! make deploy\n~~~"))

    def test_section_ends_block(self):
        got = hook.user_asks("**What I need from you**\n- merge the PR\n\n**Decisions**\n- kept the old API — fewer changes")
        self.assertIn("merge the PR", got)
        self.assertNotIn("kept the old API", got)
        got = hook.user_asks("**Що від тебе**\n- апрув\n\n**Рішення**\n- лишив старий API — менше змін")
        self.assertIn("апрув", got)
        self.assertNotIn("старий API", got)


class StopHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(T, "DB", d / "team.db"), mock.patch.object(T, "STATE", d)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def stop(self, text):
        inp = {"hook_event_name": "Stop", "session_id": SESSION, "last_assistant_message": text}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(inp))), contextlib.redirect_stdout(io.StringIO()):
            hook.main()
        con = T.connect()
        try:
            return [r["text"] for r in con.execute("SELECT text FROM events WHERE kind='needs_user' ORDER BY id")]
        finally:
            con.close()

    def test_needs_user_from_english_and_ukrainian(self):
        self.stop("Report.\n\n**What I need from you**\n- approve PROJ-34")
        self.stop("Звіт.\n\n**Що від тебе**\n- апрув PROJ-35")
        got = self.stop("Report.\n\n**What I need from you**\n- nothing")
        self.assertEqual(len(got), 2)
        self.assertIn("approve PROJ-34", got[0])
        self.assertIn("апрув PROJ-35", got[1])


class MsgTest(unittest.TestCase):
    def test_messages(self):
        with mock.patch.object(T, "LANG", "en"):
            self.assertEqual(T.msg("note.resumed"), "resumed work")
            self.assertEqual(T.msg("session.lead", source="startup"), "lead session: startup")
        with mock.patch.object(T, "LANG", "uk"):
            self.assertEqual(T.msg("note.resumed"), "продовжує роботу")
        self.assertEqual(T.RESUME_NOTES, {"resumed work", "продовжує роботу"})
        for key, (en, uk) in T.TEXT.items():
            self.assertEqual(sorted(re.findall(r"\{(\w+)\}", en)), sorted(re.findall(r"\{(\w+)\}", uk)), key)


if __name__ == "__main__":
    unittest.main()
