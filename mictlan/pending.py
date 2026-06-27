#!/usr/bin/env python3
"""Aggregate still-pending node/link proposals across the full dream-journal history.

Walks every dreams/*.md, harvests the candidate slugs that the dream cycle
(and, once their shims land, the other agents) proposed under "Held for review"
and "Proposed new note stubs", drops anything that now resolves to a real note
(direct slug or `<slug>-cafe` / de-hyphenated alias forms), and emits the
leftovers — the backlog a human still has to approve.

Intended to be called at the start of the interactive `/dream` backlog-review
step so no proposal is silently parked forever. Output is JSON on stdout.

    uv run _system/scripts/pending_proposals.py            # all pending
    uv run _system/scripts/pending_proposals.py --since 2026-06-01
"""
import argparse
import glob
import json
import os
import re
import sys

from mictlan.paths import VAULT
NOTES_DIR = os.path.join(VAULT, "notes")
DREAMS_DIR = os.path.join(VAULT, "dreams")

SECTION_HEADERS = ("Held for review", "Proposed new note stubs")

# slug-bearing patterns inside a proposal line
SLUG_PATTERNS = [
    re.compile(r"would create \[\[([a-z0-9][a-z0-9-]+)\]\]", re.I),
    re.compile(r"Suggested slug:\s*`?([a-z0-9][a-z0-9-]+)`?", re.I),
    re.compile(r"create (?:new note )?\[\[([a-z0-9][a-z0-9-]+)\]\]", re.I),
]
TYPE_PATTERN = re.compile(r"type:\s*`?([a-z/]+)`?", re.I)

# generic tokens the REM pass throws out that are not worth a node
NOISE = {"none", "macos", "gmail", "productivity", "automation", "config",
         "meta", "business", "planning", "ai-tools"}


def existing_slugs():
    slugs = {os.path.basename(p)[:-3].lower()
             for p in glob.glob(os.path.join(NOTES_DIR, "*.md"))}
    dehyph = {s.replace("-", "") for s in slugs}
    return slugs, dehyph


def is_covered(slug, slugs, dehyph):
    s = slug.lower()
    if s in slugs:
        return True
    if s.replace("-", "") in dehyph:
        return True
    # F&B portfolio convention: `<name>` proposals land as `<name>-cafe`
    if f"{s}-cafe" in slugs:
        return True
    return False


def extract_section(text, header):
    pat = re.compile(r"^##\s+" + re.escape(header) + r".*?$(.*?)(?=^##\s|\Z)",
                     re.S | re.M)
    m = pat.search(text)
    return m.group(1) if m else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO date; ignore journals before it")
    args = ap.parse_args()

    slugs, dehyph = existing_slugs()
    pending = {}  # slug -> {type, times_seen, first_seen, last_seen}

    for jpath in sorted(glob.glob(os.path.join(DREAMS_DIR, "*.md"))):
        date = os.path.basename(jpath)[:-3]
        if args.since and date < args.since:
            continue
        text = open(jpath, errors="ignore").read()
        for header in SECTION_HEADERS:
            sec = extract_section(text, header)
            for line in sec.splitlines():
                for pat in SLUG_PATTERNS:
                    for slug in pat.findall(line):
                        s = slug.lower()
                        if s in NOISE or is_covered(s, slugs, dehyph):
                            continue
                        tm = TYPE_PATTERN.search(line)
                        rec = pending.setdefault(
                            s, {"slug": s, "type": tm.group(1) if tm else "topic",
                                "times_seen": 0, "first_seen": date,
                                "last_seen": date})
                        rec["times_seen"] += 1
                        rec["last_seen"] = max(rec["last_seen"], date)
                        rec["first_seen"] = min(rec["first_seen"], date)

    out = sorted(pending.values(),
                 key=lambda r: (-r["times_seen"], r["slug"]))
    json.dump({"pending_count": len(out), "pending": out},
              sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
