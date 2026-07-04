"""Characterization tests for the vault-mutation path (the only code that
writes real notes): apply(), _apply_append(), idempotency, collision handling.
"""
import pytest

import mictlan.analyzer as analyzer
from mictlan.analyzer import Append, GraphUpdate, NewNote


@pytest.fixture()
def vault(tmp_path):
    (tmp_path / "notes").mkdir()
    analyzer.bind_vault(tmp_path)
    yield tmp_path
    # rebind to the real default so later tests/imports aren't polluted
    from mictlan.paths import VAULT as REAL_VAULT
    analyzer.bind_vault(REAL_VAULT)


def _note(vault, slug, body="\nSeed body.\n", fm="id: {slug}\ncreated: 2026-07-01"):
    p = vault / "notes" / f"{slug}.md"
    p.write_text(f"---\n{fm.format(slug=slug)}\n---{body}", encoding="utf-8")
    return p


def _append(slug, content="New insight.", source="hermes", source_id="abc12345"):
    return Append(
        target_slug=slug, section_date="2026-07-03",
        content=content, source=source, source_id=source_id,
    )


def test_apply_creates_new_note_with_frontmatter_defaults(vault):
    up = GraphUpdate(creates=[NewNote(slug="new-topic", folder="notes",
                                      frontmatter={"type": "topic"}, body="Hello.")])
    report = analyzer.apply(up, today="2026-07-03")

    assert report.created == ["notes/new-topic.md"] and not report.errors
    fm, body = analyzer.load_note(vault / "notes" / "new-topic.md")
    assert fm["id"] == "new-topic" and fm["created"] == "2026-07-03"
    assert "Hello." in body


def test_append_adds_dated_section_with_source_marker_and_bumps_updated(vault):
    target = _note(vault, "goes")
    report = analyzer.apply(GraphUpdate(appends=[_append("goes")]), today="2026-07-03")

    assert report.appended == ["notes/goes.md"] and not report.errors
    fm, body = analyzer.load_note(target)
    assert "## 2026-07-03" in body and "<!-- src:hermes:abc12345 -->" in body
    assert "New insight." in body and "Seed body." in body
    assert fm["updated"] == "2026-07-03"


def test_append_is_idempotent_by_source_hash(vault):
    _note(vault, "goes")
    analyzer.apply(GraphUpdate(appends=[_append("goes")]), today="2026-07-03")
    report2 = analyzer.apply(GraphUpdate(appends=[_append("goes")]), today="2026-07-03")

    assert report2.skipped_idempotent == ["goes:abc12345"]
    assert report2.appended == []
    _, body = analyzer.load_note(vault / "notes" / "goes.md")
    assert body.count("New insight.") == 1


def test_create_collision_converts_to_append(vault):
    _note(vault, "goes")
    up = GraphUpdate(creates=[NewNote(slug="goes", folder="notes",
                                      frontmatter={"source": "hermes", "id": "x1"},
                                      body="Colliding content.")])
    report = analyzer.apply(up, today="2026-07-03")

    assert report.converted_to_append == ["goes"]
    assert report.created == []
    _, body = analyzer.load_note(vault / "notes" / "goes.md")
    assert "Colliding content." in body and "Seed body." in body


def test_append_to_missing_target_is_an_error_not_a_crash(vault):
    report = analyzer.apply(GraphUpdate(appends=[_append("nonexistent")]))
    assert report.errors == ["append target missing: nonexistent"]


def test_path_traversal_slugs_are_rejected(vault):
    for hostile in ("../../etc/passwd", "..", "a/../b", "UPPER", ""):
        assert analyzer.find_note(hostile) is None
    report = analyzer.apply(
        GraphUpdate(creates=[NewNote(slug="../evil", folder="notes",
                                     frontmatter={}, body="x")])
    )
    assert report.errors == ["invalid slug: ../evil"]
    assert report.created == []


def test_skip_reason_short_circuits(vault):
    report = analyzer.apply(GraphUpdate(skip_reason="trivial session"))
    assert report.errors == ["skipped: trivial session"]
