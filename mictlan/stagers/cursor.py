#!/usr/bin/env python3
"""Stage Cursor agent .jsonl transcripts into clean ConversationUnit JSON files.

Parses ~/.cursor/projects/*/agent-transcripts/*/*.jsonl (parent sessions only),
extracts user + assistant text turns, filters trivial sessions, pre-greps
candidate vault entities, and writes one JSON file per session under
_system/ingestion/staging/cursor-code/.

Same downstream pipeline as Claude Code: orchestrate_digest prepare → subagent
→ apply. No LLM calls and no vault writes here — pure parsing.

Usage:
    uv run --with pyyaml _system/scripts/stage_cursor.py
    uv run --with pyyaml _system/scripts/stage_cursor.py --since 2026-05-01 --limit 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from mictlan.analyzer import list_existing_aliases, list_existing_slugs
from mictlan.stagers.claude_code import classify_mode, count_actions, pre_grep_entities

PROJECTS_ROOT = Path.home() / ".cursor" / "projects"
from mictlan.paths import VAULT
STAGING = VAULT / "_system" / "ingestion" / "staging" / "cursor-code"

USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
AGENT_SKILLS_RE = re.compile(r"<agent_skills>.*?</agent_skills>", re.DOTALL)
MCP_FILESYSTEM_RE = re.compile(r"<mcp_file_system>.*?</mcp_file_system>", re.DOTALL)
TOOL_RESULT_RE = re.compile(r"^\[\{'tool_use_id'")

TRIVIAL_TOKENS = {"exit", "quit", "clear", "ok", "yes", "no", "thanks", "thank you"}
MIN_TURNS = 4
MIN_CHARS_TOTAL = 200


def infer_cwd(project_slug: str) -> str | None:
    """Best-effort reverse of Cursor's workspace slug → filesystem path."""
    if project_slug == "empty-window":
        return str(Path.home())

    tail = project_slug
    base = Path.home()
    if project_slug.startswith("Users-skalas-"):
        tail = project_slug[len("Users-skalas-") :]
    elif project_slug.startswith("Users-"):
        rest = project_slug[len("Users-") :]
        user, _, tail = rest.partition("-")
        if user:
            base = Path("/Users") / user

    if not tail:
        return str(base)

    segments = tail.split("-")
    for split_at in range(len(segments), 0, -1):
        head = segments[: split_at - 1] if split_at > 1 else []
        last = "-".join(segments[split_at - 1 :])
        candidate = base.joinpath(*head, last) if head else base / last
        if candidate.exists():
            return str(candidate.resolve())

    fallback = base / tail.replace("-", "/")
    if fallback.exists():
        return str(fallback.resolve())
    return None


def extract_message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = (block.get("text") or "").strip()
            if t:
                parts.append(t)
    return "\n\n".join(parts).strip()


def clean_user_content(content: str) -> str:
    if not isinstance(content, str):
        content = str(content)
    if TOOL_RESULT_RE.match(content.strip()):
        return ""
    text = extract_message_text({"content": content}) if content.startswith("[{") else content
    m = USER_QUERY_RE.search(text)
    if m:
        text = m.group(1)
    text = SYSTEM_REMINDER_RE.sub("", text)
    text = AGENT_SKILLS_RE.sub("", text)
    text = MCP_FILESYSTEM_RE.sub("", text)
    return text.strip()


def extract_assistant_text(message: dict) -> str:
    text = extract_message_text(message)
    text = SYSTEM_REMINDER_RE.sub("", text)
    return text.strip()


def iso_from_mtime(path: Path, use_ctime: bool = False) -> str:
    ts = path.stat().st_ctime if use_ctime else path.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def discover_cursor_jsonl() -> list[Path]:
    files: list[Path] = []
    if not PROJECTS_ROOT.exists():
        return files
    for transcripts_dir in PROJECTS_ROOT.glob("*/agent-transcripts"):
        for session_dir in transcripts_dir.iterdir():
            if not session_dir.is_dir() or session_dir.name == "subagents":
                continue
            jsonl = session_dir / f"{session_dir.name}.jsonl"
            if jsonl.is_file():
                files.append(jsonl)
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def parse_session(path: Path, project_slug: str) -> dict | None:
    turns: list[dict] = []
    session_id = path.stem
    cwd = infer_cwd(project_slug)
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
            role = ev.get("role")
            if role not in ("user", "assistant"):
                continue
            msg = ev.get("message") or {}
            if role == "user":
                content = clean_user_content(extract_message_text(msg) or msg.get("content") or "")
                if not content or content.lower() in TRIVIAL_TOKENS:
                    continue
                if len(content) < 4:
                    continue
                turns.append({"role": "user", "content": content})
            else:
                mut, other = count_actions(msg)
                file_mutations += mut
                other_tool_calls += other
                content = extract_assistant_text(msg)
                if not content:
                    continue
                turns.append({"role": "assistant", "content": content})

    if len(turns) < MIN_TURNS:
        return None
    total_chars = sum(len(t["content"]) for t in turns)
    if total_chars < MIN_CHARS_TOTAL:
        return None
    user_turns = sum(1 for t in turns if t["role"] == "user")
    user_chars = sum(len(t["content"]) for t in turns if t["role"] == "user")

    title = next((t["content"][:120].replace("\n", " ") for t in turns if t["role"] == "user"), "")
    started_at = iso_from_mtime(path, use_ctime=True)
    ended_at = iso_from_mtime(path, use_ctime=False)

    return {
        "source": "cursor-jsonl",
        "source_id": session_id,
        "session_id": session_id,
        "cwd": cwd,
        "project_slug": project_slug,
        "started_at": started_at,
        "ended_at": ended_at,
        "title": title,
        "turn_count": len(turns),
        "total_chars": total_chars,
        "file_mutations": file_mutations,
        "tool_calls": other_tool_calls,
        "mode": classify_mode(file_mutations, user_turns, user_chars),
        "turns": turns,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO date — skip sessions ended before this", default=None)
    ap.add_argument("--limit", type=int, default=None, help="Only stage the N newest sessions")
    ap.add_argument("--clean", action="store_true", help="Wipe staging/cursor-code before run")
    args = ap.parse_args()

    if args.clean and STAGING.exists():
        for p in STAGING.glob("*.json"):
            p.unlink()
    STAGING.mkdir(parents=True, exist_ok=True)

    aliases = list_existing_aliases()
    slugs = list_existing_slugs()

    import mictlan.ledger as _ledger
    ledger_keys: set[str] = _ledger.ledger_keys(VAULT)

    def _ledger_short(source_id: str) -> str:
        return source_id[:8]

    staged = 0
    skipped = 0
    ledgered = 0
    total = 0
    since_dt = args.since

    for path in discover_cursor_jsonl():
        total += 1
        if args.limit and staged >= args.limit:
            break
        project_slug = path.parents[2].name if len(path.parents) >= 3 else "unknown"
        unit = parse_session(path, project_slug)
        if not unit:
            skipped += 1
            continue
        if since_dt and unit.get("ended_at"):
            if unit["ended_at"][: len(since_dt)] < since_dt:
                skipped += 1
                continue
        if f"cursor-jsonl:{_ledger_short(unit['source_id'])}" in ledger_keys:
            ledgered += 1
            continue
        unit["candidate_entities"] = pre_grep_entities(unit, aliases, slugs)
        out_path = STAGING / f"{unit['source_id']}.json"
        out_path.write_text(json.dumps(unit, ensure_ascii=False, indent=2), encoding="utf-8")
        staged += 1

    print(f"scanned={total} staged={staged} skipped={skipped} already_ledgered={ledgered}")
    print("staging dir: _system/ingestion/staging/cursor-code/")
    print(
        "with entities: "
        f"{sum(1 for p in STAGING.glob('*.json') if json.loads(p.read_text()).get('candidate_entities'))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
