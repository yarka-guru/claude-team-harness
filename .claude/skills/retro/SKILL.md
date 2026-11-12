---
name: retro
description: Retrospective of the AI dev team - measure the last week (or one shipped feature) from the team event log and the user's own words, find what slowed or broke the work, and apply up to three concrete improvements to agent lessons, agent definitions, CLAUDE.md or the knowledge base. Run on Friday (launchd runs it at 16:07) or right after a multi-agent feature ships.
---

# Retrospective

You are the team lead. The point is a better team next week, so every finding ends in a change to a file or is dropped. Output: `.claude/team/rituals/retro/<YYYY-MM-DD>.md`, applied improvements, a ritual event, a summary for the user. No Slack, Jira or GitHub writes.

`T` below means `python3 .claude/team/team.py`, run from the workspace root.

1. Evidence, in parallel:
   - `T stats --days 7` (or the feature's window): runs per role with duration, model/effort mix, reviewer and codex verdicts, rework (cards that needed more than one worker run), asks and how many got answered, conflicts, overlaps, guards, needs-you waits, cycle time.
   - `T digest --hours 168`.
   - The week's daily and reflect notes, and the previous retro's actions.
   - `T transcripts --hours 168 --width 300`: the user's own messages. Corrections, repeats and frustration ("no", "not like that", "I already said", "again?", "stop", in whatever language the user writes) are escapes: problems the team let through to the user. Each one counts more than any metric.
2. Analyse, briefly and with numbers:
   - Escapes: what did the user catch that the reviewer and codex did not? Why did the review miss it?
   - Staffing: right role for each card? Model/effort too low (FAILs, rework) or too high (cheap lookups on opus)?
   - Coordination: conflicts, overlaps, guards, unanswered asks. Which plan or file split would have avoided them?
   - Flow: where did cards wait (review, waiting_user)? How long did the user wait for answers?
   - Did last retro's actions help? Keep, change or revert each.
3. Choose at most three actions, each tied to evidence, and apply them now:
   - a role lesson: `.claude/team/lessons/<agent-type>.md` (or `all.md`), one imperative line with the reason. It is injected into every future run of that role. Keep each file under 15 lines: merge or drop old lines;
   - an agent definition change in `.claude/agents/*.md` (tools, default model/effort, rules);
   - a CLAUDE.md change (staffing table, control rules);
   - a knowledge-base entry in `.claude/knowledge/` (skill `kb`; then run `python3 .claude/skills/kb/kb.py`).
   Do not touch another assistant's configuration (`AGENTS.md`, `.codex/**`) if the workspace has it. Do not write memory here unless the user stated a preference; `reflect` owns memory.
4. Note, in the team's language (English by default; set it once in CLAUDE.md): numbers, what went well, what did not, actions with the file each one changed, actions carried over. Then `T ritual retro <path> "<summary>"` and `T decision --as lead "Retro: <actions>"`.
5. Reply to the user: three to five lines, ending with any idea that needs their decision before it is applied.
