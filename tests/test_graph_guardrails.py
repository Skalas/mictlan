"""Guardrails added in the graph renovation: wikilink sanitization on the
apply path, staleness-honest project MOC, shelf-folder resolution, and the
graph-health report.
"""
from datetime import date

import pytest

import mictlan.analyzer as analyzer
import mictlan.reindex as reindex
from mictlan.analyzer import Append, GraphUpdate, sanitize_wikilinks

TODAY = date(2026, 7, 4)


# ---------- sanitize_wikilinks ----------

def test_sanitize_keeps_clean_slugs_and_display_links():
    text = "See [[goes]] and [[orbis-bos|ORBIS]] for context."
    assert sanitize_wikilinks(text) == text


def test_sanitize_unwraps_code_strings_paths_and_uppercase():
    assert sanitize_wikilinks('x [[vertex_utils.py::logger.error(f"boom {call]] y') == \
        'x vertex_utils.py::logger.error(f"boom {call y'
    assert sanitize_wikilinks("see [[projects/goes/edu/mined]] there") == "see projects/goes/edu/mined there"
    assert sanitize_wikilinks("was [[MOC_IA]] once") == "was MOC_IA once"


def test_sanitize_prefers_display_text_when_unwrapping():
    assert sanitize_wikilinks("[[agents/cto/SOUL|the CTO soul doc]]") == "the CTO soul doc"


# ---------- apply path uses the sanitizer ----------

@pytest.fixture()
def vault(tmp_path):
    (tmp_path / "notes").mkdir()
    analyzer.bind_vault(tmp_path)
    yield tmp_path
    from mictlan.paths import VAULT as REAL_VAULT
    analyzer.bind_vault(REAL_VAULT)


def test_append_content_is_sanitized_before_write(vault):
    p = vault / "notes" / "goes.md"
    p.write_text("---\nid: goes\n---\nSeed.\n", encoding="utf-8")
    app = Append(target_slug="goes", section_date="2026-07-03",
                 content="Broke in [[utils.py::main()]] but [[goes]] is fine.",
                 source="hermes", source_id="zz9")
    report = analyzer.apply(GraphUpdate(appends=[app]), today="2026-07-03")
    assert report.appended and not report.errors
    body = p.read_text(encoding="utf-8")
    assert "[[utils.py::main()]]" not in body
    assert "utils.py::main()" in body and "[[goes]]" in body


def test_find_note_resolves_shelf_folders(vault):
    shelf = vault / "library" / "books"
    shelf.mkdir(parents=True)
    (shelf / "book-dune.md").write_text("---\nid: book-dune\n---\nx\n", encoding="utf-8")
    assert analyzer.find_note("book-dune") == shelf / "book-dune.md"


# ---------- reindex: staleness-honest projects MOC ----------

def _proj(slug, updated, status="active"):
    return {"id": slug, "type": "project", "status": status,
            "updated": updated, "start": "2026-01-01", "org": "x"}


def test_stale_active_projects_are_demoted_out_of_active():
    notes = [_proj("fresh", "2026-07-01"), _proj("stale-one", "2026-04-01")]
    out = reindex.render_projects(notes, today=TODAY)
    active = out.split("## Stale")[0]
    assert "[[fresh]]" in active and "[[stale-one]]" not in active
    assert "last touched 2026-04-01" in out


def test_dormant_and_archived_sections_untouched_by_staleness():
    notes = [_proj("old-dormant", "2025-01-01", status="dormant")]
    out = reindex.render_projects(notes, today=TODAY)
    assert "## Dormant" in out and "## Stale" not in out


# ---------- reindex: catalog kinds leave the concept MOCs ----------

def test_topics_moc_excludes_kind_notes_and_library_includes_them():
    notes = [
        {"id": "python", "type": "topic", "tags": []},
        {"id": "book-dune", "type": "ref", "kind": "book", "status": "read"},
        {"id": "recipe-pan", "type": "ref", "kind": "recipe"},
    ]
    topics = reindex.render_topics(notes)
    assert "[[python]]" in topics and "book-dune" not in topics
    library = reindex.render_library(notes)
    assert "[[book-dune]] — read" in library and "[[recipe-pan]]" in library


def test_gtd_moc_splits_open_and_closed():
    notes = [
        {"id": "task-a", "type": "ref", "kind": "task", "status": "open"},
        {"id": "task-b", "type": "ref", "kind": "task", "status": "done"},
        {"id": "delegated-gera-x", "type": "ref", "kind": "delegated_task", "status": "open"},
    ]
    out = reindex.render_gtd(notes)
    assert "1 open, 1 closed" in out and "[[task-a]]" in out and "[[task-b]]" not in out
    assert "[[delegated-gera-x]]" in out


# ---------- reindex: graph health report ----------

def test_health_report_flags_orphans_piles_and_stale(tmp_path, monkeypatch):
    ndir = tmp_path / "notes"
    ndir.mkdir()
    pile_body = "Intro.\n" + "".join(f"\n## 2026-06-{d:02d} <!-- src:x:{d} -->\n\nnote\n" for d in range(1, 12))
    (ndir / "pile.md").write_text(f"---\nid: pile\ntype: topic\n---\n{pile_body}", encoding="utf-8")
    monkeypatch.setattr(reindex, "VAULT", tmp_path)

    notes = [
        {"id": "pile", "type": "topic", "updated": "2026-07-01"},
        {"id": "lonely", "type": "topic", "updated": "2026-07-01"},
        {"id": "linked", "type": "topic", "updated": "2026-07-01"},
        {"id": "stale-proj", "type": "project", "status": "active", "updated": "2026-03-01"},
        {"id": "book-dune", "type": "ref", "kind": "book"},  # kinds never count as orphans
    ]
    outgoing = {"pile": {"linked"}}
    out = reindex.render_health(notes, outgoing, today=TODAY)
    assert "[[lonely]]" in out.split("## Compaction")[0]
    assert "[[linked]]" not in out.split("## Compaction")[0]
    assert "[[pile]] — 11 appends" in out
    assert "[[stale-proj]] — last touched 2026-03-01" in out
    assert "book-dune" not in out


# ---------- pending: settled dispositions stay settled ----------

def test_pending_drops_slugs_with_journal_disposition(tmp_path, monkeypatch, capsys):
    import sys
    import mictlan.pending as pending
    (tmp_path / "notes").mkdir()
    dreams = tmp_path / "dreams"
    dreams.mkdir()
    (dreams / "2026-06-10.md").write_text(
        "## Proposed new note stubs\n"
        '- "Cafe Escalante" — seen 2 times\n'
        "  - Suggested slug: `cafe-escalante`\n"
        "  - Suggested type: project\n"
        "- \"Sampler\" — seen 3 times\n"
        "  - Suggested slug: `sampler-x`\n",
        encoding="utf-8")
    (dreams / "2026-07-04.md").write_text(
        "## Backlog review\n"
        "- `cafe-escalante` (seen 2× since 2026-06-06) → folded into [[orbis-bos]]\n",
        encoding="utf-8")
    monkeypatch.setattr(pending, "NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setattr(pending, "DREAMS_DIR", str(dreams))
    monkeypatch.setattr(sys, "argv", ["pending"])
    pending.main()
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    slugs = {p["slug"] for p in out["pending"]}
    assert "cafe-escalante" not in slugs and "sampler-x" in slugs
