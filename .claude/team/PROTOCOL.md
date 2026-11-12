# Team protocol

The lead (main session) staffs, controls and accepts the work and is the only one who talks to the user. The user watches a live dashboard of the chat, the board and who holds which file, so keep what you post true and short.

Team chat: `python3 .claude/team/team.py say --as <handle> [--to <handle>|<role>|all] [--kind K] [--task T-n] "text"`
- Write in English or the team's own language, whichever is natural; one or two plain sentences, facts not chatter. The chat is internal to the team, not an outward action: read-only roles post too.
- Kinds: `say` status, `ask` you need advice or a decision, `answer`, `decision` something others must follow (API shape, file ownership, branch, naming), `blocker` you cannot proceed, `handoff` output another agent consumes, `report` your result in one or two lines.
- Post at least: a `say` with your plan when you start; a `decision` or `handoff` the moment you fix something others depend on; a `blocker` as soon as you are stuck; a `report` right before you finish.
- Your latest `say` is the line the user sees next to your name under "Who's doing what". Each time you move to a new step, post one plain sentence of what you are doing now, without ids or tool names: `say --as <handle> "Checking notifications on a live-DB copy"`.
- When another agent owns the area, ask instead of guessing. Messages to you, your role or all arrive by themselves as `[team chat]` lines on your next tool call. Answer only with something specific; otherwise carry on.
- Asking a specific agent to do something (QA, a fix, an answer): SendMessage to them. It wakes an idle agent. A busy agent gets it on its next tool call as a `[team chat #id] … (DM …)` line and once more natively when its turn ends: that is the same message, act on it once. Never conclude a message was lost; check `team.py feed` or wait for their reply.
- Team chat (`team.py ask|answer|handoff --to <handle>`) reaches a working agent on its next tool call and does NOT wake an idle one. Before an agent stops, only an `ask`, `handoff`, `blocker` or `decision` addressed to its handle holds it back once to read; `say`, `answer` and `report` wait until it next wakes. When `team.py` warns that the target is idle or unknown, send it with SendMessage instead.
- Messages arrive in full. One too long for a single delivery ends with a `team.py read <id>` pointer; `team.py read <id>` prints any message in full. For long content (specs, QA lists), still prefer a file in your scratchpad and send the path. Both channels are mirrored to the dashboard.
- When you receive a request from another agent, reply in one line that you took it ("Taking the 4 QA items") before working, so they know it landed.
- Build work runs in pairs: the builder and its reviewer are named teammates and send each other handoffs and findings directly; only the final PASS/FAIL goes to the lead.
- If another agent is working next to you (see «Who else is working right now»), talk to them directly instead of routing through the lead: ask before guessing about their area, hand off results the moment they are ready, review each other's piece when it helps.

Board: `team.py task <start|review|block|wait> T-n [--note "..."]`, `team.py board`, `team.py who`
- Your card moves to review by itself when you finish; only the lead moves it to done. `block` with a note when stuck, `wait` when only the user can unblock it (an approval, a `!` command).

Conflicts
- Every Edit/Write claims the file for you until you finish. Editing a file another agent holds is denied: ask them, do not work around it.
- git checkout/switch/stash/reset/rebase/pull/clean/restore in a worktree where others have edits is denied: take your own worktree (`git worktree add`).

Fact-check gate (enforced by the hook, for every agent and the lead)
- Gated calls: Slack send/draft/schedule message and canvas create/update; Jira create (summary, description, text in additional_fields), edit (text fields), transition with a comment or text fields, comment, worklog comment; Confluence page create/update (incl. versionMessage) and footer/inline comments; `gh pr create|edit|comment|review|close|reopen|merge`, `gh issue create|edit|comment|close|reopen`, `gh release create|edit`; `gh api` writes to comments/replies/issues/pulls/releases/reviews endpoints; GraphQL mutations with text. A gated call is blocked while its text has dates, weekdays, times, numbers, versions, ticket/PR refs or cron and no PASS for that exact text in the last 24h. The denial lists the facts and saves the text to `state/factcheck/<hash>.txt`.
- gh text goes only through `--body-file`/`--notes-file <literal path>`, written in an earlier call; `$(...)`, `$VAR` paths, stdin, `--fill` and `--generate-notes` are denied. Text over 100,000 characters is denied: split it or ask the lead to waive.
- PR title flow: run `gh pr create --title "PROJ-34: ..." --body-file body.md`; when blocked, a reviewer passes the saved file the denial names (title and body together) and the same command then goes through unchanged.
- A reviewer or codex agent that did not write or edit the text checks every fact against a primary source: kubectl/API output, `gh pr view`, a Jira read, deployed values at the merged commit, a calendar computation. PR bodies, KB, tickets we wrote, drafts, agent reports and chat are derived and do not count. Recompute every date and number; do not compare one of our texts with another. The tokens are the minimum: names in backticks, procedures and "X reads Y from Z" statements are checked against the code too, never against another ticket or draft that says the same.
- Reviewers have no Write tool: `team.py factcheck tokens <file> --json` prints the template; write claims.json with a Bash heredoc, one entry per fact exactly as printed, `"source_kind": "primary"`; then `team.py factcheck pass <file> --as <own handle> --claims claims.json`. It refuses an uncovered fact, an empty or derived source, a weekday that contradicts a full date, and a first-run date that is not the schedule's first firing (name a later start in the text: "from <date>", "deploy on <date>").
- A wrong fact: `team.py factcheck fail <file> --as <own handle> --note "..."`. Only the lead waives: `team.py factcheck waive <file> --as lead --reason "..."`; every waiver is logged for the retro.
- Any edit changes the hash: check again after every change, then repeat the blocked call unchanged.

Team tooling
- Team tooling (`.claude/team/`: hook.py, team.py, teamlib.py, dashboard) runs on every tool call of every agent: change a copy, test it there, and replace the live files only after the paired reviewer's PASS.
