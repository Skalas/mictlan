"""Shared ingest framework: dataclasses, idempotent apply path, state tracking.

Source-specific ingesters (ingest_repos.py, ingest_claude_code.py, ...) build
ConversationUnit / GraphUpdate objects and call apply(). The LLM analyzer
function (analyze_with_llm) is plugged in later once an ANTHROPIC_API_KEY is
available; ingesters that don't need an LLM (e.g. repos) construct GraphUpdate
directly via heuristics.

Conventions enforced here:
- Every dated H2 append carries a source hash comment for idempotency.
- Frontmatter is parsed/serialized via PyYAML; ordering preserved by key list.
- Files are only written when content actually changes (byte-for-byte compare).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import yaml

from mictlan.paths import VAULT
NOTES = VAULT / "notes"
CONVERSATIONS = VAULT / "conversations"
STATE_PATH = VAULT / "_system" / "ingestion" / "state.json"

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
HASH_COMMENT_RE = re.compile(r"<!--\s*src:([^\s]+):([^\s]+)\s*-->")


# ---------- Data classes ----------


@dataclass
class Turn:
    role: str
    content: str
    timestamp: str | None = None


@dataclass
class ConversationUnit:
    source: str
    source_id: str
    started_at: str
    title: str | None = None
    turns: list[Turn] = field(default_factory=list)
    cwd: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class NewNote:
    slug: str
    folder: str
    frontmatter: dict
    body: str


@dataclass
class Append:
    target_slug: str
    section_date: str
    content: str
    source: str
    source_id: str
    heading_slug: str = ""


@dataclass
class GraphUpdate:
    creates: list[NewNote] = field(default_factory=list)
    appends: list[Append] = field(default_factory=list)
    log_note: NewNote | None = None
    skip_reason: str | None = None


@dataclass
class ApplyReport:
    created: list[str] = field(default_factory=list)
    appended: list[str] = field(default_factory=list)
    converted_to_append: list[str] = field(default_factory=list)
    skipped_idempotent: list[str] = field(default_factory=list)
    log_written: str | None = None
    errors: list[str] = field(default_factory=list)


# ---------- Frontmatter I/O ----------


def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[_\s]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "untitled"


def load_note(path: Path) -> tuple[dict, str] | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    return fm, m.group(2)


def serialize_note(fm: dict, body: str) -> str:
    return f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()}\n---\n{body}"


def write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp file on same filesystem, then os.replace.
    # If disk fills or process dies mid-write, the target is untouched.
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return True


# ---------- Idempotency helpers ----------


def find_note(slug: str) -> Path | None:
    for folder in ("notes", "meetings", "daily", "conversations"):
        p = VAULT / folder / f"{slug}.md"
        if p.exists():
            return p
    return None


def has_source_hash(body: str, source: str, source_id: str) -> bool:
    """True if a dated H2 with the given source hash already exists in the body."""
    target = f"src:{source}:{source_id}"
    return target in body


def append_dated_section(body: str, section_date: str, content: str, source: str, source_id: str, heading_slug: str = "") -> str:
    slug_part = f" — {heading_slug}" if heading_slug else ""
    header = f"## {section_date}{slug_part} <!-- src:{source}:{source_id} -->"
    block = f"\n\n{header}\n\n{content.strip()}\n"
    return body.rstrip() + block + "\n"


# ---------- Apply ----------


def apply(update: GraphUpdate, today: str | None = None) -> ApplyReport:
    if today is None:
        today = date.today().isoformat()
    report = ApplyReport()

    if update.skip_reason:
        report.errors.append(f"skipped: {update.skip_reason}")
        return report

    # 1. New notes (with collision handling: convert to append if slug exists)
    for new in update.creates:
        if not SLUG_RE.match(new.slug):
            report.errors.append(f"invalid slug: {new.slug}")
            continue
        existing = find_note(new.slug)
        if existing:
            converted = Append(
                target_slug=new.slug,
                section_date=today,
                content=new.body,
                source=new.frontmatter.get("source") or "manual",
                source_id=new.frontmatter.get("id") or new.slug,
            )
            _apply_append(converted, report, today=today)
            report.converted_to_append.append(new.slug)
            continue
        path = VAULT / new.folder / f"{new.slug}.md"
        fm = dict(new.frontmatter)
        fm.setdefault("id", new.slug)
        fm.setdefault("created", today)
        fm.setdefault("updated", today)
        content = serialize_note(fm, new.body if new.body.startswith("\n") else f"\n{new.body}")
        if write_if_changed(path, content):
            report.created.append(str(path.relative_to(VAULT)))

    # 2. Appends to existing notes
    for app in update.appends:
        _apply_append(app, report, today=today)

    # 3. Log note (the conversation file)
    if update.log_note:
        ln = update.log_note
        path = VAULT / ln.folder / f"{ln.slug}.md"
        fm = dict(ln.frontmatter)
        fm.setdefault("id", ln.slug)
        fm.setdefault("created", today)
        fm.setdefault("updated", today)
        content = serialize_note(fm, ln.body if ln.body.startswith("\n") else f"\n{ln.body}")
        if write_if_changed(path, content):
            report.log_written = str(path.relative_to(VAULT))

    return report


def _apply_append(app: Append, report: ApplyReport, today: str | None = None) -> None:
    target = find_note(app.target_slug)
    if not target:
        report.errors.append(f"append target missing: {app.target_slug}")
        return
    loaded = load_note(target)
    if not loaded:
        report.errors.append(f"could not parse: {target}")
        return
    fm, body = loaded
    if has_source_hash(body, app.source, app.source_id):
        report.skipped_idempotent.append(f"{app.target_slug}:{app.source_id}")
        return
    new_body = append_dated_section(body, app.section_date, app.content, app.source, app.source_id, app.heading_slug)
    fm["updated"] = today or app.section_date
    content = serialize_note(fm, new_body)
    if write_if_changed(target, content):
        report.appended.append(str(target.relative_to(VAULT)))


# ---------- State tracking ----------


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_if_changed(STATE_PATH, json.dumps(state, indent=2, sort_keys=True))


# ---------- LLM analyzer (subagent dispatch) ----------

RECIPE_PATH = VAULT / "_system" / "recipes" / "conversation-append-pass.md"
NOTE_TOP_CHARS = 1200


def _read_note_top(path: Path, n: int = NOTE_TOP_CHARS) -> str:
    text = path.read_text(encoding="utf-8")
    return text[:n] + ("\n…[truncated]" if len(text) > n else "")


def build_vault_context(unit: ConversationUnit) -> dict:
    """Gather the context a subagent needs to analyze a single conversation.

    Returns a JSON-serializable dict with:
      - existing_slugs: every note id currently in the vault (collision avoidance)
      - existing_aliases: alias -> slug map (entity resolution)
      - candidate_notes: {slug: top_chars} for plausible append targets (tag
        intersection with conversation frontmatter when available)
      - recipe_text: the conversation-append-pass recipe (single source of truth
        for H2 format, language rules, guardrails)
    """
    existing_slugs = sorted(list_existing_slugs())
    existing_aliases = list_existing_aliases()

    # Candidate targets: notes whose slug or alias appears in the conversation
    # frontmatter tags / title / cwd path. Cheap heuristic; the subagent gets
    # the final say. We always include the top of any matched note so the
    # subagent can check voice/language/existing entries.
    candidates: dict[str, str] = {}
    conv_tags = {str(t).lower() for t in (unit.extra.get("tags") or [])}
    haystacks: list[str] = []
    haystacks.append((unit.title or "").lower())
    haystacks.append((unit.cwd or "").lower())
    haystacks.extend(conv_tags)
    haystack = " | ".join(h for h in haystacks if h)

    for slug in existing_slugs:
        if slug in candidates:
            continue
        if slug in conv_tags or slug in haystack:
            p = find_note(slug)
            if p is not None:
                candidates[slug] = _read_note_top(p)
    for alias_lower, slug in existing_aliases.items():
        if slug in candidates:
            continue
        if alias_lower in haystack:
            p = find_note(slug)
            if p is not None:
                candidates[slug] = _read_note_top(p)

    recipe_text = RECIPE_PATH.read_text(encoding="utf-8") if RECIPE_PATH.exists() else ""

    return {
        "existing_slugs": existing_slugs,
        "existing_aliases": existing_aliases,
        "candidate_notes": candidates,
        "recipe_text": recipe_text,
    }


def _mode_directive(mode: str) -> str:
    """Steer the digest by coding-session mode (see _system/dream-policy.md §3).

    Producer is the stager (classify_mode); this is the consumer. For non-coding
    sources (claude-web) or legacy units without a mode, returns "" → default
    behavior unchanged.
    """
    if mode == "execution":
        return (
            "\n## Session mode: EXECUTION (coding session, low dialogue)\n\n"
            "This session is about *what was done*, not *what was discussed*. Its "
            "durable value is the DECISION/OUTCOME only. Emit AT MOST ONE append "
            "capturing the key decision or what changed and why "
            '(e.g. "chose X over Y because Z"). If there is no decision worth '
            'recalling months from now, set `skip_reason: "ephemeral"` and return '
            "empty appends/creates. Do NOT narrate the play-by-play of edits and "
            "commands.\n"
        )
    if mode in ("discussion", "mixed"):
        return (
            f"\n## Session mode: {mode.upper()} (reasoning is the value)\n\n"
            "The design reasoning, trade-offs, decisions, and rejected alternatives "
            "ARE the durable content. Digest in full per the recipe — do not "
            "compress to a one-line outcome.\n"
        )
    return ""


def build_analysis_prompt(unit: ConversationUnit, vault_context: dict) -> str:
    """Render the subagent prompt as a single self-contained string.

    The subagent will receive this prompt with no further context, so the
    string embeds: the recipe, the candidate note tops, the conversation
    turns, and a strict JSON output spec.
    """
    candidates_section: list[str] = []
    for slug, top in sorted(vault_context.get("candidate_notes", {}).items()):
        candidates_section.append(f"### {slug}\n\n```markdown\n{top}\n```")
    candidates_block = "\n\n".join(candidates_section) or "_None — no candidate matched._"

    turns_section: list[str] = []
    for t in unit.turns:
        ts = f" ({t.timestamp})" if t.timestamp else ""
        turns_section.append(f"**{t.role}{ts}:**\n\n{t.content.strip()}")
    turns_block = "\n\n---\n\n".join(turns_section)

    recipe = vault_context.get("recipe_text") or "_recipe unavailable_"
    slugs_csv = ", ".join(vault_context.get("existing_slugs") or [])
    mode_directive = _mode_directive((unit.extra.get("mode") or "").lower())

    return f"""You are analyzing a single Claude conversation to decide what (if anything) should be persisted into the user's Obsidian vault.

Follow the recipe below to the letter. Output ONLY a single JSON object matching the schema at the bottom. Do not include prose around the JSON.
{mode_directive}
# Recipe (authoritative)

{recipe}

# Conversation metadata

- source: `{unit.source}`
- source_id (shortid): `{unit.source_id}`
- started_at: `{unit.started_at}`
- title: `{unit.title or ''}`
- cwd: `{unit.cwd or ''}`
- tags: {sorted(unit.extra.get('tags') or [])}
- mode: `{unit.extra.get('mode') or 'n/a'}`

# Candidate target notes (tops of matching notes)

{candidates_block}

# All existing slugs (for collision avoidance — never create a slug that already exists; convert to append instead)

{slugs_csv}

# Conversation turns

{turns_block}

# Output schema

Return exactly one JSON object. No prose, no markdown fences around it.

```
{{
  "skip_reason": null | "low-signal" | "ephemeral" | "duplicate" | "already-cited",
  "appends": [
    {{
      "target_slug": "<existing slug from list above>",
      "section_date": "YYYY-MM-DD",     // unit.started_at date is the default
      "heading_slug": "<3-7 word human-readable title in target note's language>",
      "content": "<full H2 body. Must NOT include the '## YYYY-MM-DD …' heading line — that is added by the apply step. Lead with outcome. Include [[wikilinks]] to entities in the existing-slugs list. Match target note's language.>"
    }}
  ],
  "creates": [
    {{
      "slug": "<new kebab-case slug. MUST NOT collide with any existing slug above. Use 'slug', NOT 'target_slug'.>",
      "folder": "notes",
      "frontmatter": {{
        "type": "person | project | topic | ref",
        "aliases": ["<alternate name>", "..."],     // optional, empty list OK
        "tags": ["kebab-case", "tags"],             // optional, empty list OK
        "status": "active",
        "context": "work | personal | gov | teaching",
        "org": "<organization — required for type=person|project; omit for topic/ref>",
        "start": "YYYY-MM-DD",                       // project only
        "end": null                                   // project only
      }},
      "body": "<initial markdown body. Use the field name 'body' (NOT 'content'). Lead with a 1-3 sentence evergreen summary, then a dated H2: '## YYYY-MM-DD — <slug> <!-- src:source:shortid -->'.>"
    }}
  ]
}}
```

Rules:
- If the conversation is ephemeral / duplicate / already-cited / low-signal, set `skip_reason` and leave `appends`/`creates` empty.
- Prefer appending to an existing note over creating a new one. Only create when no candidate fits AND the entity is durable.
- `target_slug` MUST be one of the existing slugs listed above. Never fabricate.
- One `appends[]` entry per target. Don't append to the same target twice in one proposal.
- Body language must match the target note's existing entries.
- Do not include the `<!-- src:... -->` marker in appends — the apply step adds it. (For `creates`, you DO include it inside the dated H2 of the body, since the apply step writes the body verbatim.)

### Wikilink discipline (strict — most common failure mode)

A wikilink emitted into `content` (appends) or `body` (creates) only resolves if the slug appears in the "All existing slugs" list above, OR appears in your own `creates[]` block in this same proposal.

DO:
- Use `[[<slug>]]` for entities that exist (e.g. `[[goes]]`, `[[python]]`, `[[diego-miranda]]`).
- Use `[[<slug>|<display text>]]` if the wikilink should render as different text (e.g. `[[goes|GOES]]`).
- Verify each wikilink before emitting: scan the "All existing slugs" list. If the slug isn't there and you aren't creating it, drop the wikilink.

DON'T:
- DO NOT emit wikilinks to **conversation files**. Conversation slugs look like `2026-05-19-some-topic-a1b2c3d4` and live in `conversations/`, not `notes/`. They're not durable entity notes — use the `<!-- src:source:shortid -->` marker instead.
- DO NOT invent wikilinks to "what should exist" or "what we just discussed". Only link to slugs you can verify in the list above.
- DO NOT emit a wikilink to the conversation's own shortid (e.g. `[[2026-05-19-foo-bar-a9cc6756]]`) — the `<!-- src:claude-jsonl:a9cc6756 -->` marker already cites it.

### Required wikilinks per append

- Appends: aim for ≥1 wikilink to an entity from the existing-slugs list (in the body, not the heading). The `target_slug` itself counts only if explicitly referenced as `[[target_slug]]` in body text. If no natural wikilink fits, the apply step will warn but not fail.

### Pre-emit verification checklist (do this before returning JSON)

- [ ] Every `target_slug` in `appends[]` appears in the "All existing slugs" list.
- [ ] Every `[[X]]` in `content` (appends) or `body` (creates) — X is in the existing-slugs list OR in this proposal's `creates[]`.
- [ ] No wikilinks point at conversation-like slugs (date-prefixed with shortid suffix).
- [ ] `creates[]` entries use `slug` (not `target_slug`), `body` (not `content`), and include `folder`, `frontmatter`, `body`.
- [ ] `creates[].frontmatter.type` is one of: person, project, topic, ref.
- [ ] `section_date` is YYYY-MM-DD (no other formats).
"""


def parse_proposal(payload: dict, source: str, source_id: str) -> GraphUpdate:
    """Convert a subagent JSON proposal into a GraphUpdate dataclass.

    Raises ValueError on schema violations the apply step can't recover from.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"proposal must be a JSON object, got {type(payload).__name__}")

    skip_reason = payload.get("skip_reason")
    if isinstance(skip_reason, str) and skip_reason.strip():
        return GraphUpdate(skip_reason=skip_reason.strip())

    appends_raw = payload.get("appends") or []
    creates_raw = payload.get("creates") or []
    if not isinstance(appends_raw, list) or not isinstance(creates_raw, list):
        raise ValueError("'appends' and 'creates' must be lists")

    appends: list[Append] = []
    seen_targets: set[str] = set()
    for i, a in enumerate(appends_raw):
        if not isinstance(a, dict):
            raise ValueError(f"appends[{i}] is not an object")
        target = (a.get("target_slug") or "").strip()
        section_date = (a.get("section_date") or "").strip()
        heading_slug = (a.get("heading_slug") or "").strip()
        content = (a.get("content") or "").strip()
        if not (target and section_date and content):
            raise ValueError(
                f"appends[{i}] missing required field "
                f"(target_slug={target!r}, section_date={section_date!r}, "
                f"content_len={len(content)})"
            )
        if target in seen_targets:
            raise ValueError(f"duplicate append target: {target}")
        seen_targets.add(target)
        appends.append(
            Append(
                target_slug=target,
                section_date=section_date,
                content=content,
                source=source,
                source_id=source_id,
                heading_slug=heading_slug,
            )
        )

    creates: list[NewNote] = []
    for i, c in enumerate(creates_raw):
        if not isinstance(c, dict):
            raise ValueError(f"creates[{i}] is not an object")
        slug = (c.get("slug") or "").strip()
        folder = (c.get("folder") or "notes").strip()
        fm = c.get("frontmatter") or {}
        body = c.get("body") or ""
        if not slug:
            raise ValueError(f"creates[{i}] missing slug")
        # folder comes from LLM output — allowlist it so it can't escape the vault.
        if folder not in ("notes", "meetings", "daily", "conversations"):
            raise ValueError(f"creates[{i}] invalid folder: {folder!r}")
        if not isinstance(fm, dict):
            raise ValueError(f"creates[{i}].frontmatter must be an object")
        creates.append(NewNote(slug=slug, folder=folder, frontmatter=fm, body=body))

    return GraphUpdate(creates=creates, appends=appends)


def analyze(
    unit: ConversationUnit,
    vault_context: dict,
    dispatcher,
) -> GraphUpdate:
    """High-level entry: build prompt, call dispatcher, parse result.

    `dispatcher` is a Callable[[str], dict] that takes the analysis prompt and
    returns the parsed JSON payload from the LLM subagent. Caller is responsible
    for picking a dispatch strategy (subprocess to `claude` CLI, Anthropic SDK,
    test mock, etc.).
    """
    prompt = build_analysis_prompt(unit, vault_context)
    payload = dispatcher(prompt)
    return parse_proposal(payload, unit.source, unit.source_id)


def analyze_with_llm(unit: ConversationUnit, vault_context: dict) -> GraphUpdate:
    """Backwards-compatible alias. Raises NotImplementedError — supply a dispatcher
    via `analyze()` directly."""
    raise NotImplementedError(
        "Use analyze(unit, vault_context, dispatcher) — supply a dispatcher callable."
    )


# ---------- Helpers for ingesters ----------


def list_existing_slugs() -> set[str]:
    """All ids currently in the vault — used by ingesters to detect collisions."""
    out: set[str] = set()
    for folder in ("notes", "meetings", "daily", "conversations"):
        d = VAULT / folder
        if d.exists():
            for p in d.glob("*.md"):
                out.add(p.stem)
    return out


def list_existing_aliases() -> dict[str, str]:
    """alias -> slug map, for entity matching."""
    out: dict[str, str] = {}
    for folder in ("notes",):
        d = VAULT / folder
        if not d.exists():
            continue
        for p in d.glob("*.md"):
            loaded = load_note(p)
            if not loaded:
                continue
            fm, _ = loaded
            for a in fm.get("aliases") or []:
                out[a.lower()] = p.stem
            out[p.stem.replace("-", " ")] = p.stem
    return out
