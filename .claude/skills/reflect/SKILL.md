---
name: reflect
description: The lead's daily hour of reflection and "sleep" - replay the day, turn the user's corrections into memory, solved problems into knowledge-base entries and agent mistakes into role lessons, consolidate memory and tidy the team state. Run every weekday evening (launchd runs it at 18:47) or when the session brief says reflection is overdue.
---

# Reflect and sleep

Sleep is when the day's experience becomes durable knowledge and noise is dropped. You are the team lead doing that for yourself and the team. Output: `.claude/team/rituals/reflect/<YYYY-MM-DD>.md` listing every file you changed, a ritual event, a short summary. No Slack, Jira or GitHub writes.

`T` below means `python3 .claude/team/team.py`, run from the workspace root. Memory lives in `~/.claude/projects/<workspace-slug>/memory/` (index `MEMORY.md`; the slug is the workspace's absolute path with every non-alphanumeric character replaced by `-`); the knowledge base in `.claude/knowledge/`.

1. Replay the day: `T transcripts --hours 24 --width 500`, `T digest --hours 24`, today's daily note.
2. Extract, each item with its evidence (session and time):
   - A user correction or a stated preference becomes a memory, following the memory rules: one fact per file, frontmatter, `**Why:**` and `**How to apply:**`, a one-line pointer in `MEMORY.md`. Read the existing memories first and update the one that covers it instead of adding a duplicate.
   - A problem solved today that the knowledge base lacks becomes an entry in `.claude/knowledge/` with frontmatter (skill `kb`); then run `python3 .claude/skills/kb/kb.py`.
   - A mistake an agent made becomes one line in `.claude/team/lessons/<agent-type>.md`.
   Only what will change future behaviour; no diary entries.
3. Consolidate memory: first copy the memory directory to `.claude/backup/memory-<YYYY-MM-DD>/`. Read `MEMORY.md` and the files it points to; check that the files, flags and commands they name still exist. Add and update memories. Merging overlapping memories or deleting wrong ones is the lead's technical call (the backup makes it reversible): do it, fix every `[[link]]` and skill reference to the old names, and list it under "Decisions"; never ask the user about memory housekeeping. Keep `MEMORY.md` one line per memory.
4. Consolidate the team: keep each lessons file under 15 lines; if a lesson repeats across roles, move it to `lessons/all.md`.
5. Housekeeping: `T vacuum` (drops old tool-activity rows), `python3 .claude/skills/kb/kb.py --check`.
6. Note, in the team's language (English by default; set it once in CLAUDE.md):

   ```
   # Reflection <date>
   ## What I learned
   ## What I changed (file — change — why)
   ## Questions for you
   ## First thing tomorrow
   ```

   Then `T ritual reflect <path> "<summary>"`.
7. Reply to the user in two or three lines: what changed in memory, the KB and the team, and any open question.
