"""The semantic entity-resolution + approval gate.

This is the single fan-in point: it takes ``DreamProposal``s from every agent and
reconciles their ``proposed_nodes`` against the existing graph BEFORE any node is
created. It is the highest-leverage enhancement over the old keyword-only linking
(dream-cycle REM pass "v1 limitation").

It does NOT create notes. It produces a reconciled, deduped backlog for the human
approval gate (Claude Code /dream Step 6.5 — the single cross-agent approval gate
per dream-policy.md §5).

Dedup strategy, cheapest first:
  1. exact slug / alias match  (keyword — fast, certain)
  2. semantic match           (brain-MCP search_semantic — catches aliases that
                               keyword match misses; this is the Cognee lesson)

The semantic backend is injected (``SearchFn``) so this works in both contexts:
- MCP agents pass a thin wrapper over ``mcp__brain__search_semantic``.
- headless/cron pass a wrapper over the brain_mcp package or its HTTP endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .schema import DreamProposal, NodeProposal

# A search backend returns (slug, score) hits for a free-text query, best first.
SearchFn = Callable[[str, int], list[tuple[str, float]]]

# Above this cosine-ish score we treat a semantic hit as the same entity.
DEFAULT_SEMANTIC_THRESHOLD = 0.82


@dataclass
class ResolvedBacklog:
    """The reconciled output handed to the human approval gate."""

    create: list[NodeProposal] = field(default_factory=list)  # genuinely new
    fold: list[NodeProposal] = field(default_factory=list)     # dedups to existing
    # proposals whose resolution is uncertain — surfaced for the human to judge
    review: list[NodeProposal] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "create": len(self.create),
            "fold": len(self.fold),
            "review": len(self.review),
        }


def _exact_slug_match(node: NodeProposal, existing_slugs: set[str]) -> Optional[str]:
    candidates = {
        node.slug,
        node.slug.replace("-", ""),
    }
    for c in candidates:
        if c in existing_slugs:
            return c
    return None


def resolve_nodes(
    proposals: Iterable[DreamProposal],
    existing_slugs: set[str],
    search: Optional[SearchFn] = None,
    threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
) -> ResolvedBacklog:
    """Reconcile proposed nodes from ALL agents into one deduped backlog.

    Args:
        proposals: DreamProposal from each dreamer this cycle.
        existing_slugs: every note slug currently in the vault.
        search: semantic backend (brain-MCP). If None, only keyword dedup runs
            and uncertain cases go to ``review`` rather than silently creating.
        threshold: semantic score above which a hit is treated as the same entity.
    """
    backlog = ResolvedBacklog()

    # Merge identical proposals across agents first (same slug seen by two dreamers).
    merged: dict[str, NodeProposal] = {}
    for p in proposals:
        for node in p.proposed_nodes:
            if node.slug in merged:
                merged[node.slug].times_seen += node.times_seen
            else:
                merged[node.slug] = node.model_copy(deep=True)

    for node in merged.values():
        hit = _exact_slug_match(node, existing_slugs)
        if hit:
            node.resolves_to_existing = hit
            node.resolution_confidence = "high"
            backlog.fold.append(node)
            continue

        if search is None:
            backlog.review.append(node)
            continue

        results = search(f"{node.name} {node.suggested_summary}".strip(), 3)
        if results and results[0][1] >= threshold:
            node.resolves_to_existing = results[0][0]
            node.resolution_confidence = "medium"
            backlog.fold.append(node)
        elif results and results[0][1] >= threshold - 0.1:
            # close but not certain — let the human decide
            node.resolves_to_existing = results[0][0]
            node.resolution_confidence = "low"
            backlog.review.append(node)
        else:
            backlog.create.append(node)

    return backlog
