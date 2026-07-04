"""Helpers shared by every stager, regardless of source."""

from __future__ import annotations

import re


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
