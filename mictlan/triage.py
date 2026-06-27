#!/usr/bin/env python3
"""Triage conversation notes by theme.

Reads every file in `conversations/`, extracts frontmatter tags + metadata,
clusters by tag, cross-references against existing topic notes in `notes/`,
and writes a markdown report to `_system/ingestion/conversation-triage-<date>.md`.

Pure stdlib + PyYAML. Run via `uv run` for consistency with team standards.

    uv run --with pyyaml _system/scripts/triage_conversations.py

Output sections:
- Summary stats
- Tag frequency (top 50)
- Clusters with existing topic note (suggest: append H2 to existing note)
- Clusters needing new topic note (suggest: draft new topic note)
- Low-signal conversations (1 tag or tiny body; archive candidates)
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("missing dep: yaml. run with `uv run --with pyyaml`", file=sys.stderr)
    sys.exit(1)


from mictlan.paths import VAULT
CONVERSATIONS = VAULT / "conversations"
NOTES = VAULT / "notes"
INGESTION = VAULT / "_system" / "ingestion"

# Tags too generic to suggest a topic note. They cluster too many distinct themes.
LOW_INFO_TAGS = {
    "personal",
    "work",
    "unknown",
    "general",
    "misc",
    "claude",
    "ai",
    "tools",
}

# Minimum cluster size to surface as a candidate topic.
MIN_CLUSTER_SIZE = 3

# Body length (chars) below which a conversation is considered ephemeral.
EPHEMERAL_BODY_THRESHOLD = 500


def parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body_text)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    fm_raw = text[4:end]
    body = text[end + 5 :]
    try:
        fm = yaml.safe_load(fm_raw) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, body


def list_existing_topic_slugs() -> set[str]:
    """Slug → True for every note in notes/ regardless of type (so we can
    detect ANY existing note that might cover a tag, not just topics)."""
    return {p.stem for p in NOTES.glob("*.md")}


def aliases_index() -> dict[str, str]:
    """Map lowercase alias → canonical slug for all notes/."""
    out: dict[str, str] = {}
    for p in NOTES.glob("*.md"):
        fm, _ = parse_frontmatter(p)
        for alias in fm.get("aliases") or []:
            out[str(alias).lower()] = p.stem
    return out


def find_topic_note_for_tag(
    tag: str, existing_slugs: set[str], aliases: dict[str, str]
) -> str | None:
    """Return a slug if any existing note plausibly covers this tag."""
    if tag in existing_slugs:
        return tag
    if tag.lower() in aliases:
        return aliases[tag.lower()]
    # Try common transforms: kebab plural→singular, underscore→dash
    candidates = {
        tag.rstrip("s"),
        tag.replace("_", "-"),
        tag.replace("-", "_"),
    }
    for cand in candidates:
        if cand in existing_slugs:
            return cand
    return None


def main() -> int:
    if not CONVERSATIONS.exists():
        print(f"missing dir: {CONVERSATIONS}", file=sys.stderr)
        return 1

    existing_slugs = list_existing_topic_slugs()
    aliases = aliases_index()

    tag_counts: Counter[str] = Counter()
    tag_to_convs: dict[str, list[tuple[str, str]]] = defaultdict(list)
    ephemeral: list[tuple[str, int, list[str]]] = []  # (id, body_len, tags)
    untagged: list[str] = []
    body_lengths: list[int] = []
    by_source: Counter[str] = Counter()
    by_year: Counter[str] = Counter()

    convs = sorted(CONVERSATIONS.glob("*.md"))
    for p in convs:
        fm, body = parse_frontmatter(p)
        tags = [str(t).lower() for t in (fm.get("tags") or [])]
        body_len = len(body.strip())
        body_lengths.append(body_len)
        conv_id = fm.get("id", p.stem)
        source = fm.get("source", "unknown")
        by_source[source] += 1
        created = str(fm.get("created", ""))[:4]
        if created:
            by_year[created] += 1

        for t in tags:
            tag_counts[t] += 1
            tag_to_convs[t].append((conv_id, str(fm.get("created", ""))))

        non_low_info_tags = [t for t in tags if t not in LOW_INFO_TAGS]
        if not non_low_info_tags:
            if not tags:
                untagged.append(conv_id)
            elif body_len < EPHEMERAL_BODY_THRESHOLD:
                ephemeral.append((conv_id, body_len, tags))

    today = date.today().isoformat()
    report_path = INGESTION / f"conversation-triage-{today}.md"
    INGESTION.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append(f"# Conversation triage — {today}")
    lines.append("")
    lines.append(f"Scanned **{len(convs)}** conversation files.")
    lines.append("")
    lines.append("## Stats")
    lines.append("")
    lines.append(f"- Total conversations: {len(convs)}")
    lines.append(
        f"- Mean body length: {sum(body_lengths) // max(len(body_lengths), 1)} chars"
    )
    lines.append(f"- Untagged: {len(untagged)}")
    lines.append(f"- Ephemeral (< {EPHEMERAL_BODY_THRESHOLD} chars + only low-info tags): {len(ephemeral)}")
    lines.append("")
    lines.append("### By source")
    for src, cnt in by_source.most_common():
        lines.append(f"- `{src}`: {cnt}")
    lines.append("")
    lines.append("### By year")
    for yr, cnt in sorted(by_year.items()):
        lines.append(f"- {yr}: {cnt}")
    lines.append("")

    lines.append("## Tag frequency (top 50, excluding low-info)")
    lines.append("")
    lines.append("| tag | count | maps to existing note? |")
    lines.append("|---|---:|---|")
    informative_tags = [
        (t, c) for t, c in tag_counts.most_common() if t not in LOW_INFO_TAGS
    ][:50]
    for tag, cnt in informative_tags:
        slug = find_topic_note_for_tag(tag, existing_slugs, aliases)
        target = f"`[[{slug}]]`" if slug else "—"
        lines.append(f"| `{tag}` | {cnt} | {target} |")
    lines.append("")

    lines.append("## Clusters with existing topic/entity note")
    lines.append("")
    lines.append(
        "Conversations tagged with these labels already have a corresponding "
        "note in `notes/`. Suggested action: append dated H2 entries to the "
        "existing note for any conversation not yet cited there."
    )
    lines.append("")
    covered: list[tuple[str, int, str]] = []
    for tag, cnt in informative_tags:
        if cnt < MIN_CLUSTER_SIZE:
            continue
        slug = find_topic_note_for_tag(tag, existing_slugs, aliases)
        if slug:
            covered.append((tag, cnt, slug))
    for tag, cnt, slug in covered:
        lines.append(f"### `{tag}` → [[{slug}]] ({cnt} conversations)")
        lines.append("")
        for conv_id, created in sorted(
            tag_to_convs[tag], key=lambda x: x[1], reverse=True
        )[:15]:
            lines.append(f"- `{created}` [[{conv_id}]]")
        if len(tag_to_convs[tag]) > 15:
            lines.append(f"- … and {len(tag_to_convs[tag]) - 15} more")
        lines.append("")

    lines.append("## Clusters needing a new topic note")
    lines.append("")
    lines.append(
        f"Tags with ≥ {MIN_CLUSTER_SIZE} conversations but no matching note "
        "in `notes/`. Suggested action: create a new `type: topic` note."
    )
    lines.append("")
    uncovered: list[tuple[str, int]] = []
    for tag, cnt in informative_tags:
        if cnt < MIN_CLUSTER_SIZE:
            continue
        if not find_topic_note_for_tag(tag, existing_slugs, aliases):
            uncovered.append((tag, cnt))
    for tag, cnt in uncovered:
        lines.append(f"### `{tag}` ({cnt} conversations) — no topic note")
        lines.append("")
        for conv_id, created in sorted(
            tag_to_convs[tag], key=lambda x: x[1], reverse=True
        )[:15]:
            lines.append(f"- `{created}` [[{conv_id}]]")
        if len(tag_to_convs[tag]) > 15:
            lines.append(f"- … and {len(tag_to_convs[tag]) - 15} more")
        lines.append("")

    lines.append("## Low-signal conversations (archive candidates)")
    lines.append("")
    lines.append(
        f"Conversations with only low-information tags AND body < "
        f"{EPHEMERAL_BODY_THRESHOLD} chars. Likely safe to mark "
        "`status: archived` without extraction."
    )
    lines.append("")
    lines.append(f"Total: **{len(ephemeral)}**")
    lines.append("")
    for conv_id, body_len, tags in ephemeral[:50]:
        lines.append(f"- `{body_len}` chars · tags: `{tags}` · [[{conv_id}]]")
    if len(ephemeral) > 50:
        lines.append(f"- … and {len(ephemeral) - 50} more")
    lines.append("")

    if untagged:
        lines.append("## Untagged conversations")
        lines.append("")
        lines.append(
            f"**{len(untagged)}** conversations have no tags at all. These "
            "are unclassifiable until tags are added. Likely caused by an "
            "ingest script that didn't infer tags."
        )
        lines.append("")
        for conv_id in untagged[:30]:
            lines.append(f"- [[{conv_id}]]")
        if len(untagged) > 30:
            lines.append(f"- … and {len(untagged) - 30} more")
        lines.append("")

    lines.append("## Recommended next actions")
    lines.append("")
    lines.append(
        "1. **Existing-topic appends**: for each cluster above with a "
        "matching topic note, scan the listed conversations and append a "
        "dated H2 to the topic note for any that contributed durable "
        "protocol/decision content not already cited there."
    )
    lines.append(
        "2. **New topic notes**: for each `uncovered` cluster with ≥ "
        f"{MIN_CLUSTER_SIZE} conversations, evaluate whether to create a "
        "new `type: topic` note. Some uncovered clusters may be sub-themes "
        "of an existing note (e.g. `back-pain` might fit under [[health]] "
        "rather than its own note)."
    )
    lines.append(
        "3. **Archive sweep**: review the low-signal list. If accurate, "
        "bulk-set `status: archived` on those files. Conversations stay "
        "as raw record but exit the active retrieval set."
    )
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {report_path}")
    print(f"  {len(convs)} conversations scanned")
    print(f"  {len(covered)} clusters with existing notes")
    print(f"  {len(uncovered)} clusters needing new notes")
    print(f"  {len(ephemeral)} archive candidates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
