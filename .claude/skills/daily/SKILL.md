---
name: daily
description: Daily standup of the AI dev team - what got done since the last daily, what runs today and who owns it, blockers and what waits for the user. Run every weekday morning (launchd runs it at 09:07) or when the session brief says the daily is not written yet.
---

# Daily standup

You are the team lead running the team's daily. Output: one note at `.claude/team/rituals/daily/<YYYY-MM-DD>.md`, a ritual event, a decision line in team chat, and a short summary for the user. Read-only towards the outside world: no Slack, Jira or GitHub writes.

`T` below means `python3 .claude/team/team.py`, run from the workspace root.

1. Gather, in parallel:
   - `T digest --hours 24` (on Monday `--hours 72`): done cards, agent runs with verdicts, decisions, blockers, coordination incidents, the user's requests.
   - `T board` and `T who`.
   - Yesterday's reflection note (`rituals/reflect/`), if any: its "first thing tomorrow" item goes into today's plan.
   - Jira, read-only: `assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC` via `searchJiraIssuesUsingJql`, if a Jira connector is attached. Skip it if the tool is unavailable and say so in the note.
2. Hygiene, internal only (the board is ours to keep true):
   - A card in `doing` with no running agent and no event for 24h: move it to `blocked` with a note, or back to `todo`.
   - A card in `review` older than 24h: it needs acceptance; put it first in today's plan.
   - Board and Jira disagree (work done but ticket still in To Do, card done but ticket in Development): list each mismatch as a proposed transition for the user; do not transition in the daily.
3. Plan today: at most three priorities, each with the owner role and model/effort from the CLAUDE.md table, and the files or repos it touches. Two priorities that touch the same repo get separate worktrees or run in sequence: say which.
4. Write the note in the team's language (English by default; set it once in CLAUDE.md):

   ```
   # Daily <date>
   ## Done
   - plain-language outcomes, no internals
   ## Doing today
   - plain-language outcome of each priority
   ## Who and how (for the team)
   - <card id> <priority> — <owner role> (<model>/<effort>) — <repos/files>
   ## Blockers and what waits for you
   - each with the exact action the user takes (`!` command, approval, answer)
   ## Risks
   - overlaps, stale cards, deadlines (security SLAs, releases)
   ## Mismatches with Jira
   ```

   The "Done / Doing today" bullets are plain outcomes (no ticket numbers or internals) so the user can read them out at a standup; staffing details go only under "Who and how".
5. `T ritual daily <path> "<one-line summary>"` and `T decision --as lead "Plan for today: <1>, <2>, <3>"`.
6. Reply to the user in the team's language with the three priorities and what waits for them. Nothing else.
