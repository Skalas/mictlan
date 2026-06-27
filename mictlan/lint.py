"""Lint filled digest proposals before they hit the apply step.

Runs deterministic checks that catch the failure modes a human reviewer
would normally catch by eyeballing each proposal:

  - target_slug points at a note that actually exists
  - every [[wikilink]] in content resolves to an existing slug
  - section_date matches conversation started_at (date prefix)
  - content carries at least one wikilink (per recipe)
  - the target note doesn't already have a section with this source hash
  - creates[].slug doesn't collide with an existing slug anywhere in the vault
  - conversation title doesn't match a guardrail keyword (wedding, legal,
    financial — these always need a human in the loop)

Severities:
  ERROR  — blocks apply.
  WARN   — prints but doesn't block (unless --strict).

Invoke standalone:

    uv run --with pyyaml _system/scripts/lint_proposals.py
    uv run --with pyyaml _system/scripts/lint_proposals.py --strict

Returns exit code 0 if clean, 1 if any ERRORs (or WARNs under --strict).
The orchestrator calls this between prepare and apply.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

VAULT_DEFAULT = Path(__file__).resolve().parents[2]

WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:\|[^\]]+)?(?:#[^\]]+)?\]\]")
SOURCE_HASH_RE = re.compile(r"<!--\s*src:([a-z0-9-]+):([a-z0-9]+)\s*-->")

# Conversation titles matching these keywords always require human review,
# even when the proposal looks clean. Spanish + English variants.
GUARDRAIL_KEYWORDS = {
    "wedding", "boda",
    "finiquito", "indemnización", "indemnizacion",
    "contrato", "contract",
    "legal", "abogado", "lawyer",
    "divorcio", "divorce",
    "herencia", "testamento", "inheritance",
    "salario", "salary",
    "demanda", "lawsuit",
    "hacienda", "sat", "irs",
    "psicologo", "psicólogo", "therapy", "terapia",
}


@dataclass
class Finding:
    severity: str  # "ERROR" | "WARN"
    key: str
    message: str

    def fmt(self) -> str:
        return f"  [{self.severity}] {self.key}: {self.message}"


def vault_slugs(vault: Path) -> set[str]:
    """All slugs reachable in the vault — notes, meetings, daily, conversations."""
    out: set[str] = set()
    for folder in ("notes", "meetings", "daily", "conversations"):
        d = vault / folder
        if not d.exists():
            continue
        for p in d.glob("*.md"):
            out.add(p.stem)
    return out


def note_body(vault: Path, slug: str) -> str | None:
    for folder in ("notes", "meetings", "daily", "conversations"):
        p = vault / folder / f"{slug}.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
    return None


def lint_proposal(
    key: str,
    entry: dict,
    payload: dict,
    slugs: set[str],
    vault: Path,
) -> list[Finding]:
    findings: list[Finding] = []
    title = (entry.get("title") or "").lower()
    started_at = entry.get("started_at") or ""
    started_date = started_at[:10] if len(started_at) >= 10 else ""

    # Guardrail keyword scan — title-level, WARN by default.
    hit = sorted(kw for kw in GUARDRAIL_KEYWORDS if kw in title)
    if hit:
        findings.append(Finding(
            "WARN", key,
            f"title matches guardrail keyword(s): {', '.join(hit)} — confirm human reviewed",
        ))

    # Skip proposals route entirely — nothing else to check.
    if isinstance(payload.get("skip_reason"), str) and payload["skip_reason"].strip():
        return findings

    appends = payload.get("appends") or []
    creates = payload.get("creates") or []

    # Slugs created within this same proposal resolve too — the apply step
    # writes creates and appends in one pass, so an append that links to a
    # note born in this proposal's creates[] is valid (per the recipe's
    # wikilink rule: "appears in your own creates[] block in this same
    # proposal"). Fold them into the resolvable set for this proposal only.
    local_slugs = slugs | {
        (c.get("slug") or "").strip()
        for c in creates
        if isinstance(c, dict) and (c.get("slug") or "").strip()
    }

    # Catch the "subagent invented its own schema" failure mode: skip_reason
    # is null and appends/creates are both empty, but the payload has extra
    # top-level keys (h2, body, target_slug, action, etc.) suggesting the
    # subagent emitted a flat structure instead of the canonical envelope.
    if not appends and not creates:
        known = {"skip_reason", "appends", "creates"}
        extras = sorted(k for k in payload.keys() if k not in known)
        if extras:
            findings.append(Finding(
                "ERROR", key,
                f"proposal has no appends/creates/skip_reason but carries unknown top-level keys {extras} — subagent likely invented a flat schema; rewrite as {{skip_reason, appends, creates}}",
            ))
        else:
            findings.append(Finding(
                "ERROR", key,
                "proposal is empty (no skip_reason, no appends, no creates) — nothing to apply",
            ))

    for i, a in enumerate(appends):
        loc = f"appends[{i}]"
        target = (a.get("target_slug") or "").strip()
        section_date = (a.get("section_date") or "").strip()
        content = a.get("content") or ""

        # target_slug must exist
        if target and target not in slugs:
            findings.append(Finding(
                "ERROR", key,
                f"{loc}.target_slug='{target}' has no matching note in vault",
            ))

        # section_date must match conversation date
        if started_date and section_date and section_date != started_date:
            findings.append(Finding(
                "WARN", key,
                f"{loc}.section_date={section_date} != conversation started_at={started_date}",
            ))

        # at least one wikilink (recipe rule)
        links = WIKILINK_RE.findall(content)
        if not links:
            findings.append(Finding(
                "WARN", key,
                f"{loc} has no [[wikilinks]] — recipe asks for ≥1",
            ))

        # every wikilink must resolve — against vault slugs OR a slug this
        # same proposal creates.
        for link in links:
            stem = link.strip()
            if stem and stem not in local_slugs:
                findings.append(Finding(
                    "ERROR", key,
                    f"{loc} wikilink [[{stem}]] does not resolve to any vault note",
                ))

        # idempotency: target body must not already carry this source hash
        body = note_body(vault, target) if target else None
        if body is not None:
            sid_match = re.match(r"([a-z0-9-]+):([a-z0-9]+)$", key)
            if sid_match:
                source, sid = sid_match.group(1), sid_match.group(2)
                if f"src:{source}:{sid}" in body:
                    findings.append(Finding(
                        "ERROR", key,
                        f"{loc}: target '{target}' already contains src:{source}:{sid} — duplicate apply",
                    ))

    for i, c in enumerate(creates):
        loc = f"creates[{i}]"
        if not isinstance(c, dict):
            findings.append(Finding(
                "ERROR", key,
                f"{loc} is not an object (got {type(c).__name__}: {c!r}) — schema requires {{slug, folder, frontmatter, body}}",
            ))
            continue
        slug = (c.get("slug") or "").strip()
        if slug and slug in slugs:
            findings.append(Finding(
                "ERROR", key,
                f"{loc}.slug='{slug}' collides with existing note — should be an append, not a create",
            ))

    return findings


def lint_all(vault: Path, only: set[str] | None = None) -> list[Finding]:
    pdir = vault / "_system" / "ingestion" / "proposed"
    manifest_path = pdir / "_manifest.json"
    if not manifest_path.exists():
        return [Finding("ERROR", "<manifest>", f"no manifest at {manifest_path}")]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # `only` scopes the lint to a subset of manifest keys — used by `apply
    # --only` so held-aside proposals don't trip "proposal JSON missing".
    if only is not None:
        manifest = {k: v for k, v in manifest.items() if k in only}
    slugs = vault_slugs(vault)

    findings: list[Finding] = []
    for key, entry in sorted(manifest.items()):
        proposal_path = vault / entry["proposal_path"]
        if not proposal_path.exists():
            findings.append(Finding("ERROR", key, f"proposal JSON missing at {proposal_path.relative_to(vault)}"))
            continue
        try:
            payload = json.loads(proposal_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            findings.append(Finding("ERROR", key, f"invalid JSON: {e}"))
            continue
        findings.extend(lint_proposal(key, entry, payload, slugs, vault))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=VAULT_DEFAULT)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat WARNs as blocking errors.",
    )
    args = parser.parse_args(argv)

    findings = lint_all(args.vault.resolve())
    errors = [f for f in findings if f.severity == "ERROR"]
    warns = [f for f in findings if f.severity == "WARN"]

    if not findings:
        print("lint: clean")
        return 0

    for f in findings:
        print(f.fmt())
    print(f"\nlint: {len(errors)} error(s), {len(warns)} warning(s)")

    if errors or (args.strict and warns):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
