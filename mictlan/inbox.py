"""Drain the sink — the producer→consolidator path (dream-policy.md §4).

Producers (today: Hermes) drop one ``DreamProposal`` envelope per run into
``_system/ingestion/inbox/<agent>-<date>.json``. The consolidator (Claude Code
``/dream``) drains it here:

- validate each envelope (malformed / schema-invalid / unregistered agent → skip
  + log, never fatal);
- bridge **safe** appends (durable, non-guardrailed, target exists) into the
  analyzer's apply path — idempotent by the ``<!-- src:… -->`` marker, so the
  brief double-write window during Hermes' cutover can't double-apply;
- route everything else (missing target, guardrail hit, proposed nodes/links) to
  the human approval gate via ``mictlan.proposals.resolve_nodes``.

Pure functions here; the CLI (`python -m mictlan.inbox`) wires them to a vault.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from pydantic import ValidationError

from .analyzer import Append, GraphUpdate
from .proposals import ResolvedBacklog, SearchFn, resolve_nodes
from .schema import DreamProposal, SectionAppend

# Extract (source, shortid) from a "<!-- src:<source>:<shortid> -->" marker.
_SRC_MARKER_RE = re.compile(r"src:([^:\s]+):([^\s>]+)")

# A producer's envelope is external, LLM-authored input on a shared path. Cap the
# read so a malfunctioning/compromised producer can't OOM the consolidator.
MAX_ENVELOPE_BYTES = 1_000_000

# slug matches a guardrail: prefix-starts-with OR exact.
GuardrailFn = Callable[[str], bool]


@dataclass
class IngestResult:
    """Validated envelopes plus one human-readable line per skipped envelope."""

    proposals: list[DreamProposal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class DrainResult:
    """Everything one drain produced, for the caller to apply + journal."""

    graph_update: GraphUpdate
    held: list[SectionAppend] = field(default_factory=list)
    backlog: Optional[ResolvedBacklog] = None
    errors: list[str] = field(default_factory=list)


def load_envelopes(inbox: Path, agents: set[str]) -> IngestResult:
    """Read + validate every envelope in the sink. Never raises on bad input."""
    result = IngestResult()
    if not inbox.exists():
        return result
    for p in sorted(inbox.glob("*.json")):
        if p.name.startswith("_") or p.name.startswith("held-"):
            continue
        try:
            if p.stat().st_size > MAX_ENVELOPE_BYTES:
                result.errors.append(
                    f"{p.name}: exceeds {MAX_ENVELOPE_BYTES} bytes, skipped"
                )
                continue
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            result.errors.append(f"{p.name}: unreadable/malformed JSON ({e})")
            continue
        try:
            prop = DreamProposal.model_validate(raw)
        except ValidationError as e:
            result.errors.append(f"{p.name}: schema-invalid ({e.error_count()} error(s))")
            continue
        if prop.agent not in agents:
            result.errors.append(f"{p.name}: unregistered agent {prop.agent!r}")
            continue
        result.proposals.append(prop)
    return result


def _split_marker(marker: str, fallback_source: str, fallback_id: str) -> tuple[str, str]:
    m = _SRC_MARKER_RE.search(marker or "")
    if m:
        return m.group(1), m.group(2)
    return fallback_source, fallback_id


def bridge_safe_appends(
    prop: DreamProposal,
    existing_slugs: set[str],
    is_guardrailed: GuardrailFn,
) -> tuple[list[Append], list[SectionAppend]]:
    """Split a producer's appends into (auto-applyable, held-for-review).

    Safe iff durable, no guardrail (envelope flag OR policy match), and the target
    note already exists. Everything else is held so the human decides.
    """
    applied: list[Append] = []
    held: list[SectionAppend] = []
    fallback_source = prop.agent.lower().replace(" ", "-")
    for a in prop.appends:
        safe = (
            a.durable
            and not a.guardrail_hit
            and not is_guardrailed(a.target_slug)
            and a.target_slug in existing_slugs
        )
        if not safe:
            held.append(a)
            continue
        # When the marker has no parseable src:X:Y, fall back to a content hash
        # (not the bare date) so two distinct marker-less appends to the same note
        # on the same day don't collide on the idempotency key and drop one.
        content_hash = hashlib.sha1(a.content.encode("utf-8")).hexdigest()[:8]
        source, source_id = _split_marker(a.source_marker, fallback_source, content_hash)
        applied.append(
            Append(
                target_slug=a.target_slug,
                section_date=a.section_date.isoformat(),
                content=a.content,
                source=source,
                source_id=source_id,
            )
        )
    return applied, held


def drain(
    inbox: Path,
    agents: set[str],
    existing_slugs: set[str],
    is_guardrailed: GuardrailFn,
    search: Optional[SearchFn] = None,
) -> DrainResult:
    """Validate + bridge + resolve every envelope in the sink into one result."""
    ingest = load_envelopes(inbox, agents)
    all_appends: list[Append] = []
    held: list[SectionAppend] = []
    for prop in ingest.proposals:
        applied, prop_held = bridge_safe_appends(prop, existing_slugs, is_guardrailed)
        all_appends.extend(applied)
        held.extend(prop_held)
    backlog = resolve_nodes(ingest.proposals, existing_slugs, search=search)
    return DrainResult(
        graph_update=GraphUpdate(appends=all_appends),
        held=held,
        backlog=backlog,
        errors=ingest.errors,
    )


# ---------- CLI ----------


def _persist_review(inbox: Path, target_date: str, result: DrainResult) -> Optional[str]:
    """Write the FULL held appends + node backlog to ``held-<date>.json`` so the
    approval gate (/dream Step 6.5) has content to act on — not just counts.

    Returns the relative filename written, or None when there's nothing to review.
    """
    if not result.held and not (result.backlog and (result.backlog.create or result.backlog.review)):
        return None
    payload = {
        "target_date": target_date,
        "held_appends": [a.model_dump(mode="json") for a in result.held],
        "backlog": {
            "create": [n.model_dump(mode="json") for n in (result.backlog.create if result.backlog else [])],
            "fold": [n.model_dump(mode="json") for n in (result.backlog.fold if result.backlog else [])],
            "review": [n.model_dump(mode="json") for n in (result.backlog.review if result.backlog else [])],
        },
    }
    dest = inbox / f"held-{target_date}.json"
    inbox.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return dest.name


def _main(argv: list[str] | None = None) -> int:
    from datetime import date

    import mictlan.analyzer as analyzer
    from mictlan.paths import VAULT
    from mictlan.policy import PolicyUnavailable, load_policy

    parser = argparse.ArgumentParser(description="Drain the dream sink into the vault.")
    parser.add_argument("--vault", type=Path, default=VAULT)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Apply safe appends. Without it, dry-run (report only).",
    )
    args = parser.parse_args(argv)

    vault = args.vault.resolve()
    try:
        pol = load_policy()
    except PolicyUnavailable as e:
        print(json.dumps({"ok": False, "error": f"policy_unavailable: {e}"}))
        return 1

    # Bind analyzer to this vault up front (same helper tests/orchestrate use) so
    # both list_existing_slugs() and apply() operate on the right vault.
    analyzer.bind_vault(vault)
    inbox = vault / pol.sink.strip("/")
    today = date.today().isoformat()
    result = drain(
        inbox=inbox,
        agents=set(pol.agents),
        existing_slugs=analyzer.list_existing_slugs(),
        is_guardrailed=pol.is_guardrailed,
        search=None,  # headless: no semantic backend → nodes go to review, never auto-create
    )

    summary = {
        "envelopes_errors": result.errors,
        "safe_appends": len(result.graph_update.appends),
        "held_for_review": len(result.held),
        "backlog": result.backlog.summary() if result.backlog else {},
    }

    if not args.confirm:
        summary["mode"] = "dry-run"
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    report = analyzer.apply(result.graph_update, today=today)
    review_file = _persist_review(inbox, today, result)
    summary["mode"] = "applied"
    summary["appended"] = report.appended
    summary["skipped_idempotent"] = report.skipped_idempotent
    summary["review_file"] = review_file  # held appends + node backlog for Step 6.5
    summary["apply_errors"] = [e for e in report.errors if not e.startswith("skipped:")]
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if summary["apply_errors"] else 0


if __name__ == "__main__":
    sys.exit(_main())
