#!/usr/bin/env python3
"""Stage Claude Code .jsonl transcripts into clean ConversationUnit JSON files.

Parses ~/.claude/projects/*/*.jsonl, extracts user + assistant text turns,
filters trivial sessions, pre-greps candidate vault entities, and writes one
JSON file per session under _system/ingestion/staging/claude-code/.

The orchestrator (this Claude Code session) reads these staged files and
dispatches subagents to analyze + apply graph updates. This script does no
LLM calls and no vault writes — it's pure parsing.

Usage:
    uv run --with pyyaml _system/scripts/stage_claude_code.py
    uv run --with pyyaml _system/scripts/stage_claude_code.py --since 2026-05-01 --limit 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mictlan.analyzer import list_existing_aliases, list_existing_slugs

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
from mictlan.paths import VAULT
STAGING = VAULT / "_system" / "ingestion" / "staging" / "claude-code"

# Patterns to strip from user text (slash-command output, system reminders)
LOCAL_CMD_RE = re.compile(r"<local-command-(?:stdout|caveat|stderr)>.*?</local-command-(?:stdout|caveat|stderr)>", re.DOTALL)
SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
COMMAND_TAG_RE = re.compile(r"<command-(?:name|message|args)>.*?</command-(?:name|message|args)>", re.DOTALL)

TRIVIAL_TOKENS = {"exit", "quit", "clear", "ok", "yes", "no", "thanks", "thank you", "/clear", "/help"}
MIN_TURNS = 4
MIN_CHARS_TOTAL = 200

# --- Session-mode classification (see _system/dream-policy.md §3) -------------
# A coding session's durable value is the DECISION ("what we did"), not the
# transcript — EXCEPT discussion-heavy sessions, where the reasoning IS the value.
# We derive the mode from signal the stager already reads (tool-use density) and
# tag it so the digest can branch: execution → extract the decision only (or
# archive); discussion/mixed → full conversational digest.
FILE_MUTATION_TOOLS = {
    "Edit", "Write", "MultiEdit", "NotebookEdit",          # Claude Code
    "create_file", "edit_file", "search_replace", "apply_patch",  # Cursor variants
}
# "execution" requires file changes AND low human dialogue. Prose length is NOT a
# spine — a long execution session narrates a lot too. The cost is asymmetric:
# mislabeling a rich discussion as execution LOSES reasoning, while the reverse
# only adds minor noise — so we only call "execution" when confident it was a
# low-dialogue grind, and default everything else to a full digest.
EXECUTION_MAX_USER_TURNS = 3      # a grind = task in, agent works, few human turns
EXECUTION_MAX_USER_CHARS = 1500   # ...and little human prose


def count_actions(message: dict) -> tuple[int, int]:
    """Return (file_mutations, other_tool_calls) for one assistant message."""
    content = message.get("content")
    if not isinstance(content, list):
        return 0, 0
    mut = other = 0
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in ("tool_use", "tool_call"):
            continue
        name = block.get("name") or block.get("toolName") or ""
        if name in FILE_MUTATION_TOOLS:
            mut += 1
        else:
            other += 1
    return mut, other


def classify_mode(file_mutations: int, user_turns: int, user_chars: int) -> str:
    """discussion = nothing built (talk/design/review); execution = built with
    little dialogue; mixed = built AND discussed. Bias: when unsure, not execution."""
    if file_mutations == 0:
        return "discussion"
    if user_turns <= EXECUTION_MAX_USER_TURNS and user_chars < EXECUTION_MAX_USER_CHARS:
        return "execution"
    return "mixed"


def clean_user_content(content: str) -> str:
    """Strip slash-command stdout, system reminders, command tags — keep only real user text."""
    if not isinstance(content, str):
        content = str(content)
    text = LOCAL_CMD_RE.sub("", content)
    text = SYSTEM_REMINDER_RE.sub("", text)
    text = COMMAND_TAG_RE.sub("", text)
    return text.strip()


def extract_assistant_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text") or ""
            if t.strip():
                parts.append(t)
    return "\n\n".join(parts).strip()


def parse_session(path: Path) -> dict | None:
    turns: list[dict] = []
    cwd = None
    session_id = path.stem
    first_ts = None
    last_ts = None
    file_mutations = 0
    other_tool_calls = 0

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype not in ("user", "assistant"):
                continue
            msg = ev.get("message") or {}
            ts = ev.get("timestamp")
            cwd = cwd or ev.get("cwd")
            if etype == "user":
                content = clean_user_content(msg.get("content") or "")
                # Skip empty or trivial user messages
                if not content or content.lower() in TRIVIAL_TOKENS:
                    continue
                if len(content) < 4:
                    continue
                turns.append({"role": "user", "content": content, "timestamp": ts})
            else:  # assistant
                mut, other = count_actions(msg)
                file_mutations += mut
                other_tool_calls += other
                content = extract_assistant_text(msg)
                if not content:
                    continue
                turns.append({"role": "assistant", "content": content, "timestamp": ts})
            if ts:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts

    if len(turns) < MIN_TURNS:
        return None
    total_chars = sum(len(t["content"]) for t in turns)
    if total_chars < MIN_CHARS_TOTAL:
        return None
    user_turns = sum(1 for t in turns if t["role"] == "user")
    user_chars = sum(len(t["content"]) for t in turns if t["role"] == "user")

    title = next((t["content"][:120].replace("\n", " ") for t in turns if t["role"] == "user"), "")
    return {
        "source": "claude-jsonl",
        "source_id": session_id,
        "session_id": session_id,
        "cwd": cwd,
        "started_at": first_ts,
        "ended_at": last_ts,
        "title": title,
        "turn_count": len(turns),
        "total_chars": total_chars,
        "file_mutations": file_mutations,
        "tool_calls": other_tool_calls,
        "mode": classify_mode(file_mutations, user_turns, user_chars),
        "turns": turns,
    }


def pre_grep_entities(unit: dict, aliases: dict[str, str], slugs: set[str]) -> list[dict]:
    """Find entity slugs mentioned in the conversation with word-boundary matching.

    Returns a list of {slug, mentions, surfaces} dicts ranked by mention count,
    capped at 25 — the subagent shouldn't be drowned in noise.
    """
    blob = "\n".join(t["content"] for t in unit["turns"]).lower()
    counts: dict[str, int] = {}
    surfaces: dict[str, set[str]] = {}

    def count_surface(slug: str, surface: str) -> None:
        # Word-boundary match against the surface form
        pattern = r"\b" + re.escape(surface.lower()) + r"\b"
        n = len(re.findall(pattern, blob))
        if n > 0:
            counts[slug] = counts.get(slug, 0) + n
            surfaces.setdefault(slug, set()).add(surface)

    # Match aliases (high-precision: these are explicit human-readable names)
    for alias, slug in aliases.items():
        if not alias or len(alias) < 4:
            continue
        count_surface(slug, alias)

    # Match slugs themselves (kebab and spaced)
    for slug in slugs:
        if len(slug) < 4:
            continue
        count_surface(slug, slug)
        count_surface(slug, slug.replace("-", " "))

    # Require at least 2 mentions to count as a candidate (filters out coincidental substrings)
    ranked = sorted(
        ((s, n) for s, n in counts.items() if n >= 2),
        key=lambda x: (-x[1], x[0]),
    )[:25]
    return [
        {"slug": s, "mentions": n, "surfaces": sorted(surfaces[s])}
        for s, n in ranked
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO date — skip sessions ended before this", default=None)
    ap.add_argument("--limit", type=int, default=None, help="Only stage the N newest sessions")
    ap.add_argument("--clean", action="store_true", help="Wipe staging/claude-code before run")
    args = ap.parse_args()

    if args.clean and STAGING.exists():
        for p in STAGING.glob("*.json"):
            p.unlink()
    STAGING.mkdir(parents=True, exist_ok=True)

    files = sorted(PROJECTS_ROOT.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    aliases = list_existing_aliases()
    slugs = list_existing_slugs()

    # Load the ledger so we can skip sessions already processed/archived.
    # Without this, we re-stage every session in the window each run — defeats
    # the post-apply unlink in orchestrate_digest. Union of legacy file + shards.
    import ledger as _ledger
    ledger_keys: set[str] = _ledger.ledger_keys(VAULT)

    def _ledger_short(source_id: str) -> str:
        # Mirrors ledger_key() truncation: agent-* gets 10 hex after the prefix,
        # everything else gets 8 hex of the UUID.
        if source_id.startswith("agent-"):
            return source_id[:16]
        return source_id[:8]

    staged = 0
    skipped = 0
    ledgered = 0
    total = 0
    since_dt = args.since

    for path in files:
        total += 1
        if args.limit and staged >= args.limit:
            break
        unit = parse_session(path)
        if not unit:
            skipped += 1
            continue
        if since_dt and unit.get("ended_at"):
            if unit["ended_at"][: len(since_dt)] < since_dt:
                skipped += 1
                continue
        if f"claude-jsonl:{_ledger_short(unit['source_id'])}" in ledger_keys:
            ledgered += 1
            continue
        unit["candidate_entities"] = pre_grep_entities(unit, aliases, slugs)
        out_path = STAGING / f"{unit['source_id']}.json"
        out_path.write_text(json.dumps(unit, ensure_ascii=False, indent=2), encoding="utf-8")
        staged += 1

    print(f"scanned={total} staged={staged} skipped={skipped} already_ledgered={ledgered}")
    print(f"staging dir: _system/ingestion/staging/claude-code/")
    print(f"with entities: {sum(1 for p in STAGING.glob('*.json') if json.loads(p.read_text()).get('candidate_entities'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
