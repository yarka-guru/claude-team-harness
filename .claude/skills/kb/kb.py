#!/usr/bin/env python3
"""Knowledge-base tooling: validate entry frontmatter and regenerate INDEX.md.

    python3 kb.py            validate every entry, rewrite INDEX.md (nothing is written on problems)
    python3 kb.py --check    validate only, exit 1 on problems or a stale INDEX.md
    python3 kb.py --review   list what needs a human look (open entries, notes, follow-ups, old reference)
"""
import argparse
import datetime
import json
import pathlib
import re
import sys

KB = pathlib.Path(__file__).resolve().parents[2] / "knowledge"
INDEX = KB / "INDEX.md"
NOT_ENTRIES = {"INDEX.md", "README.md"}

DOMAINS = {
    "method": "Method: verification and measurement",
    "process": "Process and team rules",
    "infra": "Infrastructure: clusters, nodes, networking, storage",
    "deploy": "Delivery: infrastructure as code, Helm, GitOps",
    "observability": "Observability and alerting",
    "services": "Services and libraries",
    "ui": "Web UI",
    "cli": "CLI and SDK",
    "ci": "CI and builds",
    "local": "Local dev box",
}
TYPES = {"case", "gotcha", "runbook", "reference", "method", "process"}
STATUSES = {"resolved", "open", "workaround", "reference"}
REQUIRED = ("description", "domain", "type", "status", "tickets", "updated")
OPTIONAL = ("revisit", "note")
QUOTED = ("description", "note")
LISTS = ("tickets", "revisit")
TICKET = re.compile(r"[A-Z][A-Z0-9]*-\d+")
MAX_DESCRIPTION = 200
REFERENCE_MAX_AGE_DAYS = 60


def parse(path):
    """Return (meta, problems). The format is a strict subset of YAML: one `key: value` per line."""
    lines = path.read_text().split("\n")
    if lines[0] != "---":
        return None, ["no frontmatter"]
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None, ["frontmatter is not closed with `---`"]
    meta, problems = {}, []
    for line in lines[1:end]:
        key, sep, value = line.partition(": ")
        if not sep or not re.fullmatch(r"[a-z_]+", key):
            problems.append(f"unparseable frontmatter line: {line!r}")
            continue
        if key in meta:
            problems.append(f"duplicate key `{key}`")
            continue
        value = value.strip()
        if key in QUOTED:
            try:
                value = json.loads(value)
            except ValueError:
                problems.append(f"`{key}` must be one double-quoted string")
                continue
            if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
                problems.append(f"`{key}` must be a non-empty single-line string")
                continue
        elif key in LISTS:
            if not (value.startswith("[") and value.endswith("]")):
                problems.append(f"`{key}` must be a list like [PROJ-7, PROJ-8] or []")
                continue
            inner = value[1:-1].strip()
            value = [v.strip() for v in inner.split(",")] if inner else []
            bad = [v or "<empty>" for v in value if not TICKET.fullmatch(v)]
            if bad:
                problems.append(f"`{key}` has invalid ticket keys: {', '.join(bad)}")
                continue
        elif not re.fullmatch(r"[A-Za-z0-9-]+", value):
            problems.append(f"`{key}` has an unsupported value: {value!r}")
            continue
        meta[key] = value
    return meta, problems


def validate(meta):
    problems = [f"missing `{key}`" for key in REQUIRED if key not in meta]
    problems += [f"unknown key `{key}`" for key in meta if key not in REQUIRED + OPTIONAL]
    if "domain" in meta and meta["domain"] not in DOMAINS:
        problems.append(f"unknown domain `{meta['domain']}`")
    if "type" in meta and meta["type"] not in TYPES:
        problems.append(f"unknown type `{meta['type']}`")
    if "status" in meta and meta["status"] not in STATUSES:
        problems.append(f"unknown status `{meta['status']}`")
    if len(meta.get("description", "")) > MAX_DESCRIPTION:
        problems.append(f"description over {MAX_DESCRIPTION} chars")
    if "updated" in meta:
        try:
            if not re.fullmatch(r"\d{4}-\d\d-\d\d", meta["updated"]):
                raise ValueError
            datetime.date.fromisoformat(meta["updated"])
        except ValueError:
            problems.append("`updated` is not a valid YYYY-MM-DD date")
    return problems


def load():
    entries, problems = {}, []
    if not KB.is_dir():
        problems.append(f"no knowledge directory at {KB}")
    paths = sorted(p for p in KB.glob("*.md") if p.name not in NOT_ENTRIES)
    for path in paths:
        meta, found = parse(path)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", path.stem):
            found.append("file name must be lowercase letters, digits, `-` or `_`")
        if meta is not None:
            found += validate(meta)
        problems += [f"{path.name}: {p}" for p in found]
        if meta is not None:
            entries[path.stem] = meta
    return entries, problems


def render(entries):
    out = [
        "# Knowledge Base",
        "",
        "GENERATED by `../skills/kb/kb.py` from each entry's frontmatter. Do not edit by hand:",
        "change the entry, then re-run the script.",
        "",
        "Line format: entry, what it is about, **status**, tickets (and follow-up tickets), last update,",
        "and a `note` when the body is known to lag behind reality.",
    ]
    if not entries:
        out += ["", "No entries yet."]
    for domain, title in DOMAINS.items():
        rows = [(stem, m) for stem, m in entries.items() if m["domain"] == domain]
        if not rows:
            continue
        out += ["", f"## {title}", ""]
        for stem, m in sorted(rows, key=lambda r: (r[1]["updated"], r[0]), reverse=True):
            parts = [m["description"], f"**{m['status']}**"]
            if m["tickets"]:
                parts.append(", ".join(m["tickets"]))
            if m.get("revisit"):
                parts.append("follow-up " + ", ".join(m["revisit"]))
            parts.append(m["updated"])
            line = f"- [{stem}]({stem}.md): " + " · ".join(parts)
            if m.get("note"):
                line += f" · NOTE: {m['note']}"
            out.append(line)
    return "\n".join(out) + "\n"


def review(entries):
    today = datetime.date.today()
    unsettled = {s: m for s, m in entries.items() if m["status"] in ("open", "workaround")}
    print(f"Open / workaround ({len(unsettled)}):")
    for stem, m in unsettled.items():
        print(f"  {stem}  [{m['status']}]  {', '.join(m['tickets'])}")
    tickets = sorted({t for m in unsettled.values() for t in m["tickets"]})
    if tickets:
        print("\nJQL, tickets of unsettled entries that Jira already closed:")
        print(f"  key in ({', '.join(tickets)}) AND statusCategory = Done")
    settled = sorted({t for m in entries.values() if m["status"] == "resolved" for t in m["tickets"]})
    if settled:
        print("\nJQL, tickets of resolved entries that Jira still has open:")
        print(f"  key in ({', '.join(settled)}) AND statusCategory != Done")
    revisit = sorted({t for m in entries.values() for t in m.get("revisit", [])})
    if revisit:
        print("\nJQL, follow-up tickets whose closing means an entry must be re-read:")
        print(f"  key in ({', '.join(revisit)}) AND statusCategory = Done")
        for stem, m in entries.items():
            if m.get("revisit"):
                print(f"  {stem}: {', '.join(m['revisit'])}")
    noted = {s: m for s, m in entries.items() if m.get("note")}
    print(f"\nEntries carrying a note ({len(noted)}): caveats, fix the body or re-verify, then drop the note")
    for stem, m in noted.items():
        print(f"  {stem}: {m['note']}")
    print(f"\nReference entries not updated for {REFERENCE_MAX_AGE_DAYS}+ days:")
    for stem, m in entries.items():
        age = (today - datetime.date.fromisoformat(m["updated"])).days
        if m["status"] == "reference" and age >= REFERENCE_MAX_AGE_DAYS:
            print(f"  {stem}  ({age} d)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate only")
    mode.add_argument("--review", action="store_true", help="list what needs a human look")
    args = parser.parse_args()

    entries, problems = load()
    for problem in problems:
        print(f"PROBLEM {problem}", file=sys.stderr)
    if problems:
        print("INDEX.md left untouched: fix the problems above first", file=sys.stderr)
        return 1
    if args.review:
        review(entries)
        return 0
    text = render(entries)
    if args.check:
        if not INDEX.exists() or INDEX.read_text() != text:
            print("PROBLEM INDEX.md is out of date, run kb.py", file=sys.stderr)
            return 1
        return 0
    INDEX.write_text(text)
    print(f"INDEX.md: {len(entries)} entries, {len(text)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
