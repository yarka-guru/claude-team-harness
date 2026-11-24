import contextlib
import io
import json
import os
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

SESSION = "sess-verdict-asks"
REPORT_STOPPED = ("Verdict: NOT COMPLETED. The Codex quota ended in the middle of the run, so this report holds no PASS or FAIL.\n\n"
                  "How it ran (folder ~/work/app, input from /dev/null, a 900-second alarm; the prompt asked for a tree audit and a bug search, "
                  "answer as \"findings by rank, each with file:line and a reason\"):\n"
                  "perl -e 'alarm 900; exec @ARGV' codex exec --sandbox read-only --skip-git-repo-check \"<audit of the tree + bug search in "
                  "the upload handler, the image resizer, the queue worker, the retry policy, the settings loader, the CLI flags "
                  "and the page templates; read only; findings by rank>\" </dev/null > "
                  "<scratchpad>/codex.txt 2>&1\n\n"
                  "Codex output: the whole 2,180-line file was read.")
REPORT_SECOND = ("**Second look: the README passes. The changelog still fails, and the build script check is not done.**\n\n"
                 "**Gate PASS for 24 hours, saved for this text (6 facts, a primary source for each):**\n"
                 "- README only: hash `0a1b2c3d4e5f6071`, saved as `<scratchpad>/gate/readme.txt`\n"
                 "- Heading plus README: hash `8192a3b4c5d6e7f8`, saved as `<scratchpad>/gate/readme-full.txt`")
REPORT_TICKETS = ("Two notes compared with primary sources. Results and hashes:\n\n## 1. Upload notes — PASS, hash 1f2e3d4c5b6a7980\n"
                  "Facts match origin/main and a local build: v1.4.2/v2.0.1, chart 3.1.0, status 413 on large files.")
REPORT_GATES = ("Both gate records are in.\n\n1. Config file notes — PASS, hash 2a3b4c5d6e7f8091.\nOnly the opening line is new, as "
                "the diff shows.\n\n2. Schema notes — PASS, hash 9a8b7c6d5e4f3021.")
REPORT_REVIEW = ("Verdict sent to the lead; the upload size fix is reviewed.\n\n**Verdict: PASS** (parser PASS, unit tests PASS, "
                 "build PASS; no outward writes)\n\nBranch `upload-limit`: `a1b2c3d` (part B) + `d4e5f6a` (part A).\n\n**Nothing blocks it.**")
REPORT_TWO_HEADS = ("Codex: done (look two, sandbox read-only, no stdin, 900 s limit).\nservice-a head 6a7b8c9: PASS. The diff only moves one dependency "
                    "(requirements.txt:12 plus lock). Nothing else moves.\nservice-b head 1f2e3d4: PASS. Code")
REPORT_WAITING = ("Verdict: NOT RUN - the fix from eng-2 is not in yet; 5c6b7a8 had a FAIL, and eng-2 has the list.\n\n"
                  "Parts: parser FAIL (F2), README FAIL (F1, F3), lint and build PASS.\n\n**F1, README: the flag name is wrong.**")
OTHER = "sess-verdict-asks-other"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patches = [mock.patch.object(T, "DB", self.dir / "team.db"), mock.patch.object(T, "STATE", self.dir)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run_hook(self, inp):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(inp))), contextlib.redirect_stdout(out):
            hook.main()
        raw = out.getvalue()
        return json.loads(raw) if raw else None

    def events(self, kind):
        con = T.connect()
        try:
            return [dict(r) for r in con.execute("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,))]
        finally:
            con.close()

    def add_agent(self, aid, atype, status="running"):
        con = T.connect()
        con.execute("INSERT INTO agents(agent_id,session,handle,type,status,started,last_ts,cursor,linked) VALUES(?,?,?,?,?,?,?,?,?)",
                    (aid, SESSION, f"{atype}#{aid}", atype, status, T.now(), T.now(), 0, "meta"))
        con.close()


class ParseVerdictTest(unittest.TestCase):
    def check(self, cases):
        for text, want in cases:
            self.assertEqual(hook.parse_verdict(text), want, text)

    def test_verdict_line_wins(self):
        self.check([("Verdict: FAIL\nParts: parser PASS (0a1b2c3), changelog FAIL.", "FAIL"),
                    ("**Verdict:** PASS\n\nNote: the README FAIL of the earlier look is gone.", "PASS"),
                    ("The config file verdict is FAIL. Both schema files PASS.", "FAIL"),
                    ("T-7, build script: Verdict: PASS. The gate pass is saved.", "PASS"),
                    ("## Verdict\n\n**PASS**\n\nThe unit tests had a FAIL before; it is closed.", "FAIL"),
                    ("The parser verdict from before still holds.\nPASS for the added lines.", "PASS")])

    def test_not_run_and_not_completed_are_none(self):
        self.check([("Verdict: NOT RUN - the Codex quota is used up.", "none"),
                    ("Verdict: NOT COMPLETED. The Codex job stopped halfway.", "none"), (REPORT_STOPPED, "none"), (REPORT_WAITING, "none"),
                    ("**Verdict: NOT RUN**\nParts: build script PASS (previous commit).", "none"),
                    ("NOT RUN: Codex hung, so I stopped the process.", "none"),
                    ("Verdict: PASS for the parser.\nVerdict: NOT RUN for the end-to-end check.", "none")])

    def test_pass_and_fail_lines_record_fail(self):
        self.check([("Verdict: PASS for the build script and its docs.\nVerdict: FAIL for the changelog text.", "FAIL"),
                    ("Verdict: FAIL (service-a 0a1b2c3); PASS (service-b 4d5e6f7). Codex: done.", "FAIL"),
                    ("Verdict: PASS (service-b 4d5e6f7); FAIL (service-a 0a1b2c3).", "FAIL"),
                    ("## T-5 second look: (A) docs PASS · (B) code FAIL (small)\n### A. Docs: PASS", "FAIL"),
                    ("**Verdict:** parser at 7e8f9a0 holds up (PASS); the README does not (FAIL), one wrong flag name.", "FAIL"),
                    ("Verdict: NOT RUN - Codex was still busy.\nVerdict: FAIL", "FAIL"),
                    ("service-a head 6a7b8c9: PASS.\nservice-b head 1f2e3d4: FAIL.", "FAIL")])

    def test_mixed_verdict_line(self):
        self.check([("Verdict: PASS for the parser. FAIL for the README.", "FAIL"),
                    ("Verdict: PASS for unit tests\nVerdict: FAIL and PASS for config file and schema, in that order.", "FAIL"),
                    ("Verdict: PASS on the unit suite; NOT RUN on the slow suite.", "none"),
                    ("Verdict: PASS on lint; NOT COMPLETED on the build script.", "none"),
                    ("Verdict: NOT RUN - the next commit is not pushed; the last one had a FAIL.", "none"),
                    ("Verdict: PASS. The parser FAIL from before is gone.", "FAIL"),
                    ("Verdict: PASS or FAIL will follow the build.", None), ("Verdict: PASS/FAIL is still open; NOT RUN.", "FAIL"),
                    ("Verdict: PASS, the README has no PASS/FAIL stub now.", "FAIL")])

    def test_template_phrase_with_two_verdict_lines(self):
        self.check([("Verdict: PASS for lint\nVerdict: FAIL/PASS for parser and README, in that order.", "FAIL"),
                    ("Verdict: PASS for lint\nVerdict: PASS or FAIL for the changelog, still open.", "FAIL"),
                    ("Verdict: PASS/FAIL", None), ("Verdict: PASS or FAIL will follow the build.", None)])

    def test_full_verdict_line_is_classified(self):
        self.check([("Verdict: PASS " + "x" * 587 + " FAIL", "FAIL"), ("Verdict: PASS " + "x" * 3000 + " NOT RUN for e2e", "none"),
                    ("Verdict: PASS " + "x" * 4100 + " FAIL", "PASS"), ("x" * 700 + "\nVerdict: FAIL", None)])

    def test_verdict_value_on_the_next_lines(self):
        self.check([("Verdict:\n- parser: PASS\n- README: FAIL", "FAIL"), ("## Verdict\n\nParser PASS.\nChangelog FAIL.", "FAIL"),
                    ("**Verdict:**\n\n- parser: PASS\n- schema: PASS\n\n## Notes\nThe FAIL seen before is closed.", "PASS"),
                    ("## Verdict\nBuild script PASS.\n## Findings\n- an older FAIL, now closed", "PASS"),
                    ("this report has no verdict.\n\nSo far: lint PASS", None),
                    ("No final verdict on the parser yet.\nLint PASS.", None)])

    def test_verdict_words_without_case_on_a_verdict_line(self):
        self.check([("Verdict: Pass", "PASS"), ("Verdict: fail", "FAIL"), ("Verdict: FAILED", "FAIL"), ("Verdict: Not run", "none"),
                    ("Verdict: Not completed, Codex quit.", "none"), ("Verdict: passed", "PASS"),
                    ("**Verdict: the README change passes, the changelog fails twice on wording.**", "FAIL"),
                    ("Verdict: PASS. Every test passed; nothing failed.", "PASS"), ("Lint passed and nothing failed.", None)])

    def test_quoted_prompt_deep_in_a_line_is_not_a_verdict_line(self):
        cmd = ("cd /tmp/work && perl -e 'alarm 900; exec @ARGV' codex exec --sandbox read-only " + "x" * 200 +
               " Begin with 'Verdict: PASS' or 'Verdict: FAIL' and list issues after it.\" </dev/null > out.txt")
        self.check([("Verdict: PASS\n\nThe command:\n" + cmd, "PASS"), ("Verdict: NOT RUN - no Codex quota left.\n" + cmd, "none")])

    def test_open_verdict_reads_later_paragraphs(self):
        self.check([("**Verdict:**\n\nPASS for the parser\n\nFAIL for the README", "FAIL"), ("## Verdict\nPASS for the parser\n### Changelog\nFAIL", "FAIL"),
                    ("## Verdict\nPASS for lint\n## Findings\n- one older FAIL, closed", "PASS"),
                    ("Verdict:\n\nPASS for the parser\n\n" + "x" * 600 + "\nFAIL for the README", "PASS")])

    def test_final_verdict_without_value_is_none(self):
        self.check([("The lead gets a final verdict.\n\nSo far: lint PASS.", None),
                    ("The final verdict:\nPASS", "PASS")])

    def test_template_phrase_with_any_second_verdict_line(self):
        self.check([("Verdict: PASS for lint\nSecond verdict: FAIL/PASS for parser and schema", "FAIL"),
                    ("Next verdict: PASS or FAIL after the build.", None)])

    def test_lowercase_fail_and_not_run_count_next_to_pass(self):
        self.check([("Verdict: PASS for the parser; fail for the README.", "FAIL"), ("Verdict: PASS on lint; not run on the slow suite.", "none"),
                    ("Verdict: PASS, the changelog check fails at one line.", "FAIL"), ("Verdict: PASS; the build script is failing on main.", "FAIL"),
                    ("Verdict: PASS; schema check not completed.", "none"), ("Verdict: PASS. Every test passed; nothing failed.", "PASS")])

    def test_first_line_is_read_loosely_without_a_verdict_line(self):
        self.check([(REPORT_SECOND, "FAIL"), (REPORT_TICKETS, "PASS"), (REPORT_GATES, "PASS"), (REPORT_REVIEW, "PASS"), (REPORT_TWO_HEADS, "PASS"),
                    ("**Second look: the README passes. The changelog still fails ...**\n\n**Gate PASS (24h) ...**", "FAIL"),
                    ("Third look: every check passed.\n\nDetails below.", None), ("2d3e4f5 cannot pass in this state.\n\nTwo issues are open.", "FAIL"),
                    ("Third look: the build failed.\n\nDetails below.", "FAIL"), ("T-7: Codex check NOT completed, nothing to list.", "none"),
                    ("Notes on the third look.\n\nAn older run failed twice.", None)])

    def test_failed_counts_unless_negated(self):
        self.check([("Verdict: PASS on lint; the parser tests failed.", "FAIL"), ("Verdict: PASS. Every test passed; nothing failed.", "PASS"),
                    ("Verdict: PASS; no step failed.", "FAIL"), ("Verdict: PASS, 0 failed, 40 passed.", "PASS"),
                    ("Verdict: PASS; never failed in 5 builds.", "PASS"), ("Verdict: PASS, but the schema step failed once.", "FAIL")])

    def test_not_passed_is_fail(self):
        self.check([("Verdict: NOT PASSED. Two blockers are open.", "FAIL"), ("Verdict: not pass, two open issues.", "FAIL"),
                    ("Verdict: the parser did not pass the check.", "FAIL"), ("Verdict: the README does not pass now.", "FAIL"),
                    ("NOT PASSED: two blockers are open.", "FAIL")])

    def test_not_yet_passed_and_marked_not_passed(self):
        self.check([("Verdict: not yet passed today.", "FAIL"), ("Verdict: NOT **PASSED** here.", "FAIL"), ("Verdict: NOT *PASSED* so far", "FAIL"),
                    ("Verdict: not `passed`", "FAIL"), ("**NOT PASSED**: two open issues.", "FAIL")])

    def test_negation_before_pass_on_a_verdict_line(self):
        self.check([("Verdict: not a PASS so far.", "FAIL"), ("Verdict: no PASS for the README.", "FAIL"), ("Verdict: the parser doesn't pass.", "FAIL"),
                    ("Verdict: the schema didn't pass the check.", "FAIL"), ("Verdict: the build cannot PASS today.", "FAIL"),
                    ("Verdict: not ready to PASS today.", "FAIL"), ("Verdict: NOT-PASSED", "FAIL"), ("Verdict: the config won’t pass lint.", "FAIL"),
                    ("Verdict: the README isn't a pass.", "FAIL"), ("Verdict: PASS. No blocker; every test passes.", "PASS"),
                    ("Verdict: PASS, no new issues, lint passes.", "PASS"), ("Verdict: PASS. Nothing blocks the parser and lint passes.", "PASS")])

    def test_explicit_pass_ignores_lowercase_negation(self):
        self.check([("Verdict: PASS. The two parsers show no difference and lint passes.", "PASS"),
                    ("Verdict: PASS — no regressions, lint passes", "PASS"), ("**Verdict: PASS**. No gate pass is saved, the README waits.", "PASS"),
                    ("Verdict: PASS. NOT PASSED for the changelog.", "FAIL"), ("Verdict: PASS, NO PASS for the schema", "FAIL"),
                    ("Verdict: PASS, README NOT-PASSED", "FAIL"), ("Verdict: PASS; the build fails.", "FAIL"),
                    ("Verdict: not ready to pass now", "FAIL"), ("is not ready to pass here.\n\nTwo issues are open.", "FAIL"),
                    ("Verdict: the parser doesn't pass.", "FAIL")])

    def test_plain_and_missing_verdicts(self):
        self.check([("**PASS** for T-5 at 8a9b0c1.", "PASS"), ("FAIL for the config file sample.", "FAIL"),
                    ("PASS, hash 3c4d5e6f708192a3, saved for 24h.", "PASS"),
                    ("No final PASS/FAIL so far; it waits on the next build.", None),
                    ("The README FAILS on wording while the parser passes.", "FAIL"), ("", None), (None, None),
                    ("x" * 600 + "\nVerdict: FAIL", None), ("Verdict: pending, the check continues.", None)])


class VerdictEventTest(Base):
    def stop(self, aid, text):
        self.run_hook({"hook_event_name": "SubagentStop", "session_id": SESSION, "agent_id": aid, "last_assistant_message": text})
        return json.loads(self.events("stop")[-1]["data"])["verdict"]

    def test_stop_event_records_parsed_verdict(self):
        for i, (atype, text, want) in enumerate([
                ("codex", "Verdict: NOT RUN - the Codex quota ran out.\nPASS on nothing.", "none"),
                ("codex", "Verdict: NOT COMPLETED. Codex stopped early.", "none"),
                ("reviewer", "Verdict: PASS for the parser.\nVerdict: FAIL for the changelog.", "FAIL"),
                ("reviewer", "Verdict: PASS. The FAIL before was on an older commit.", "FAIL"),
                ("reviewer", "The diff is read; no verdict so far.", None),
                ("software-engineer", "Verdict: PASS", None)]):
            aid = f"a{i}"
            self.add_agent(aid, atype)
            self.assertEqual(self.stop(aid, text), want, text)

    def test_backfilled_report_records_parsed_verdict(self):
        self.add_agent("cdx9", "codex", status="done")
        con = T.connect()
        T.emit(con, "stop", "codex#cdx9", "", session=SESSION, agent_id="cdx9", actor_type="codex", data={"verdict": None})
        con.close()
        body = "Verdict: NOT RUN - Codex hung.\nAn older run had PASS."
        prompt = f'<agent-message from="cdx9">[Subagent hand-back] done\n  {body.replace(chr(10), chr(10) + "  ")}\n</agent-message>'
        self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": SESSION, "prompt": prompt})
        self.assertEqual(json.loads(self.events("stop")[-1]["data"])["verdict"], "none")
        self.assertEqual(json.loads(self.events("report")[-1]["data"])["verdict"], "none")


class AskBlockTest(Base):
    def test_block_with_no_items_makes_no_event(self):
        for text in ["Готово.\n\n**Що від тебе**\n", "Готово.\n\n**Що від тебе**\n\n**Рішення**\n- лишив старий API — менше змін",
                     "Done.\n\n**What I need from you**\n\n## Decisions\n- kept the old API — fewer changes", "**Що від тебе:**",
                     "Звіт.\n**Що від тебе**"]:
            self.assertIsNone(hook.user_asks(text), text)

    def test_later_from_you_line_makes_no_event(self):
        for text in ["Готово.\n\nПізніше від тебе: апрув PR після зеленого CI.",
                     "Готово.\n\nПізніше від тебе: апрув PR після зеленого CI.\n\n**Що від тебе**\n",
                     "Готово.\n\n**Пізніше від тебе:** апрув PR.\n\n**Рішення**\n- x — y",
                     "Done.\n\nLater from you: approve PR #47 after CI.\n\n**What I need from you**\n",
                     "**Що від тебе**\nПізніше від тебе: апрув PR #47.",
                     "**Що від тебе пізніше**\n- апрув PR #47", "**What I need from you later:** approve PR #47",
                     "Від тебе пізніше: апрув PR #47."]:
            self.assertIsNone(hook.user_asks(text), text)
        got = hook.user_asks("Пізніше від тебе: апрув PR після CI.\n\n**Що від тебе**\n1. Апрувни [web-app#47](https://x/47)")
        self.assertIn("web-app#47", got)
        self.assertNotIn("після CI", got)

    def test_now_items_still_make_an_event(self):
        for text in ["**Що від тебе**\n1. Апрувни PR #47\n\n**Пізніше**\n- тег v1.2.3", "**Що від тебе**\nАпрувни PR #47.",
                     "Від тебе те саме: чернетка в DM і два питання. Пізніше: апрув PR.",
                     "**What I need from you**\nNothing now; once CI is green, approve PR #47.",
                     "**Що від тебе**\nЗараз нічого.\n- Завтра — апрув PR #47"]:
            self.assertTrue(hook.user_asks(text), text)

    def test_now_label_ends_a_later_section(self):
        for text in ["## What I need from you\n**Later**\n- rotate keys after release\n### Now\n- approve PR #47",
                     "**Що від тебе**\n**Пізніше**\n- ротація ключів\n### Зараз\n- апрув PR #47",
                     "**Що від тебе**\n**Пізніше**\n- ротація ключів\n**Зараз**\n- апрув PR #47",
                     "**Що від тебе**\nПізніше:\n- ротація ключів\nЗараз:\n- апрув PR #47",
                     "## What I need from you\n### Later\n- rotate keys\n### Now:\n- approve PR #47",
                     "**What I need from you**\nNothing right now.\n\nLater:\n- rotate keys\n\nNow:\n- approve PR #47"]:
            got = hook.user_asks(text)
            self.assertTrue(got, text)
            self.assertRegex(got, "PR #47")
        for text in ["## What I need from you\n### Now\n- nothing\n### Later\n- rotate keys",
                     "**Що від тебе**\n### Зараз\n- нічого\n### Пізніше\n- ротація ключів"]:
            self.assertIsNone(hook.user_asks(text), text)

    def test_now_label_starts_a_new_part(self):
        heads = {"en": ("## What I need from you", "Later", "- rotate keys after release", "Now", "Approve PR #47."),
                 "uk": ("**Що від тебе**", "Пізніше", "- ротація ключів після релізу", "Зараз", "Апрувни PR #47.")}
        for head, later, item, now, ask in heads.values():
            for form in ("### {}", "**{}**", "{}:"):
                for before in (f"{head}\n{form.format(later)}\n{item}\n", f"{head}\n"):
                    for text in (before + form.format(now) + "\n" + ask,
                                 before + (form.format(now + ":") if form != "{}:" else form.format(now)) + " " + ask):
                        got = hook.user_asks(text)
                        self.assertTrue(got, text)
                        self.assertIn("PR #47", got)
        for text in ["## What I need from you\n### Now: nothing\n### Later\n- rotate keys",
                     "**Що від тебе**\n**Зараз: нічого**\n**Пізніше**\n- ротація ключів",
                     "**Що від тебе**\n**Пізніше**\n- ротація ключів\n### Зараз\nнічого"]:
            self.assertIsNone(hook.user_asks(text), text)

    def test_more_now_labels(self):
        for label in ("А зараз", "На зараз", "Сьогодні", "Терміново", "До 18:00", "Today", "Urgent", "Later today", "Пізніше сьогодні"):
            for text in (f"**Що від тебе**\n**Пізніше**\n- ротація ключів\n**{label}**\n- апрув PR #47",
                         f"**Що від тебе**\nПізніше:\n- ротація ключів\n{label}: апрув PR #47",
                         f"**Що від тебе**\nЗараз нічого.\n{label}: апрув PR #47"):
                got = hook.user_asks(text)
                self.assertTrue(got, text)
                self.assertIn("PR #47", got)

    def test_numbered_item_after_a_later_list(self):
        got = hook.user_asks("**Що від тебе**\nПізніше:\n- ротація ключів\n\n1. Апрувни PR #47")
        self.assertIn("PR #47", got)
        self.assertIsNone(hook.user_asks("**Що від тебе**\n\nЗараз нічого.\n\nПізніше:\n\n1. Апрув PR\n2. Токени"))
        self.assertIsNone(hook.user_asks("**Що від тебе**\nПізніше:\n1. Апрув PR\n2. Токени"))

    def test_later_today_heading_is_a_current_ask(self):
        for text in ["**What I need from you later today**\n- approve PR #47", "**Що від тебе пізніше сьогодні**\n- апрув PR #47",
                     "**Що від тебе:** зараз нічого, пізніше сьогодні — апрув PR #47."]:
            self.assertTrue(hook.user_asks(text), text)

    def test_short_label_ends_a_later_section(self):
        for label in ("Після CI", "Увечері", "Завтра зранку", "ASAP", "Right now"):
            for text in (f"**Що від тебе**\nПізніше:\n- тег\n{label}:\n- апрув PR #47", f"**Що від тебе**\n**Пізніше**\n- тег\n\n{label}:\nАпрувни PR #47."):
                got = hook.user_asks(text)
                self.assertTrue(got, text)
                self.assertIn("PR #47", got)
        self.assertIsNone(hook.user_asks("**Що від тебе**\nПізніше:\n- тег\n\nРішення:\n- лишив старий API"))

    def test_short_heading_ends_a_later_section(self):
        for text in ["**Later**\n- rotate keys\n### Right now\n- Approve PR #47.", "**Later**\n- rotate keys\n**Right now**\n- Approve PR #47.",
                     "**Пізніше**\n- ротація ключів\n### Після CI\n- апрув PR #47", "**Пізніше**\n- ротація ключів\n**Увечері**\n\nАпрувни PR #47."]:
            got = hook.user_asks("**What I need from you**\n" + text)
            self.assertTrue(got, text)
            self.assertIn("PR #47", got)
        self.assertIsNone(hook.user_asks("**What I need from you**\n**Later**\n- rotate keys\n### Risks\n- the migration is slow"))

    def test_right_now_label_with_text_after_a_later_list(self):
        for line in ("Right now: Approve PR #47.", "### Right now: Approve PR #47.", "**Right now:** Approve PR #47.", "- Right now: Approve PR #47."):
            for head in ("**What I need from you**\n**Later**\n- rotate keys\n", "**Що від тебе**\nПізніше:\n- тег\n"):
                got = hook.user_asks(head + line)
                self.assertTrue(got, head + line)
                self.assertIn("PR #47", got)

    def test_section_words_match_the_full_label_only(self):
        for sub in ("**Статус PR #47:**", "Release notes:", "### Status of the PR", "**Note for PR #47**"):
            text = "**Що від тебе**\nПізніше:\n- тег\n" + sub + "\n- апрув PR #47"
            got = hook.user_asks(text)
            self.assertTrue(got, text)
            self.assertIn("апрув PR #47", got)
        for sub in ("**Статус**", "Status:", "### Notes", "**Примітки:**", "## Note"):
            self.assertIsNone(hook.user_asks("**Що від тебе**\nПізніше:\n- тег\n" + sub + "\n- CI зелений"), sub)

    def test_status_and_note_are_sections(self):
        for sub in ("Статус", "Status", "Примітка", "Note", "Notes"):
            for form in ("### {}", "**{}**", "{}:"):
                for head in ("**What I need from you**\n**Later**\n- rotate keys\n", "**Що від тебе**\nПізніше:\n- тег\n"):
                    text = head + form.format(sub) + "\n- CI is green, PR #47 is open"
                    self.assertIsNone(hook.user_asks(text), text)
        got = hook.user_asks("**Що від тебе**\n- апрув PR #47\n\n**Статус**\n- CI зелений")
        self.assertIn("PR #47", got)
        self.assertNotIn("CI", got)

    def test_blank_line_after_a_now_label(self):
        for text in ["**Що від тебе**\nПізніше:\n- тег\n### Now\n\nApprove PR #47.", "**Що від тебе**\nПізніше:\n- тег\n**Зараз**\n\nАпрувни PR #47.",
                     "**Що від тебе**\nПізніше:\n- тег\n\nЗараз:\n\nАпрувни PR #47."]:
            got = hook.user_asks(text)
            self.assertTrue(got, text)
            self.assertIn("PR #47", got)
        got = hook.user_asks("**Що від тебе**\n### Now\n\nApprove PR #47.\n\nI will merge after that.")
        self.assertNotIn("merge after", got)

    def test_stop_hook_events(self):
        for text in ["Готово.\n\n**Що від тебе**\n", "Пізніше від тебе: апрув PR.\n\n**Що від тебе**\n",
                     "**Що від тебе**\nЗараз нічого.\n\nПізніше:\n- вибрати назву бакета", "Пізніше від тебе: апрув PR #47."]:
            self.run_hook({"hook_event_name": "Stop", "session_id": SESSION, "last_assistant_message": text})
        self.assertEqual(self.events("needs_user"), [])
        self.run_hook({"hook_event_name": "Stop", "session_id": SESSION,
                       "last_assistant_message": "Пізніше від тебе: тег.\n\n**Що від тебе**\n1. Апрувни PR #47"})
        got = self.events("needs_user")
        self.assertEqual(len(got), 1)
        self.assertIn("PR #47", got[0]["text"])
        self.assertNotIn("тег", got[0]["text"])


class DoneReminderTest(Base):
    ASK = "Звіт.\n\n**Що від тебе**\n1. Апрувни PR #47"

    def prompt(self, text, session=SESSION, **extra):
        out = self.run_hook(dict({"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": text}, **extra))
        return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")

    def stop(self, text, session=SESSION):
        self.run_hook({"hook_event_name": "Stop", "session_id": session, "last_assistant_message": text})

    def card(self, title, note, session=None):
        con = T.connect()
        try:
            return T.task_add(con, title, "lead", status="waiting_user", note=note, session=session)
        finally:
            con.close()

    def test_done_word_lists_open_asks_and_cards(self):
        self.stop(self.ASK)
        tid = self.card("Апрув релізу", "мердж PR #50")
        got = self.prompt("зробив, апрувнув")
        self.assertIn("Open-asks reminder", got)
        self.assertIn("Апрувни PR #47", got)
        self.assertIn(tid, got)
        self.assertIn("мердж PR #50", got)
        self.assertIn(f"task done|todo {tid}", got)
        self.assertEqual(len(self.events("request")), 1)

    def test_each_done_word_triggers(self):
        self.card("Апрув релізу", None)
        for word in ["зробив", "Зробила", "апрувнув", "відправив", "готово", "Done", "approved", "sent it"]:
            self.assertIn("Open-asks reminder", self.prompt(word), word)

    def test_more_done_words(self):
        self.card("Апрув релізу", None)
        for word in ["апрувед, мерджу далі", "залогінився", "запустив скрипт", "видалив чернетку", "додав токен"]:
            self.assertIn("Open-asks reminder", self.prompt(word), word)

    def test_question_gets_no_reminder(self):
        self.card("Апрув релізу", None)
        for text in ["все готово з тегом?", "готово? можна йти далі?", "ти запустив білд?", "Done?"]:
            self.assertNotIn("Open-asks reminder", self.prompt(text), text)
        for text in ["1. відправив 2. ок", "відправив. А далі що?", "готово!"]:
            self.assertIn("Open-asks reminder", self.prompt(text), text)

    def test_no_reminder_without_done_word_or_open_items(self):
        self.stop(self.ASK)
        for text in ["а що з тегом?", "ще не зробив", "not done yet", "надіслав", "<agent-message from=\"x\">done</agent-message>",
                     "зробивши паузу, продовжу", "senta"]:
            self.assertNotIn("Open-asks reminder", self.prompt(text), text)
        self.assertNotIn("Open-asks reminder", self.prompt("зробив"))

    def test_answered_ask_is_not_listed_again(self):
        self.stop(self.ASK)
        tid = self.card("Апрув релізу", None)
        self.assertIn("Апрувни PR #47", self.prompt("апрувнув"))
        got = self.prompt("готово")
        self.assertNotIn("Апрувни PR #47", got)
        self.assertIn(tid, got)

    def test_other_session_asks_are_not_listed(self):
        self.stop("**Що від тебе**\n1. Тег v9.9.9", session=OTHER)
        self.stop(self.ASK)
        got = self.prompt("зробив")
        self.assertIn("Апрувни PR #47", got)
        self.assertNotIn("v9.9.9", got)

    def test_subagent_prompt_gets_no_reminder(self):
        self.stop(self.ASK)
        con = T.connect()
        con.execute("INSERT INTO agents(agent_id,session,handle,type,status,started,last_ts,cursor,linked) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("eng1", SESSION, "software-engineer#eng1", "software-engineer", "running", T.now(), T.now(), 0, "meta"))
        con.close()
        self.assertNotIn("Open-asks reminder", self.prompt("done", agent_id="eng1"))

    def test_error_fails_open(self):
        self.prompt("привіт")
        self.stop(self.ASK)
        con = T.connect()
        T.emit(con, "say", "eng", "inbox line for the lead", session=SESSION, target="lead")
        con.close()
        with mock.patch.object(T, "open_asks", side_effect=RuntimeError("boom")):
            got = self.prompt("зробив")
        self.assertNotIn("Open-asks reminder", got)
        self.assertIn("inbox line for the lead", got)
        self.assertEqual(len(self.events("request")), 2)
        guard = self.events("guard")
        self.assertEqual(json.loads(guard[-1]["data"])["gate"], "asks")
        self.assertIn("boom", (self.dir / "hook-errors.log").read_text())


class NeedsCliTest(Base):
    def cli(self, *args, session=None):
        env = dict(os.environ, TEAM_DB=str(self.dir / "team.db"), TEAM_LANG="en")
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        if session:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return subprocess.run([sys.executable, str(ROOT / "team.py"), "needs", *args], capture_output=True, text=True, env=env,
                              check=True, timeout=30).stdout

    def test_needs_prints_open_items(self):
        self.assertEqual(self.cli().strip(), "nothing open")
        self.run_hook({"hook_event_name": "Stop", "session_id": SESSION, "last_assistant_message": "**Що від тебе**\n1. Апрувни PR #47"})
        self.run_hook({"hook_event_name": "Stop", "session_id": OTHER, "last_assistant_message": "**Що від тебе**\n1. Тег v9.9.9"})
        con = T.connect()
        tid = T.task_add(con, "Апрув релізу", "lead", status="waiting_user", note="Продуктове: 20 MB чи 8 MB?")
        con.close()
        everything = self.cli()
        for want in ("Апрувни PR #47", "v9.9.9", tid, "20 MB"):
            self.assertIn(want, everything)
        mine = self.cli(session=SESSION)
        self.assertIn("Апрувни PR #47", mine)
        self.assertNotIn("v9.9.9", mine)
        self.assertIn(tid, mine)
        self.assertIn("v9.9.9", self.cli("--all", session=SESSION))
        self.assertNotIn("Апрувни", self.cli("--session", OTHER))
        self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": SESSION, "prompt": "апрувнув"})
        self.assertNotIn("Апрувни PR #47", self.cli())


if __name__ == "__main__":
    unittest.main()
