"""Tests for the sink drain — envelope validation + safe-append bridging."""

import json
from datetime import date

from mictlan.inbox import bridge_safe_appends, drain, load_envelopes
from mictlan.schema import DreamProposal, NodeProposal, NoteType, SectionAppend

AGENTS = {"Claude Code", "Hermes"}
GUARDRAILS = {"wedding", "salud-personal"}
GUARD_PREFIXES = ("legal-", "financial-", "salud-", "health-")


def _is_guardrailed(slug: str) -> bool:
    return slug in GUARDRAILS or any(slug.startswith(p) for p in GUARD_PREFIXES)


def _envelope(agent="Hermes", appends=None, nodes=None):
    return DreamProposal(
        agent=agent, target_date=date(2026, 7, 3), policy_version=2,
        appends=appends or [], proposed_nodes=nodes or [],
    )


def _append(slug, marker="<!-- src:hermes:2026-07-03 -->", durable=True, guardrail_hit=False):
    return SectionAppend(
        target_slug=slug, section_date=date(2026, 7, 3),
        content="x", source_marker=marker, durable=durable, guardrail_hit=guardrail_hit,
    )


# ---------- load_envelopes (T3) ----------


def test_valid_envelope_loads(tmp_path):
    (tmp_path / "hermes-2026-07-03.json").write_text(
        _envelope(appends=[_append("goes")]).model_dump_json(), encoding="utf-8"
    )
    res = load_envelopes(tmp_path, AGENTS)
    assert len(res.proposals) == 1 and not res.errors


def test_malformed_json_is_skipped_not_fatal(tmp_path):
    (tmp_path / "hermes-2026-07-03.json").write_text("{not json", encoding="utf-8")
    res = load_envelopes(tmp_path, AGENTS)
    assert not res.proposals and len(res.errors) == 1


def test_schema_invalid_is_skipped(tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps({"agent": "Hermes"}), encoding="utf-8")
    res = load_envelopes(tmp_path, AGENTS)
    assert not res.proposals and len(res.errors) == 1


def test_unregistered_agent_rejected(tmp_path):
    (tmp_path / "nico-2026-07-03.json").write_text(
        _envelope(agent="Nico", appends=[_append("goes")]).model_dump_json(), encoding="utf-8"
    )
    res = load_envelopes(tmp_path, AGENTS)
    assert not res.proposals and "unregistered agent" in res.errors[0]


def test_absent_inbox_is_noop(tmp_path):
    res = load_envelopes(tmp_path / "does-not-exist", AGENTS)
    assert not res.proposals and not res.errors


# ---------- bridge_safe_appends (T4) ----------


def test_safe_append_when_target_exists():
    prop = _envelope(appends=[_append("goes")])
    applied, held = bridge_safe_appends(prop, existing_slugs={"goes"}, is_guardrailed=_is_guardrailed)
    assert len(applied) == 1 and not held
    assert applied[0].source == "hermes" and applied[0].source_id == "2026-07-03"


def test_missing_target_is_held():
    prop = _envelope(appends=[_append("ghost")])
    applied, held = bridge_safe_appends(prop, existing_slugs=set(), is_guardrailed=_is_guardrailed)
    assert not applied and len(held) == 1


def test_guardrailed_slug_is_held():
    prop = _envelope(appends=[_append("wedding")])
    applied, held = bridge_safe_appends(prop, existing_slugs={"wedding"}, is_guardrailed=_is_guardrailed)
    assert not applied and len(held) == 1


def test_ephemeral_append_is_held():
    prop = _envelope(appends=[_append("goes", durable=False)])
    applied, held = bridge_safe_appends(prop, existing_slugs={"goes"}, is_guardrailed=_is_guardrailed)
    assert not applied and len(held) == 1


def test_marker_falls_back_to_agent_when_unparseable():
    prop = _envelope(appends=[_append("goes", marker="just a signature")])
    applied, _ = bridge_safe_appends(prop, existing_slugs={"goes"}, is_guardrailed=_is_guardrailed)
    # source falls back to the agent slug; source_id is a content hash (not the bare date)
    assert applied[0].source == "hermes" and applied[0].source_id != "2026-07-03"


def test_markerless_same_note_same_day_do_not_collide():
    a1 = SectionAppend(target_slug="goes", section_date=date(2026, 7, 3),
                       content="first insight", source_marker="sig")
    a2 = SectionAppend(target_slug="goes", section_date=date(2026, 7, 3),
                       content="second insight", source_marker="sig")
    prop = _envelope(appends=[a1, a2])
    applied, _ = bridge_safe_appends(prop, existing_slugs={"goes"}, is_guardrailed=_is_guardrailed)
    # distinct content → distinct idempotency keys → neither is silently dropped
    assert len(applied) == 2
    assert applied[0].source_id != applied[1].source_id


# ---------- drain (T4 end to end, node routing) ----------


def test_drain_routes_nodes_to_review_without_search(tmp_path):
    env = _envelope(
        appends=[_append("goes")],
        nodes=[NodeProposal(name="New Co", slug="new-co", type=NoteType.person)],
    )
    (tmp_path / "hermes-2026-07-03.json").write_text(env.model_dump_json(), encoding="utf-8")
    res = drain(tmp_path, AGENTS, existing_slugs={"goes"}, is_guardrailed=_is_guardrailed, search=None)
    assert len(res.graph_update.appends) == 1
    assert res.backlog.review and not res.backlog.create  # never auto-create without dedup
