"""The common proposal schema — the single envelope every dreamer emits.

This is the *contract* that unifies the three agents (Claude Code, Hermes,
OpenClaw/Nico). Each agent runs its own pipeline but returns a ``DreamProposal``
instead of bespoke markdown, so the outputs become reconcilable: one
entity-resolution + human-approval gate consumes proposals from all agents.

Trust posture (mirrors dream-policy.md §1, §2, §5):
- ``appends`` to an EXISTING entity note are the ONLY auto-applyable output
  (durable, non-guardrailed). Everything else is propose-only.
- ``proposed_nodes`` and ``proposed_links`` are NEVER auto-created; they flow to
  the approval gate.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class NoteType(str, Enum):
    person = "person"
    project = "project"
    topic = "topic"
    ref = "ref"
    meeting = "meeting"
    daily = "daily"
    conversation = "conversation"


class LifeArea(str, Enum):
    """Orthogonal life-domain axis (distinct from ``context:`` sensitivity)."""

    work = "work"
    restaurants = "restaurants"
    wedding = "wedding"
    personal = "personal"
    rnd = "rnd"
    health = "health"
    finance = "finance"


Confidence = Literal["high", "medium", "low"]


class EntityMention(BaseModel):
    """A named entity the dreamer saw, for the resolution gate to reconcile."""

    name: str
    type: NoteType = NoteType.topic
    times_seen: int = 1
    evidence: str = ""


class SectionAppend(BaseModel):
    """A dated H2 section to append to an EXISTING note. Auto-applyable iff safe."""

    target_slug: str = Field(..., description="Existing notes/<slug>.md")
    section_date: date
    content: str
    source_marker: str = Field(
        ..., description="e.g. <!-- src:claude-jsonl:abc123 --> or agent signature"
    )
    durable: bool = True
    guardrail_hit: bool = False


class NodeProposal(BaseModel):
    """A brand-new note proposed for a repeatedly-seen entity. NEVER auto-created."""

    name: str
    slug: str = Field(..., description="kebab-case canonical slug")
    type: NoteType
    area: Optional[LifeArea] = None
    times_seen: int = 1
    first_seen: Optional[date] = None
    last_seen: Optional[date] = None
    suggested_summary: str = ""
    suggested_links: list[str] = Field(default_factory=list)
    # filled by the resolution gate:
    resolves_to_existing: Optional[str] = Field(
        default=None, description="existing slug this dedups to, if any"
    )
    resolution_confidence: Optional[Confidence] = None


class LinkProposal(BaseModel):
    """A proposed [[wikilink]] between two existing notes. NEVER auto-applied."""

    note_a: str
    note_b: str
    relation: Optional[str] = Field(
        default=None, description="typed edge, e.g. reports-to, vendor-for, located-in"
    )
    evidence: str = ""
    confidence: Confidence = "low"


class DreamProposal(BaseModel):
    """Everything one dreamer learned in one run, in one envelope."""

    agent: str = Field(..., description="registered agent identity (policy.agents)")
    target_date: date
    policy_version: int
    policy_stale: bool = False

    appends: list[SectionAppend] = Field(default_factory=list)
    proposed_nodes: list[NodeProposal] = Field(default_factory=list)
    proposed_links: list[LinkProposal] = Field(default_factory=list)
    entities: list[EntityMention] = Field(default_factory=list)

    def safe_appends(self) -> list[SectionAppend]:
        """Appends eligible for auto-apply: durable, no guardrail hit."""
        return [a for a in self.appends if a.durable and not a.guardrail_hit]
