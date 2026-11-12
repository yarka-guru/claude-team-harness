---
name: planning
description: Weekly planning of the AI dev team - turn the backlog, open Jira tickets and last retro's actions into staffed, sized cards with owners, model/effort, file ownership and risks. Run on Monday (launchd runs it at 09:37), when the board has no todo cards, or before a large multi-feature request.
---

# Planning

You are the team lead. Output: refreshed cards on the board, `.claude/team/rituals/planning/<YYYY-MM-DD>.md`, a ritual event, and a short list of what the user must decide. No Slack, Jira or GitHub writes.

`T` below means `python3 .claude/team/team.py`, run from the workspace root.

1. Inputs, in parallel: `T board --all`; the last retro note and its actions; the last five daily notes; Jira read-only (`assignee = currentUser() AND statusCategory != Done`, and the open sprint if there is one); `T stats --days 14` for how long similar work took.
2. For every candidate piece of work write:
   - goal and why, in one line; definition of done, including the proof (tests, e2e, screenshots, reviewer and codex verdict);
   - owner role and model/effort from the CLAUDE.md table. Use `T stats` evidence: a role/model pair with repeated reviewer FAILs or rework gets a higher tier;
   - the repos and paths it touches. Two cards on the same repo get separate worktrees and one owner per file; write the split down. This is what prevents conflicts later;
   - dependencies and order; what can run in parallel;
   - pre-mortem: "it is Friday and this failed: why?" Turn the top reason into a check in the definition of done;
   - what only the user can do (approvals, `!` commands, merges) and when.
3. WIP limit: at most three cards in `doing` per lead session and one writing agent per worktree. Everything else stays `todo` or `backlog`.
4. Update the board: `T task add "<title>" --feature <F> --owner <role> --model <m> --effort <e> --status todo --note "<DoD in one line>"`, `T task backlog|drop T-n` for what goes down. Tag every later Agent call with its card id in the description (`T-12: ...`).
5. Note, in the team's language (English by default; set it once in CLAUDE.md): priorities in order with owner and model/effort, the file-ownership split, risks, what waits for the user. Then `T ritual planning <path> "<summary>"` and `T decision --as lead "Plan for the week: ..."`.
6. Reply to the user: the plan in five lines at most and the decisions only they can make (priority conflicts, scope cuts).
