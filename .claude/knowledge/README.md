# Knowledge base

One Markdown file per solved problem, gotcha, runbook or reference, each with the frontmatter
described in `../skills/kb/SKILL.md`. It starts empty: the team adds entries while working and
in the `reflect` and `retro` rituals.

After adding or changing an entry, run `python3 .claude/skills/kb/kb.py` from the workspace root.
It validates the frontmatter and regenerates `INDEX.md`. This README and `INDEX.md` are not
entries.
