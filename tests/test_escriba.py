"""Escriba meeting cards: parser, filters, idempotency, rendering, dream-cycle hook."""

import importlib.util
from pathlib import Path

import pytest
import yaml

import mictlan.escriba as escriba
from mictlan.escriba import (
    EscribaSession,
    cleanup_orphan_files,
    existing_meeting_ids,
    is_orphan_empty_session,
    is_valid_session,
    parse_escriba_file,
    render_meeting_card,
    slugify_title,
    summarize_general_topics,
    sync_escriba_meetings,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

ORPHAN = """# Session 2026-06-28 15:30

**Date:** 2026-06-28T21:30:47.002460+00:00
**Duration:** 00:00:00

## Transcript
"""

LONG_LINE = "Revisamos la arquitectura del sistema de citas y el plan de migración de datos."


def _dialogue(lines: int) -> str:
    return "\n".join(
        f'<a id="seg-{i}"></a>[00:00:{i:02d}] {LONG_LINE}' for i in range(lines)
    )


def _export(title: str, body: str, transcript: str = "", duration: str = "00:28:46") -> str:
    return (
        f"# {title}\n\n**Date:** 2026-07-05T04:05:30.893028+00:00\n"
        f"**Duration:** {duration}\n\n{body}## Transcript\n\n{transcript}"
    )


def _write(directory: Path, name: str, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def _session(**overrides) -> EscribaSession:
    base = dict(
        escriba_id="b8accd2d",
        filename="Propuesta de estructura para ADRs_b8accd2d.md",
        path=Path.home() / "Documents/Escriba/Propuesta de estructura para ADRs_b8accd2d.md",
        title="Propuesta de estructura para ADRs",
        started_at="2026-07-05T04:05:30.893028+00:00",
        date_str="2026-07-05",
        duration="00:28:46",
        user_notes="",
        ai_notes="",
        transcript="",
        useful_transcript_text="",
    )
    base.update(overrides)
    return EscribaSession(**base)  # type: ignore[arg-type]


def _fake_llm(calls: list):
    def caller(prompt):
        calls.append(prompt)
        return {"topics": ["Arquitectura de citas", "Migración de datos", "Plan de pruebas"]}

    return caller


# --- parser ----------------------------------------------------------------


def test_parse_user_notes_and_transcript(tmp_path):
    path = _write(tmp_path, "Revisión semanal_767f7816.md",
                  _export("Revisión semanal", "## User Notes\n\nmy context\n\n", _dialogue(2)))

    s = parse_escriba_file(path)

    assert s is not None
    assert s.escriba_id == "767f7816"
    assert s.title == "Revisión semanal"
    assert s.started_at == "2026-07-05T04:05:30.893028+00:00"
    assert s.date_str == "2026-07-05"
    assert s.duration == "00:28:46"
    assert s.user_notes == "my context"
    assert s.ai_notes == ""
    assert s.transcript.startswith('<a id="seg-0">')
    assert s.useful_transcript_text == f"{LONG_LINE}\n{LONG_LINE}"


def test_parse_notes_keeps_nested_summary_sections(tmp_path):
    body = (
        "## Notes\n\n## Summary\n\nResumen breve.\n\n## Key Points\n\n- Punto A\n\n"
        "## Topics\n\n- Hollow Knight\n\n"
    )
    path = _write(tmp_path, "Silksong_ab29e530.md", _export("Silksong", body, "[00:00:01] hola"))

    s = parse_escriba_file(path)

    assert s is not None
    assert s.user_notes == ""
    assert "## Summary\n\nResumen breve." in s.ai_notes
    assert "## Key Points\n\n- Punto A" in s.ai_notes
    assert "## Topics\n\n- Hollow Knight" in s.ai_notes
    assert "Transcript" not in s.ai_notes


def test_parse_ai_summary_heading(tmp_path):
    path = _write(tmp_path, "Sync_14d7d8fb.md", _export("Sync", "## AI Summary\n\nEnhanced notes\n\n"))

    s = parse_escriba_file(path)

    assert s is not None and s.ai_notes == "Enhanced notes"


def test_parse_transcript_only_strips_speakers(tmp_path):
    transcript = '<a id="seg-1"></a>[01:02:03] **Ana**: Buenos días\n\n[00:00:09] Seguimos'
    path = _write(tmp_path, "Daily_3db35120.md", _export("Daily", "", transcript))

    s = parse_escriba_file(path)

    assert s is not None
    assert s.user_notes == s.ai_notes == ""
    assert s.useful_transcript_text == "Buenos días\nSeguimos"


def test_parse_rejects_filename_without_id(tmp_path):
    path = _write(tmp_path, "2026-09-10-impacto.md", _export("x", "", "hola"))
    assert parse_escriba_file(path) is None


def test_parse_title_falls_back_to_filename(tmp_path):
    path = _write(tmp_path, "Sin título_aaaaaaaa.md", "**Date:** 2026-01-02T00:00:00\n\n## Transcript\n")
    s = parse_escriba_file(path)
    assert s is not None and s.title == "Sin título" and s.date_str == "2026-01-02"


# --- orphan & noise filters --------------------------------------------------


def test_orphan_cleanup_deletes_only_empty_sessions(tmp_path):
    orphan = _write(tmp_path, "Session 2026-06-28 15_30_78782658.md", ORPHAN)
    assert 100 <= orphan.stat().st_size <= 120  # the ~109-byte header-only export
    with_notes = _write(tmp_path, "Notes_767f7816.md", _export("Notes", "## User Notes\n\nctx\n\n"))
    with_dialogue = _write(tmp_path, "Talk_412faf8c.md", _export("Talk", "", "[00:00:00] Architecture review"))
    foreign = _write(tmp_path, "notes.md", "")

    deleted = cleanup_orphan_files(tmp_path)

    assert deleted == [orphan.name]
    assert not orphan.exists()
    assert with_notes.exists() and with_dialogue.exists() and foreign.exists()


def test_is_orphan_empty_session():
    assert is_orphan_empty_session(_session())
    assert not is_orphan_empty_session(_session(user_notes="x"))
    assert not is_orphan_empty_session(_session(ai_notes="x"))
    assert not is_orphan_empty_session(_session(transcript="[00:00:00] hi", useful_transcript_text="hi"))


def test_is_valid_session_noise_threshold():
    assert not is_valid_session(_session(useful_transcript_text="x" * 199))
    assert is_valid_session(_session(useful_transcript_text="x" * 200))
    assert is_valid_session(_session(useful_transcript_text="corto", user_notes="ctx"))
    assert is_valid_session(_session(useful_transcript_text="", ai_notes="resumen"))


# --- slug & idempotency ------------------------------------------------------


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Alineación de necesidades y fase 3", "alineacion-de-necesidades-y-fase-3"),
        ("Año ñoño: ¿qué pasó?", "ano-nono-que-paso"),
        ("!!!", "sesion"),
    ],
)
def test_slugify_title(title, expected):
    assert slugify_title(title) == expected


def test_slugify_title_truncates_on_word_boundary():
    slug = slugify_title("Propuesta de estructura para ADRs del sistema nacional", max_len=30)
    assert slug == "propuesta-de-estructura-para"
    assert len(slug) <= 30


def test_existing_meeting_ids_reads_suffix_and_frontmatter(tmp_path):
    _write(tmp_path, "2026-07-05-algo-b8accd2d.md", "# x\n")
    _write(tmp_path, "renamed-card.md", '---\nid: x\nescriba_id: "ABCDEF12"\n---\n# x\n')
    _write(tmp_path, "plain.md", "escriba_id: 99999999\n")  # not frontmatter

    assert existing_meeting_ids(tmp_path) == {"b8accd2d", "abcdef12"}


def test_sync_is_idempotent_by_escriba_id(tmp_path):
    export, vault = tmp_path / "Escriba", tmp_path / "Vault"
    _write(export, "Reunión de arquitectura_c13e1f5d.md", _export("Reunión de arquitectura", "", _dialogue(5)))
    calls = []

    first = sync_escriba_meetings(export, vault, _fake_llm(calls))
    # Escriba re-exports the session under a new title; the id is unchanged.
    (export / "Reunión de arquitectura_c13e1f5d.md").rename(export / "Otro título_c13e1f5d.md")
    second = sync_escriba_meetings(export, vault, _fake_llm(calls))

    assert len(first.created_cards) == 1
    assert second.created_cards == []
    assert second.already_synced == ["Otro título_c13e1f5d.md"]
    assert len(calls) == 1
    assert len(list((vault / "meetings").glob("*.md"))) == 1


def test_sync_full_flow_report(tmp_path):
    export, vault = tmp_path / "Escriba", tmp_path / "Vault"
    _write(export, "Session 2026-06-28 15_30_78782658.md", ORPHAN)
    _write(export, "Ruido_14d7d8fb.md", _export("Ruido", "", "[00:00:00] hola"))
    _write(export, "Plan_94ac7f8b.md", _export("Plan", "## User Notes\n\nctx\n\n"))
    _write(export, "README.md", "# not an export")
    calls = []

    report = sync_escriba_meetings(export, vault, _fake_llm(calls))

    assert report.orphans_deleted == ["Session 2026-06-28 15_30_78782658.md"]
    assert report.scanned == 2
    assert report.trivial_skipped == ["Ruido_14d7d8fb.md"]
    assert [p.name for p in report.created_cards] == ["2026-07-05-plan-94ac7f8b.md"]
    assert report.errors == []
    assert len(calls) == 1


def test_sync_records_llm_failure_and_continues(tmp_path):
    export, vault = tmp_path / "Escriba", tmp_path / "Vault"
    _write(export, "A_aaaaaaaa.md", _export("A", "## User Notes\n\nuno\n\n"))
    _write(export, "B_bbbbbbbb.md", _export("B", "## User Notes\n\ndos\n\n"))
    responses = iter([{"topics": []}, {"topics": ["Tema uno", "Tema dos", "Tema tres"]}])

    report = sync_escriba_meetings(export, vault, lambda _p: next(responses))

    assert len(report.errors) == 1 and report.errors[0].startswith("A_aaaaaaaa.md")
    assert [p.name for p in report.created_cards] == ["2026-07-05-b-bbbbbbbb.md"]


def test_sync_dry_run_touches_nothing(tmp_path):
    export, vault = tmp_path / "Escriba", tmp_path / "Vault"
    orphan = _write(export, "Session_78782658.md", ORPHAN)
    _write(export, "Plan_94ac7f8b.md", _export("Plan", "## User Notes\n\nctx\n\n"))

    report = sync_escriba_meetings(export, vault, lambda _p: pytest.fail("LLM called"), dry_run=True)

    assert orphan.exists()
    assert len(report.created_cards) == 1
    assert not (vault / "meetings").exists()


def test_sync_reindexes_only_when_cards_created(tmp_path, monkeypatch):
    export, vault = tmp_path / "Escriba", tmp_path / "Vault"
    _write(export, "Plan_94ac7f8b.md", _export("Plan", "## User Notes\n\nctx\n\n"))
    reindexed = []
    monkeypatch.setattr(escriba, "_reindex_vault", reindexed.append)

    sync_escriba_meetings(export, vault, _fake_llm([]), run_reindex=True)
    sync_escriba_meetings(export, vault, _fake_llm([]), run_reindex=True)

    assert reindexed == [vault]


# --- summarization -----------------------------------------------------------


def test_summarize_prompt_guards_and_truncation():
    prompts = []
    long_text = "a" * 30_000 + "b" * 20_000
    topics = summarize_general_topics(_session(useful_transcript_text=long_text), _fake_llm(prompts))

    prompt = prompts[0]
    assert "NO asumir nombres de hablantes no confirmados ni inventar acuerdos rígidos" in prompt
    assert '{"topics":' in prompt
    assert "a" * 25_000 in prompt and "a" * 25_001 not in prompt
    assert "b" * 10_000 in prompt and "b" * 10_001 not in prompt
    assert topics == ["Arquitectura de citas", "Migración de datos", "Plan de pruebas"]


@pytest.mark.parametrize(
    "payload",
    [
        ["- Uno", "Dos", "Tres"],
        '{"topics": ["Uno", "Dos", "Tres"]}',
        {"topics": ["Uno", "Dos", "Tres", "", "  "]},
    ],
)
def test_summarize_accepts_list_str_and_dict(payload):
    assert summarize_general_topics(_session(), lambda _p: payload) == ["Uno", "Dos", "Tres"]


def test_summarize_caps_at_six_topics():
    topics = summarize_general_topics(_session(), lambda _p: {"topics": [f"t{i}" for i in range(9)]})
    assert len(topics) == 6


def test_default_caller_requires_api_key(monkeypatch):
    for var in escriba.API_KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(escriba.EscribaLLMUnavailableError):
        summarize_general_topics(_session())


# --- card rendering ----------------------------------------------------------


def _split_card(content: str) -> tuple[dict, str]:
    _, fm, body = content.split("---\n", 2)
    return yaml.safe_load(fm), body


def test_render_meeting_card_frontmatter_and_sections():
    s = _session(title='ADRs: "v2"', user_notes="mi contexto", ai_notes="## Summary\n\nResumen IA")
    filename, content = render_meeting_card(s, ["Tema uno", "Tema dos", "Tema tres"])

    fm, body = _split_card(content)
    assert filename == "2026-07-05-adrs-v2-b8accd2d.md"
    assert fm["id"] == "2026-07-05-adrs-v2-b8accd2d"
    assert fm["type"] == "meeting"
    assert fm["title"] == 'ADRs: "v2"'
    assert str(fm["date"]) == str(fm["created"]) == str(fm["updated"]) == "2026-07-05"
    assert fm["status"] == "active" and fm["context"] == "work" and fm["area"] == "work"
    assert fm["escriba_id"] == "b8accd2d"
    assert fm["duration"] == "00:28:46"
    assert fm["transcript_path"] == "~/Documents/Escriba/Propuesta de estructura para ADRs_b8accd2d.md"
    assert fm["tags"] == ["meeting", "escriba"]
    assert fm["links"] == []

    assert '\n# ADRs: "v2"\n' in body
    assert "> **Puntero de Tracing (Escriba)**" in body
    assert "> - **Escriba ID:** `b8accd2d`" in body
    assert "## Notas Propias" in body
    assert "### Notas del usuario\n\nmi contexto" in body
    assert "#### Summary\n\nResumen IA" in body  # nested under the card's H3
    assert body.rstrip().endswith("## Temas Generales\n\n- Tema uno\n- Tema dos\n- Tema tres")
    assert "Transcript\n\n<a id" not in body  # transcript never copied verbatim


def test_render_meeting_card_omits_notes_section_when_empty():
    _, content = render_meeting_card(_session(), ["Uno", "Dos", "Tres"])
    assert "## Notas Propias" not in content


def test_rendered_card_is_detected_as_existing(tmp_path):
    filename, content = render_meeting_card(_session(), ["Uno", "Dos", "Tres"])
    _write(tmp_path, "renamed.md", content)
    assert existing_meeting_ids(tmp_path) == {"b8accd2d"}


# --- dream cycle integration -------------------------------------------------


@pytest.fixture
def dream_cycle(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "hermes_dream_cycle_under_test", REPO_ROOT / "adapters/hermes/dream_cycle.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_policy", lambda: type("P", (), {"version": "test"})())
    monkeypatch.setattr(module, "log_audit", lambda *a, **k: None)
    return module


def test_dream_cycle_syncs_meetings_even_without_messages(dream_cycle, tmp_path, monkeypatch):
    export, vault = tmp_path / "Escriba", tmp_path / "Vault"
    _write(export, "Plan_94ac7f8b.md", _export("Plan", "## User Notes\n\nctx\n\n"))
    reindexed = []
    monkeypatch.setattr(escriba, "ESCRIBA_EXPORT_DIR", export)
    monkeypatch.setattr(escriba, "_reindex_vault", reindexed.append)
    monkeypatch.setattr(dream_cycle, "_VAULT", vault)
    monkeypatch.setattr(dream_cycle, "GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(dream_cycle, "call_gemini", _fake_llm([]))
    monkeypatch.setattr(dream_cycle, "get_messages_for_date", lambda _d: [])

    dream_cycle.run_dream_cycle("2026-07-05")

    assert (vault / "meetings" / "2026-07-05-plan-94ac7f8b.md").exists()
    assert reindexed == [vault]


def test_dream_cycle_skips_meetings_without_api_key(dream_cycle, tmp_path, monkeypatch):
    monkeypatch.setattr(dream_cycle, "GOOGLE_API_KEY", None)
    monkeypatch.setattr(dream_cycle, "sync_escriba_meetings", lambda **k: pytest.fail("synced"))
    monkeypatch.setattr(dream_cycle, "get_messages_for_date", lambda _d: [])

    dream_cycle.run_dream_cycle("2026-07-05")  # must not raise


def test_dream_cycle_meeting_failure_does_not_block_consolidation(dream_cycle, monkeypatch, capsys):
    def boom(**_kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(dream_cycle, "GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(dream_cycle, "sync_escriba_meetings", boom)
    monkeypatch.setattr(dream_cycle, "get_messages_for_date", lambda _d: [])

    dream_cycle.run_dream_cycle("2026-07-05")

    assert "Escriba sync failed: OSError: disk gone" in capsys.readouterr().err
