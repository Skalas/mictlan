#!/usr/bin/env python3
"""Stage Claude on the web (claude.ai) export into ConversationUnit JSON files.

Reads `<export-dir>/conversations.json` (array of conversation objects), parses
the `chat_messages` of each, filters trivial sessions, pre-greps candidate vault
entities, and writes one JSON file per conversation under
`_system/ingestion/staging/claude-web/`.

Also processes `memories.json` separately and outputs the user-memory text to
`_system/ingestion/staging/claude-web/_memories.txt` for the orchestrator to
review.

Usage:
    uv run --with pyyaml _system/scripts/stage_claude_web.py --src /path/to/claude-web-export
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from mictlan.analyzer import list_existing_aliases, list_existing_slugs

from mictlan.paths import VAULT
STAGING = VAULT / "_system" / "ingestion" / "staging" / "claude-web"

MIN_TURNS = 4
MIN_CHARS_TOTAL = 300
TRIVIAL_TOKENS = {"hi", "hello", "test", "thanks", "ok", "yes", "no", "/clear"}


def clean_message_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return text.strip()


def parse_conversation(c: dict) -> dict | None:
    msgs = c.get("chat_messages") or []
    if len(msgs) < MIN_TURNS:
        return None
    turns = []
    total_chars = 0
    for m in msgs:
        sender = m.get("sender")
        if sender not in ("human", "assistant"):
            continue
        text = clean_message_text(m.get("text") or "")
        if not text or text.lower() in TRIVIAL_TOKENS or len(text) < 3:
            continue
        role = "user" if sender == "human" else "assistant"
        turns.append({"role": role, "content": text, "timestamp": m.get("created_at")})
        total_chars += len(text)
    if len(turns) < MIN_TURNS or total_chars < MIN_CHARS_TOTAL:
        return None

    name = c.get("name", "").strip()
    title = name or next((t["content"][:120].replace("\n", " ") for t in turns if t["role"] == "user"), "")
    return {
        "source": "claude-web",
        "source_id": c["uuid"],
        "uuid": c["uuid"],
        "title": title,
        "summary": c.get("summary", ""),
        "started_at": c.get("created_at"),
        "ended_at": c.get("updated_at"),
        "turn_count": len(turns),
        "total_chars": total_chars,
        "turns": turns,
    }


def pre_grep_entities(unit: dict, aliases: dict[str, str], slugs: set[str]) -> list[dict]:
    import re
    blob = "\n".join(t["content"] for t in unit["turns"]).lower()
    counts: dict[str, int] = {}
    surfaces: dict[str, set[str]] = {}

    def count_surface(slug: str, surface: str) -> None:
        pattern = r"\b" + re.escape(surface.lower()) + r"\b"
        n = len(re.findall(pattern, blob))
        if n > 0:
            counts[slug] = counts.get(slug, 0) + n
            surfaces.setdefault(slug, set()).add(surface)

    for alias, slug in aliases.items():
        if len(alias) < 4:
            continue
        count_surface(slug, alias)
    for slug in slugs:
        if len(slug) < 4:
            continue
        count_surface(slug, slug)
        count_surface(slug, slug.replace("-", " "))

    ranked = sorted(
        ((s, n) for s, n in counts.items() if n >= 2),
        key=lambda x: (-x[1], x[0]),
    )[:25]
    return [{"slug": s, "mentions": n, "surfaces": sorted(surfaces[s])} for s, n in ranked]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="Path to claude.ai export directory")
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    src_dir = Path(args.src).expanduser().resolve()
    conv_path = src_dir / "conversations.json"
    if not conv_path.exists():
        print(f"ERROR: {conv_path} not found", file=sys.stderr)
        return 1

    if args.clean and STAGING.exists():
        for p in STAGING.glob("*.json"):
            p.unlink()
    STAGING.mkdir(parents=True, exist_ok=True)

    data = json.load(conv_path.open())
    print(f"loaded {len(data)} raw conversations from {conv_path.name}")

    aliases = list_existing_aliases()
    slugs = list_existing_slugs()

    # Skip conversations already in the ledger so we don't re-stage what the
    # apply step (orchestrate_digest) just deleted.
    ledger_path = VAULT / "_system" / "ingestion" / "processed.json"
    ledger_keys: set[str] = set()
    if ledger_path.exists():
        try:
            ledger_keys = set(json.loads(ledger_path.read_text(encoding="utf-8")).get("entries", {}).keys())
        except Exception:
            pass

    staged = 0
    skipped = 0
    ledgered = 0
    oversize_threshold = 500_000

    for c in data:
        if args.limit and staged >= args.limit:
            break
        unit = parse_conversation(c)
        if not unit:
            skipped += 1
            continue
        if f"claude-web:{unit['source_id'][:8]}" in ledger_keys:
            ledgered += 1
            continue
        if unit["total_chars"] > oversize_threshold:
            unit["oversized"] = True
        unit["candidate_entities"] = pre_grep_entities(unit, aliases, slugs)
        (STAGING / f"{unit['source_id']}.json").write_text(
            json.dumps(unit, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        staged += 1

    # Memories.json — extract the conversations_memory text
    mem_path = src_dir / "memories.json"
    if mem_path.exists():
        memories = json.load(mem_path.open())
        if isinstance(memories, list) and memories:
            text = memories[0].get("conversations_memory", "")
            (STAGING / "_memories.txt").write_text(text, encoding="utf-8")
            print(f"memories: {len(text)} chars → staging/claude-web/_memories.txt")

    # Projects subdir — small project descriptions
    projects_dir = src_dir / "projects"
    if projects_dir.exists():
        proj_records = []
        for pp in sorted(projects_dir.glob("*.json")):
            d = json.load(pp.open())
            proj_records.append({
                "uuid": d.get("uuid"),
                "name": d.get("name"),
                "description": d.get("description"),
                "is_starter_project": d.get("is_starter_project"),
                "created_at": d.get("created_at"),
                "updated_at": d.get("updated_at"),
            })
        (STAGING / "_projects.json").write_text(
            json.dumps(proj_records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"projects: {len(proj_records)} → staging/claude-web/_projects.json")

    print(f"\nstaged={staged} skipped={skipped} already_ledgered={ledgered} oversized={sum(1 for p in STAGING.glob('*.json') if not p.name.startswith('_') and json.loads(p.read_text()).get('oversized'))}")
    print(f"staging dir: _system/ingestion/staging/claude-web/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
