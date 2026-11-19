import re
import sys
from pathlib import Path

KINDS = ("brief", "chat", "doc")
RULES = [("H4", "contraction", re.compile(r"\b[A-Za-z]+['’](?:t|s|re|ve|ll|d|m)\b", re.I)),
         ("H5", "semicolon", re.compile(r";"))]
NONLATIN = re.compile(r"[^\W\d_A-Za-z]")
WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?")


def place(text, i):
    line = text.count("\n", 0, i) + 1
    return line, i - (text.rfind("\n", 0, i) + 1) + 1


def finding(text, rule, tag, m):
    line, col = place(text, m.start())
    return {"rule": rule, "tag": tag, "line": line, "col": col, "text": m.group(0)}


def check(text, kind="brief", data_dir=None):
    hard = [finding(text, rule, tag, m) for rule, tag, rx in RULES for m in rx.finditer(text)]
    first = NONLATIN.search(text)
    if first and kind != "chat":
        hard.append(finding(text, "H7", "not English", first))
    hard.sort(key=lambda f: (f["line"], f["col"]))
    sentences = len([s for s in re.split(r"[.!?]+(?:\s+|$)", text) if s.strip()])
    return {"kind": kind, "words": len(WORD.findall(text)), "sentences": sentences, "hard": hard, "soft": [], "error": None}


def summary(result):
    return f"{result['words']} words, {result['sentences']} sentences, {len(result['hard'])} hard, {len(result['soft'])} soft"


def line_of(f, name=None):
    return f"{name + ':' if name else ''}{f['line']}:{f['col']}: {f['rule']} [{f['tag']}] {f['text']}"


def gate(result, mode):
    hard = list(result.get("hard") or [])
    if mode not in ("warn", "deny") or not hard:
        return "allow", ""
    over = len(hard) > 2 or any(f["rule"] == "H7" for f in hard)
    decision = "deny" if mode == "deny" and result.get("kind") == "brief" and over else "warn"
    lines = [f"ste: {summary(result)}"] + ["  " + line_of(f) for f in hard + result["soft"]]
    lines.append(f"Test a draft: team.py ste --kind {result.get('kind')} <file>")
    return decision, "\n".join(lines)


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="ste_lint.py")
    p.add_argument("--kind", choices=KINDS, default="brief")
    p.add_argument("--soft", action="store_true")
    p.add_argument("file")
    a = p.parse_args(argv)
    text = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")
    result = check(text, a.kind)
    for f in result["hard"] + (result["soft"] if a.soft else []):
        print(line_of(f, a.file))
    print(f"ste: {summary(result)}")
    return 1 if result["hard"] else 0


if __name__ == "__main__":
    sys.exit(main())
