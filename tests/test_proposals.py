"""Tests for the common schema + the semantic resolution gate."""

from datetime import date

from mictlan.schema import DreamProposal, NodeProposal, SectionAppend, NoteType
from mictlan.proposals import resolve_nodes


def _proposal(agent, nodes):
    return DreamProposal(
        agent=agent, target_date=date(2026, 6, 27), policy_version=1, proposed_nodes=nodes
    )


def test_exact_slug_folds_into_existing():
    p = _proposal("Hermes", [NodeProposal(name="Boda", slug="wedding", type=NoteType.topic)])
    backlog = resolve_nodes([p], existing_slugs={"wedding"})
    assert backlog.fold and backlog.fold[0].resolves_to_existing == "wedding"
    assert not backlog.create


def test_no_search_routes_unknowns_to_review_not_create():
    p = _proposal("Hermes", [NodeProposal(name="New Vendor", slug="new-vendor", type=NoteType.person)])
    backlog = resolve_nodes([p], existing_slugs=set(), search=None)
    assert backlog.review and not backlog.create  # never silently create without dedup


def test_semantic_hit_folds_low_score_creates():
    def fake_search(q, k):
        return [("carlos-orlando-vazquez", 0.95)] if "Carlos" in q else [("x", 0.10)]

    p = _proposal(
        "Hermes",
        [
            NodeProposal(name="Carlos Vazquez", slug="carlos-vazquez", type=NoteType.person),
            NodeProposal(name="Totally New", slug="totally-new", type=NoteType.topic),
        ],
    )
    backlog = resolve_nodes([p], existing_slugs=set(), search=fake_search)
    folded = {n.slug for n in backlog.fold}
    created = {n.slug for n in backlog.create}
    assert "carlos-vazquez" in folded
    assert "totally-new" in created


def test_cross_agent_merge_sums_times_seen():
    a = _proposal("Hermes", [NodeProposal(name="R&D", slug="rnd", type=NoteType.topic, times_seen=2)])
    b = _proposal("Claude Code", [NodeProposal(name="R&D", slug="rnd", type=NoteType.topic, times_seen=3)])
    backlog = resolve_nodes([a, b], existing_slugs=set(), search=lambda q, k: [])
    assert len(backlog.create) == 1
    assert backlog.create[0].times_seen == 5


def test_safe_appends_excludes_guardrail_and_ephemeral():
    p = DreamProposal(
        agent="Hermes", target_date=date(2026, 6, 27), policy_version=1,
        appends=[
            SectionAppend(target_slug="goes", section_date=date(2026, 6, 27), content="x", source_marker="## 2026-06-27 — Hermes", durable=True),
            SectionAppend(target_slug="wedding", section_date=date(2026, 6, 27), content="y", source_marker="sig", durable=True, guardrail_hit=True),
            # durable defaults to False (fail closed): unmarked appends are never safe
            SectionAppend(target_slug="z", section_date=date(2026, 6, 27), content="z", source_marker="sig"),
        ],
    )
    safe = p.safe_appends()
    assert [a.target_slug for a in safe] == ["goes"]


def test_slug_dedup_is_hyphen_insensitive_both_directions():
    def prop(slug):
        return DreamProposal(
            agent="Hermes", target_date=date(2026, 6, 27), policy_version=1,
            proposed_nodes=[NodeProposal(name=slug, slug=slug, type=NoteType.topic)],
        )

    # proposal without hyphens folds into hyphenated existing note
    backlog = resolve_nodes([prop("saludmental")], existing_slugs={"salud-mental"})
    assert [n.resolves_to_existing for n in backlog.fold] == ["salud-mental"]
    # and the reverse
    backlog = resolve_nodes([prop("salud-mental")], existing_slugs={"saludmental"})
    assert [n.resolves_to_existing for n in backlog.fold] == ["saludmental"]


def test_append_durability_defaults_to_fail_closed():
    a = SectionAppend(target_slug="goes", section_date=date(2026, 6, 27),
                      content="x", source_marker="sig")
    assert a.durable is False
