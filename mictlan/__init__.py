"""mictlan — the centralized dreaming (memory consolidation) engine.

One engine, many thin per-agent adapters. Each adapter discovers and parses its
own sources, then emits the common proposal schema (``mictlan.schema``). The
engine handles everything shared: policy loading, dedup ledger, triage, the
vault writer (append-only / signed / guardrailed), and the semantic
entity-resolution gate that dedups proposed nodes against the existing graph
before any node is created.

Node creation stays propose-only with a single human approval gate. mictlan
does NOT auto-construct the graph.
"""

from .schema import (
    DreamProposal,
    NodeProposal,
    LinkProposal,
    SectionAppend,
    EntityMention,
)

__all__ = [
    "DreamProposal",
    "NodeProposal",
    "LinkProposal",
    "SectionAppend",
    "EntityMention",
]

__version__ = "0.1.0"
