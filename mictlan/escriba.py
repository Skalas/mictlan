"""Escriba meeting sync: lightweight meeting cards in the vault.

Escriba exports one markdown file per recorded session
(``~/Documents/Escriba/<Title>_<8-hex>.md``). This module turns each meaningful
session into a small card under ``meetings/`` — frontmatter for brain-mcp, the
user's own notes, and 3–6 LLM-written topic bullets — plus a tracing pointer back
to the original export. Transcripts are never copied verbatim into the vault.

Idempotent by ``escriba_id``: a session that already has a card is never
re-summarized, even if its title changed upstream.

    python -m mictlan.escriba [--dry-run] [--reindex]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from mictlan.analyzer import write_if_changed
from mictlan.paths import ESCRIBA_EXPORT_DIR, VAULT

LLMCaller = Callable[[str], Any]

GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TIMEOUT_SECONDS = 120.0
API_KEY_ENV_VARS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")

MEETINGS_FOLDER = "meetings"
MIN_TRANSCRIPT_CHARS = 200
SLUG_MAX_LEN = 50
SLUG_FALLBACK = "sesion"
MIN_TOPICS = 3
MAX_TOPICS = 6
# Long meetings: keep the opening and the closing, where agendas and wrap-ups live.
TRANSCRIPT_HEAD_CHARS = 25_000
TRANSCRIPT_TAIL_CHARS = 10_000

USER_NOTES_HEADING = "user notes"
TRANSCRIPT_HEADING_RE = re.compile(r"^## Transcript[ \t]*$", re.MULTILINE)
SECTION_HEADING_RE = re.compile(r"^## (.+?)[ \t]*$", re.MULTILINE)
TITLE_RE = re.compile(r"^# (.+?)[ \t]*$", re.MULTILINE)
DATE_FIELD_RE = re.compile(r"^\*\*Date:\*\*[ \t]*(.+?)[ \t]*$", re.MULTILINE)
DURATION_FIELD_RE = re.compile(r"^\*\*Duration:\*\*[ \t]*(.+?)[ \t]*$", re.MULTILINE)
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

EXPORT_ID_RE = re.compile(r"_([0-9a-fA-F]{8})\.md$")
CARD_ID_RE = re.compile(r"-([0-9a-f]{8})\.md$")
FRONTMATTER_ID_RE = re.compile(r"^escriba_id:[ \t]*[\"']?([0-9a-fA-F]{8})[\"']?[ \t]*$", re.MULTILINE)

ANCHOR_TAG_RE = re.compile(r"<a id=\"[^\"]*\"></a>")
TIMESTAMP_RE = re.compile(r"\[\d{1,2}:\d{2}:\d{2}\]")
SPEAKER_PREFIX_RE = re.compile(r"^\s*\*\*[^*]+\*\*:\s*")
MARKDOWN_HEADING_RE = re.compile(r"^(#{1,4}) ", re.MULTILINE)


class EscribaError(Exception):
    """Base error for the Escriba meeting sync."""


class EscribaLLMUnavailableError(EscribaError):
    """No Gemini API key is configured for topic summarization."""


class EscribaSummaryError(EscribaError):
    """The LLM returned no usable topic bullets."""


@dataclass
class EscribaSession:
    escriba_id: str
    filename: str
    path: Path
    title: str
    started_at: str
    date_str: str
    duration: str
    user_notes: str
    ai_notes: str
    transcript: str
    useful_transcript_text: str


@dataclass
class SyncReport:
    scanned: int = 0
    orphans_deleted: list[str] = field(default_factory=list)
    trivial_skipped: list[str] = field(default_factory=list)
    already_synced: list[str] = field(default_factory=list)
    created_cards: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# --- parsing ---------------------------------------------------------------


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def _split_note_sections(header: str) -> tuple[str, str]:
    """Return (user_notes, ai_notes) from the block before ``## Transcript``.

    ``## User Notes`` is the user's own writing. Everything else under an H2
    (``## Notes`` / ``## AI Summary`` and the ``## Summary`` / ``## Key Points`` /
    ``## Topics`` siblings Escriba emits) is AI output; the container heading is
    dropped and the sub-headings are kept so the structure survives.
    """
    headings = list(SECTION_HEADING_RE.finditer(header))
    user_parts: list[str] = []
    ai_parts: list[str] = []
    for i, m in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(header)
        name = m.group(1).strip()
        body = header[m.end():end].strip()
        if name.lower() == USER_NOTES_HEADING:
            user_parts.append(body)
        elif name.lower() in ("notes", "ai summary"):
            ai_parts.append(body)
        else:
            ai_parts.append(f"## {name}\n\n{body}".strip())
    return _join_nonempty(user_parts), _join_nonempty(ai_parts)


def _join_nonempty(parts: list[str]) -> str:
    return "\n\n".join(p for p in parts if p)


def useful_text(transcript: str) -> str:
    """Transcript text minus anchors, timestamps and speaker labels."""
    lines = []
    for raw in transcript.splitlines():
        line = TIMESTAMP_RE.sub("", ANCHOR_TAG_RE.sub("", raw))
        line = SPEAKER_PREFIX_RE.sub("", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _resolve_date(started_at: str, title: str, path: Path) -> str:
    for candidate in (started_at, title, path.name):
        m = ISO_DATE_RE.search(candidate)
        if m:
            return m.group(0)
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def parse_escriba_file(path: Path) -> EscribaSession | None:
    """Parse one Escriba export; ``None`` if the filename lacks the ``_<8-hex>`` id."""
    id_match = EXPORT_ID_RE.search(path.name)
    if not id_match:
        return None
    text = path.read_text(encoding="utf-8")
    t_match = TRANSCRIPT_HEADING_RE.search(text)
    header = text[: t_match.start()] if t_match else text
    transcript = text[t_match.end():].strip() if t_match else ""
    title = _first_match(TITLE_RE, header) or path.name[: id_match.start()]
    started_at = _first_match(DATE_FIELD_RE, header)
    user_notes, ai_notes = _split_note_sections(header)
    return EscribaSession(
        escriba_id=id_match.group(1).lower(),
        filename=path.name,
        path=path,
        title=title,
        started_at=started_at,
        date_str=_resolve_date(started_at, title, path),
        duration=_first_match(DURATION_FIELD_RE, header),
        user_notes=user_notes,
        ai_notes=ai_notes,
        transcript=transcript,
        useful_transcript_text=useful_text(transcript),
    )


# --- filters ---------------------------------------------------------------


def is_orphan_empty_session(session: EscribaSession) -> bool:
    """True iff the session has no notes of any kind and zero dialogue lines."""
    return not (
        session.user_notes.strip()
        or session.ai_notes.strip()
        or session.transcript.strip()
        or session.useful_transcript_text.strip()
    )


def cleanup_orphan_files(export_dir: Path) -> list[str]:
    """Delete empty orphan exports (header-only files); return their names."""
    deleted = []
    for path in sorted(export_dir.glob("*.md")):
        session = parse_escriba_file(path)
        if session is not None and is_orphan_empty_session(session):
            path.unlink()
            deleted.append(path.name)
    return deleted


def is_valid_session(session: EscribaSession, min_transcript_chars: int = MIN_TRANSCRIPT_CHARS) -> bool:
    """A session earns a card if it has any notes or enough real dialogue."""
    if session.user_notes.strip() or session.ai_notes.strip():
        return True
    return len(session.useful_transcript_text.strip()) >= min_transcript_chars


# --- cards -----------------------------------------------------------------


def slugify_title(title: str, max_len: int = SLUG_MAX_LEN) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    if len(slug) > max_len:
        cut = slug[:max_len]
        # Back off to the last whole word rather than ending mid-word.
        if slug[max_len] != "-" and "-" in cut:
            cut = cut.rsplit("-", 1)[0]
        slug = cut.strip("-")
    return slug or SLUG_FALLBACK


def existing_meeting_ids(meetings_dir: Path) -> set[str]:
    """Escriba ids already carded, from the filename suffix or ``escriba_id:``."""
    ids: set[str] = set()
    for path in meetings_dir.glob("*.md"):
        m = CARD_ID_RE.search(path.name)
        if m:
            ids.add(m.group(1))
        fm = FRONTMATTER_ID_RE.search(_frontmatter_block(path))
        if fm:
            ids.add(fm.group(1).lower())
    return ids


def _frontmatter_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def _display_path(path: Path) -> str:
    home = Path.home()
    try:
        return f"~/{path.relative_to(home).as_posix()}"
    except ValueError:
        return str(path)


def _yaml_str(value: str) -> str:
    # A JSON string literal is a valid YAML double-quoted scalar.
    return json.dumps(value, ensure_ascii=False)


def _demote_headings(text: str) -> str:
    """Nest Escriba's H2 sub-sections under the card's own H2/H3 structure."""
    return MARKDOWN_HEADING_RE.sub(lambda m: "#" * (len(m.group(1)) + 2) + " ", text)


def _render_own_notes(session: EscribaSession) -> str:
    user, ai = session.user_notes.strip(), _demote_headings(session.ai_notes.strip())
    if user and ai:
        return f"### Notas del usuario\n\n{user}\n\n### Notas de IA (Escriba)\n\n{ai}"
    return user or ai


def card_stem(session: EscribaSession) -> str:
    return f"{session.date_str}-{slugify_title(session.title)}-{session.escriba_id}"


def render_meeting_card(session: EscribaSession, topics: list[str]) -> tuple[str, str]:
    """Return ``(card_filename, markdown)`` for a session's meeting card."""
    stem = card_stem(session)
    transcript_path = _display_path(session.path)
    frontmatter = "\n".join([
        "---",
        f"id: {stem}",
        "type: meeting",
        f"title: {_yaml_str(session.title)}",
        f"date: {session.date_str}",
        f"created: {session.date_str}",
        f"updated: {session.date_str}",
        "status: active",
        "context: work",
        "area: work",
        f"escriba_id: {_yaml_str(session.escriba_id)}",
        f"duration: {_yaml_str(session.duration)}",
        f"transcript_path: {_yaml_str(transcript_path)}",
        "tags: [meeting, escriba]",
        "links: []",
        "---",
    ])
    pointer = "\n".join([
        "> **Puntero de Tracing (Escriba)**",
        f"> - **Transcript verbatim:** `{transcript_path}`",
        f"> - **Fecha/Hora de inicio:** `{session.started_at}`",
        f"> - **Duración:** `{session.duration}`",
        f"> - **Escriba ID:** `{session.escriba_id}`",
    ])
    sections = [frontmatter, f"# {session.title}", pointer]
    own_notes = _render_own_notes(session)
    if own_notes:
        sections.append(f"## Notas Propias\n\n{own_notes}")
    bullets = "\n".join(f"- {t}" for t in topics)
    sections.append(f"## Temas Generales\n\n{bullets}")
    return f"{stem}.md", "\n\n".join(sections) + "\n"


# --- topic summarization ---------------------------------------------------

TOPICS_PROMPT = """Eres un indexador de reuniones para un Vault de Obsidian.

Lee el material de la reunión "{title}" y describe los temas generales tratados.

Reglas:
- NO asumir nombres de hablantes no confirmados ni inventar acuerdos rígidos; describir únicamente los temas generales, proyectos y problemas discutidos para indexación y búsqueda semántica en el Vault.
- Devuelve entre {min_topics} y {max_topics} viñetas de alto nivel, en español, concisas y directas.
- Ancla cada viñeta en el material; no agregues información que no aparezca.

Responde SOLO con JSON con esta forma exacta:
{{"topics": ["viñeta 1", "viñeta 2", "viñeta 3"]}}

### Notas
{notes}

### Transcripción (limpia)
```text
{transcript}
```
"""


def _truncate_transcript(text: str) -> str:
    if len(text) <= TRANSCRIPT_HEAD_CHARS + TRANSCRIPT_TAIL_CHARS:
        return text
    return f"{text[:TRANSCRIPT_HEAD_CHARS]}\n[…]\n{text[-TRANSCRIPT_TAIL_CHARS:]}"


def build_topics_prompt(session: EscribaSession) -> str:
    notes = _join_nonempty([session.user_notes.strip(), session.ai_notes.strip()])
    return TOPICS_PROMPT.format(
        title=session.title,
        min_topics=MIN_TOPICS,
        max_topics=MAX_TOPICS,
        notes=notes or "(sin notas)",
        transcript=_truncate_transcript(session.useful_transcript_text),
    )


def _coerce_topics(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict):
        raw = raw.get("topics", [])
    if not isinstance(raw, list):
        raise EscribaSummaryError(f"unexpected topics payload type: {type(raw).__name__}")
    topics = [str(t).strip().lstrip("-• ").strip() for t in raw if str(t).strip()]
    if not topics:
        raise EscribaSummaryError("LLM returned no topics")
    return topics[:MAX_TOPICS]


def gemini_json_caller(prompt: str) -> Any:
    """Default transport: one Gemini call in JSON mode, parsed."""
    api_key = next((os.environ[k] for k in API_KEY_ENV_VARS if os.environ.get(k)), None)
    if not api_key:
        raise EscribaLLMUnavailableError(f"set one of {', '.join(API_KEY_ENV_VARS)}")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.1},
    }
    # Force IPv4: the Mac Mini's network stalls on IPv6 to the Gemini endpoint.
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    with httpx.Client(transport=transport, timeout=GEMINI_TIMEOUT_SECONDS) as client:
        r = client.post(
            GEMINI_ENDPOINT.format(model=GEMINI_MODEL),
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
        )
    r.raise_for_status()
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])


def summarize_general_topics(session: EscribaSession, llm_caller: LLMCaller | None = None) -> list[str]:
    caller = llm_caller or gemini_json_caller
    return _coerce_topics(caller(build_topics_prompt(session)))


# --- sync ------------------------------------------------------------------


def _reindex_vault(vault: Path) -> None:
    # Lazy: reindex/orchestrate pull in the whole engine, only needed here.
    import mictlan.reindex as reindex
    from mictlan.orchestrate import reindex_vectors

    reindex.main()
    reindex_vectors(vault)


def _sync_session(
    session: EscribaSession,
    meetings_dir: Path,
    llm_caller: LLMCaller | None,
    dry_run: bool,
) -> Path:
    if dry_run:
        return meetings_dir / f"{card_stem(session)}.md"
    topics = summarize_general_topics(session, llm_caller)
    filename, content = render_meeting_card(session, topics)
    card_path = meetings_dir / filename
    write_if_changed(card_path, content)
    return card_path


def sync_escriba_meetings(
    export_dir: Path | None = None,
    vault: Path | None = None,
    llm_caller: LLMCaller | None = None,
    clean_orphans: bool = True,
    run_reindex: bool = False,
    dry_run: bool = False,
) -> SyncReport:
    """Card every new, meaningful Escriba session. See module docstring."""
    export_dir = export_dir or ESCRIBA_EXPORT_DIR
    vault = vault or VAULT
    meetings_dir = vault / MEETINGS_FOLDER
    report = SyncReport()
    if not export_dir.is_dir():
        report.errors.append(f"export dir not found: {export_dir}")
        return report
    if clean_orphans and not dry_run:
        report.orphans_deleted = cleanup_orphan_files(export_dir)
    known_ids = existing_meeting_ids(meetings_dir)
    for path in sorted(export_dir.glob("*.md")):
        session = parse_escriba_file(path)
        if session is None:
            continue
        report.scanned += 1
        if session.escriba_id in known_ids:
            report.already_synced.append(session.filename)
        elif not is_valid_session(session):
            report.trivial_skipped.append(session.filename)
        else:
            _record_sync(session, meetings_dir, llm_caller, dry_run, report)
            known_ids.add(session.escriba_id)
    if run_reindex and report.created_cards and not dry_run:
        _reindex_vault(vault)
    return report


def _record_sync(
    session: EscribaSession,
    meetings_dir: Path,
    llm_caller: LLMCaller | None,
    dry_run: bool,
    report: SyncReport,
) -> None:
    # One failing session (LLM hiccup, bad payload) must not block the rest.
    try:
        report.created_cards.append(_sync_session(session, meetings_dir, llm_caller, dry_run))
    except (EscribaError, httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        report.errors.append(f"{session.filename}: {type(e).__name__}: {e}")


def format_report(report: SyncReport) -> str:
    lines = [
        f"escriba: scanned={report.scanned} created={len(report.created_cards)} "
        f"already_synced={len(report.already_synced)} trivial={len(report.trivial_skipped)} "
        f"orphans_deleted={len(report.orphans_deleted)} errors={len(report.errors)}"
    ]
    lines += [f"  + {p.name}" for p in report.created_cards]
    lines += [f"  ! {e}" for e in report.errors]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--export-dir", type=Path, default=None)
    parser.add_argument("--vault", type=Path, default=None)
    parser.add_argument("--reindex", action="store_true", help="refresh MOCs + vectors when cards are created")
    parser.add_argument("--dry-run", action="store_true", help="report only: no deletes, no LLM, no writes")
    parser.add_argument("--keep-orphans", action="store_true", help="do not delete empty orphan exports")
    args = parser.parse_args(argv)
    report = sync_escriba_meetings(
        export_dir=args.export_dir,
        vault=args.vault,
        clean_orphans=not args.keep_orphans,
        run_reindex=args.reindex,
        dry_run=args.dry_run,
    )
    print(format_report(report))
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
