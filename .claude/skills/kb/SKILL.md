---
name: kb
description: The team's working knowledge from real incidents and tasks, in `.claude/knowledge/`. Consult before debugging or changing anything, and add or update an entry after solving anything new. The knowledge base starts empty; the team fills it through daily work, `reflect` and `retro`.
---

# Knowledge base

Verified knowledge from the team's own work. Everything lives in `.claude/knowledge/` (relative
to the workspace root): one file per problem, each with frontmatter. This file holds only the
procedure and the rules that cut across entries. Facts live in the entries and nowhere else.

`KB` below means `python3 .claude/skills/kb/kb.py`, run from the workspace root.

## Using it

1. Read `.claude/knowledge/INDEX.md` (one line per entry, grouped by domain). Pick entries by
   symptom.
2. `grep -rl '<error text or component>' .claude/knowledge/` when the index line is not enough;
   the index describes each entry in 200 characters and cannot list every symptom.
3. Open the matching entries and apply the known fix before re-diagnosing from scratch.
4. Trust order for status: frontmatter `status`/`note` first, then the body.

## Adding or changing knowledge

1. Create or edit `.claude/knowledge/<kebab-slug>.md` with this frontmatter:

   ```
   ---
   description: "<=200 chars, symptom first, with the literal error text or component name>"
   domain: method | process | infra | deploy | observability | services | ui | cli | ci | local
   type: case | gotcha | runbook | reference | method | process
   status: resolved | open | workaround | reference
   tickets: [PROJ-123]
   revisit: [PROJ-456]
   updated: YYYY-MM-DD
   note: "optional, temporary: what in the body is known to be stale or unverified"
   ---
   ```

   `tickets` are the ones the entry is about (`[]` when there is none). `revisit` (optional) lists
   follow-up tickets whose closing changes what the entry should say. Describe the body, not an
   older summary of it: read the entry before writing `description` and `status`. The domain list
   lives in `DOMAINS` at the top of `kb.py`; change it there to fit your product.
2. Run `KB` after every change. It validates every entry and regenerates `INDEX.md`; on any
   problem it writes nothing. Never edit `INDEX.md` by hand.
3. When a finding changes, rewrite the entry so it states the current truth. Do not stack a
   correction on top of the old text, and bump `updated`.
4. Add a rule to this file only when it applies across domains. A fact about one component
   belongs in its entry.
5. `KB --check` validates without writing and fails on a stale `INDEX.md`. `KB --review` lists
   open entries, entries with a `note`, stale reference entries, and JQL queries to compare entry
   status and `revisit` tickets with your tracker.

## Cross-cutting rules

None yet. A rule goes here once it has cost at least one wrong conclusion in more than one
domain, with the entry that holds the evidence in brackets.
