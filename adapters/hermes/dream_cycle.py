import os
import sys
import sqlite3
import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
import httpx

# Configuration
from mictlan.paths import VAULT as _VAULT  # single shared vault resolver (MICTLAN_VAULT)

DB_PATH = os.path.expanduser("~/.hermes/state.db")
DAILY_DIR = str(_VAULT / "daily")
AUDIT_LOG_PATH = os.path.expanduser("~/.hermes/logs/dream_audit.json")

# Single model for the consolidation fleet.
GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# --- 0. Policy Loading Gate (FAIL CLOSED) ---
# Policy loader comes from the shared engine (requires `mictlan` installed here).
try:
    from mictlan.policy import load_policy
    from mictlan.paths import INBOX
    from mictlan.schema import DreamProposal, SectionAppend, LinkProposal, NodeProposal, NoteType
    policy = load_policy()
    policy_version = policy.version
    heading_signature = policy._d["heading_signature"]
    ingest_boundary_hermes = policy.boundary("Hermes") # usually ["~/.hermes/"]
except Exception as e:
    print(f"❌ FAIL CLOSED: Coexistence policy could not be loaded or parsed: {e}", file=sys.stderr)
    # Log audit as failed
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
        with open(AUDIT_LOG_PATH, 'a') as f:
            f.write(json.dumps({"date": datetime.now().strftime("%Y-%m-%d"), "status": f"failed: policy_unavailable ({e})"}) + "\n")
    except Exception:
        pass
    sys.exit(1)

# Ensure the boundary config makes sense for the run
INGEST_ROOTS = [os.path.expanduser(p) for p in ingest_boundary_hermes] if ingest_boundary_hermes else [os.path.expanduser("~/.hermes/")]

def load_keys_from_openclaw_env():
    env_path = os.path.expanduser("~/.openclaw/service-env/ai.openclaw.gateway.env")
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if "=" in line:
                    key, val = line.split("=", 1)
                    val = val.strip().strip("'").strip('"')
                    os.environ[key.strip()] = val.strip()

load_keys_from_openclaw_env()

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")


def call_gemini(prompt):
    """
    Single LLM transport for the dream cycle. One provider, one model.
    Returns the parsed JSON object from the model's response.
    """
    if not GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is not set; cannot call the consolidator.")

    url = GEMINI_ENDPOINT.format(model=GEMINI_MODEL)
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GOOGLE_API_KEY,
    }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    r = httpx.post(url, headers=headers, json=payload, timeout=120.0)
    r.raise_for_status()
    resp_data = r.json()
    try:
        text = resp_data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as e:
        # Bounded summary only — never dump the raw response into the audit log.
        shape = list(resp_data.keys()) if isinstance(resp_data, dict) else type(resp_data).__name__
        raise ValueError(f"Unexpected Gemini response (status={r.status_code}, keys={shape})") from e

    # Defensive: strip markdown fences if the model wraps the JSON anyway.
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return json.loads(text.strip())

def get_messages_for_date(target_date):
    """
    Fetches all messages and tool executions from state.db for the given date.
    Date should be in YYYY-MM-DD format.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Target date timestamps in UTC. The datetimes must be timezone-aware:
    # .timestamp() on a naive datetime uses the host's LOCAL zone, shifting the
    # day window by the UTC offset (and letting the laptop and mini disagree).
    start_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=1)
    
    start_ts = start_dt.timestamp()
    end_ts = end_dt.timestamp()
    
    query = """
        SELECT m.role, m.content, m.tool_name, m.tool_calls, m.timestamp, s.model, s.source, m.session_id
        FROM messages m
        JOIN sessions s ON m.session_id = s.id
        WHERE m.timestamp >= ? AND m.timestamp < ? AND m.active = 1
        ORDER BY m.timestamp ASC;
    """
    cursor.execute(query, (start_ts, end_ts))
    rows = cursor.fetchall()
    conn.close()
    
    messages = []
    for r in rows:
        messages.append({
            "role": r[0],
            "content": r[1] or "",
            "tool_name": r[2] or "",
            "tool_calls": r[3] or "",
            "timestamp": r[4],
            "model": r[5],
            "source": r[6] or "",
            "session_id": r[7]
        })
    return messages

def clean_messages(raw_messages):
    """
    Deduplicates and cleans raw messages, stripping out heavy tool blocks,
    excessive heartbeat logs, and formatting them into a readable conversation transcript.
    """
    cleaned_transcript = []
    
    # Track which sessions are conversational (have at least one user message)
    session_has_user = {}
    for msg in raw_messages:
        s_id = msg.get("session_id")
        if msg["role"].upper() == "USER":
            session_has_user[s_id] = True

    for msg in raw_messages:
        role = msg["role"].upper()
        content = msg["content"].strip()
        tool_name = msg["tool_name"]
        s_id = msg.get("session_id")
        s_source = msg.get("source", "")
        
        # 1. Exclude Automated/Headless Sessions (0 user messages, e.g. background crons)
        if s_id and not session_has_user.get(s_id):
            continue
            
        # 2. Exclude sessions whose source is explicitly 'cron' to keep context conversational
        if s_source == "cron":
            continue

        # 3. Filter out heartbeat and noise
        if "HEARTBEAT" in content or "HEARTBEAT" in tool_name:
            continue
        if not content and not tool_name:
            continue
            
        # 4. Format roles beautifully
        if role == "USER":
            cleaned_transcript.append(f"Miguel: {content}")
        elif role == "ASSISTANT":
            # Strip tool calls JSON from assistant content to keep transcript compact
            clean_content = re.sub(r"\[tool_call_id=.*?\]", "", content).strip()
            if clean_content:
                cleaned_transcript.append(f"Hermes: {clean_content}")
        elif role == "TOOL":
            # 5. AGGRESSIVE TOOL FILTERING (Ignore non-signal tool outputs)
            # Skip read-only/navigational metadata tools
            low_signal_tools = [
                'session_search', 'skills_list', 'todo', 'cronjob', 'process',
                'mcp_brain_list_kinds', 'mcp_brain_list_task', 'mcp_brain_list_delegated_task'
            ]
            if tool_name in low_signal_tools:
                continue
                
            # Skip verbose read_file on config or lock files
            if tool_name == "read_file":
                if any(k in content for k in ["uv.lock", "package-lock.json", "config.yaml", "processor_state.json", ".env"]):
                    continue
            
            # Skip verbose installation logs, build boilerplate, or directory listing outputs in terminal
            if tool_name in ["terminal", "execute_code"]:
                # If terminal output is pure boilerplate, skip it
                boilerplate_indicators = [
                    "Vite v", "transforming...", "built in", "Resolved", "packages in", 
                    "Successfully synchronized", "Transactions Ignored", "npm warn",
                    "sqlite3.connect", "PRAGMA", "Total transactions in SQLite"
                ]
                if any(ind in content for ind in boilerplate_indicators):
                    continue
                    
                # Skip simple folder listings or path checks
                if content.strip().startswith("Total unique parsed keys") or "Total rows in transactions.csv" in content:
                    continue

            # Limit the body of the remaining tools to keep transcript extremely focused
            if len(content) > 1500:
                content_preview = content[:1500] + "\n... [TRUNCATED FOR CONSOLIDATION] ..."
            else:
                content_preview = content
                
            cleaned_transcript.append(f"Tool Execute ({tool_name}): {content_preview}")
            
    return "\n\n".join(cleaned_transcript)

def call_ai_consolidator(transcript, target_date):
    """
    Calls Gemini to perform the cognitive synthesis of the day (Maker step).
    """
    prompt = f"""
You are the **Thinking Partner** of Miguel Escalante. Your task is to perform the daily cognitive "Dreaming & Consolidating" cycle for the date: {target_date}.

Below is the transcript of today's work, terminal actions, and discussions. You must analyze it deeply and generate a structured Daily Log.

### 📋 Rules of Engagement:
1. **Be Honest & Non-Sycophantic:** Give factual, dry, and highly critical insights. No flattery.
2. **Exclusion Rule:** Exclude press briefs or newsletters. Focus only on Miguel's decisions, code, and conceptual ideas.
3. **Distinguish Execution vs Conceptual:** 
   - Tag work done or code written as `#execution`.
   - Tag abstract plans, brainstorms, or designs that are not yet built as `#conceptual`.

### Transcript of the Day:
```text
{transcript}
```

### Please output a JSON structure with EXACTLY these keys:
{{
    "resumen_operativo": "A 2-3 sentence overview of what was built, decided, or left pending today.",
    "trabajo_tecnico": "Markdown bullets of projects touched and code modified.",
    "negocios": "Business updates (Aluxe, Fratellino, Personal Brand, etc.) if any.",
    "decisiones": "Markdown block in the format: 'Decisión: X — Razón: Y — Impacto: Z'.",
    "insights": [
        {{
            "insight": "Insight description...",
            "target_note": "Name of the relevant existing project note in Obsidian (e.g. 'goes-salud', 'finance', 'boda') or null if none.",
            "evidence": "1-line evidence from transcript.",
            "action": "Next step or monitor [owner, due]"
        }}
    ],
    "proposed_links": [
        {{
            "note_a": "Existing note name in Obsidian",
            "note_b": "Existing note name in Obsidian",
            "evidence": "Why they should be linked...",
            "confidence": "high|medium|low"
        }}
    ],
    "proposed_notes": [
        {{
            "name": "Note name",
            "type": "person|project|topic|ref",
            "slug": "kebab-case-slug"
        }}
    ]
}}
"""
    return call_gemini(prompt)

def verify_and_refine_consolidation(draft_data, transcript, target_date):
    """
    Calls the Checker agent to audit and refine the proposed Daily Log JSON against Vault conventions.
    """
    prompt = f"""
You are the **Verification Agent (Checker)** for Miguel Escalante's daily memory consolidation.
Your sole job is to audit and refine the proposed Daily Log JSON against his strict quality standards and Vault conventions.

### Rules of Engagement for the Checker:
1. **Durable vs Ephemeral:** Ensure every insight is truly durable (facts, decisions, relationships, or reusable insights he would want months from now). Filter out generic or empty insights (like "Miguel continued working on files").
2. **Conciseness & Tone:** Keep explanations concise, dry, and highly technical. Remove any flattery or generic filler words.
3. **No TODOs:** Ensure no task lists or temporary TODO states are recorded as long-term memories.
4. **Validation:** Check if the referenced project notes exist or make sense.

### Proposed Draft Daily Log (from Maker):
```json
{json.dumps(draft_data, indent=2, ensure_ascii=False)}
```

### Raw Transcript Context of the Day:
```text
{transcript}
```

Please review the proposed Daily Log. Filter out any redundant, low-signal, or generic insights. Improve the wording to be dry, direct, and factual. Output the refined JSON structure with the exact same keys:
{{
    "resumen_operativo": "...",
    "trabajo_tecnico": "...",
    "negocios": "...",
    "decisiones": "...",
    "insights": [...],
    "proposed_links": [...],
    "proposed_notes": [...]
}}
"""
    return call_gemini(prompt)

def write_daily_log_to_vault(target_date, data):
    """
    Creates and writes the canonical Daily Log flat in the Obsidian Vault under daily/YYYY-MM-DD.md
    """
    os.makedirs(DAILY_DIR, exist_ok=True)
    daily_file = os.path.join(DAILY_DIR, f"{target_date}.md")
    
    day_name = datetime.strptime(target_date, "%Y-%m-%d").strftime("%A")
    
    # Translate day name to Spanish
    days_es = {
        "Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miércoles",
        "Thursday": "Jueves", "Friday": "Viernes", "Saturday": "Sábado", "Sunday": "Domingo"
    }
    day_es = days_es.get(day_name, day_name)
    
    projects_list = ', '.join([f"[[{i['target_note']}]]" for i in data['insights'] if i.get('target_note')]) or "Ninguno"

    content = f"""---
created: {target_date}
updated: {target_date}
tags: [daily/log]
type: daily
---

# Daily Log: {target_date} ({day_es})

## 📋 Resumen Operativo
{data['resumen_operativo']}

## 🔧 Trabajo Técnico
{data['trabajo_tecnico']}

## 💼 Negocios
{data['negocios'] if data.get('negocios') else "*(No hubo actividad de negocios hoy)*"}

## 🧠 Decisiones y Contexto
{data['decisiones'] if data.get('decisiones') else "*(No se tomaron decisiones de alto nivel hoy)*"}

## 🔗 Conexiones
- **Proyectos:** {projects_list}
- **Skills:** [[memory-consolidation]]
- **Agentes:** [[hermes]] (consolidación)

## 💭 REM

### Cross-links propuestos
"""
    if data.get('proposed_links'):
        for link in data['proposed_links']:
            content += f"- [[{link['note_a']}]] ↔ [[{link['note_b']}]] — \"{link['evidence']}\" — confianza: {link['confidence']}\n"
    else:
        content += "*(No se propusieron nuevos enlaces hoy)*\n"
        
    content += "\n### Notas nuevas propuestas\n"
    if data.get('proposed_notes'):
        for note in data['proposed_notes']:
            content += f"- \"{note['name']}\" — slug sugerido: `{note['slug']}` — tipo: {note['type']}\n"
    else:
        content += "*(No se propusieron notas nuevas hoy)*\n"
        
    with open(daily_file, 'w', encoding='utf-8') as f:
        f.write(content)
        
    print(f"✅ Daily Log written to: {daily_file}")
    return daily_file

_NOTE_TYPE = {"person": NoteType.person, "project": NoteType.project,
              "topic": NoteType.topic, "ref": NoteType.ref}


def emit_proposal(target_date, data):
    """Emit a DreamProposal envelope into the sink (dream-policy.md §4).

    Hermes no longer writes to notes/ directly. It hands the consolidator
    (Claude Code /dream) one envelope per run: insights that name an existing
    note become `appends`; REM output becomes `proposed_links` / `proposed_nodes`.
    The consolidator validates, auto-applies the safe appends (idempotent by the
    `src:hermes:<date>` marker), and routes the rest to the approval gate.

    Multiple insights targeting the same note on the same day are merged into one
    section, preserving the prior one-section-per-note-per-day behaviour.
    """
    date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()
    marker = f"<!-- src:hermes:{target_date} -->"

    # Merge insights by target note.
    by_note = {}
    for item in data.get("insights", []):
        note_name = item.get("target_note")
        if not note_name:
            continue
        by_note.setdefault(note_name, []).append(item)

    appends = []
    for note_name, items in by_note.items():
        lines = []
        for insight in items:
            lines.append(
                f"- **Insight:** {insight.get('insight', '')}\n"
                f"- **Evidencia:** {insight.get('evidence', '')}\n"
                f"- **Acción:** {insight.get('action', '')} (#hermes)"
            )
        appends.append(SectionAppend(
            target_slug=note_name,
            section_date=date_obj,
            content="\n\n".join(lines),
            source_marker=marker,
            # Held for review: the same untrusted-transcript-driven LLM call that
            # wrote this content cannot also vouch for its safety, so Hermes
            # never self-declares durability (fail closed → approval gate).
            durable=False,
            guardrail_hit=policy.is_guardrailed(note_name),
        ))

    proposed_links = [
        LinkProposal(
            note_a=lk["note_a"], note_b=lk["note_b"],
            evidence=lk.get("evidence", ""), confidence=lk.get("confidence", "low"),
        )
        for lk in data.get("proposed_links", [])
    ]
    proposed_nodes = [
        NodeProposal(
            name=n["name"], slug=n["slug"],
            type=_NOTE_TYPE.get(n.get("type", "topic"), NoteType.topic),
        )
        for n in data.get("proposed_notes", [])
    ]

    envelope = DreamProposal(
        agent="Hermes",
        target_date=date_obj,
        policy_version=policy_version,
        appends=appends,
        proposed_links=proposed_links,
        proposed_nodes=proposed_nodes,
    )

    INBOX.mkdir(parents=True, exist_ok=True)
    dest = INBOX / f"hermes-{target_date}.json"
    payload = envelope.model_dump_json(indent=2)
    # Atomic write: temp file on the same filesystem, then os.replace — a crash
    # never leaves a half-written envelope for the consolidator to trip on.
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=str(INBOX))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f"✅ Emitted DreamProposal → {dest} "
          f"({len(appends)} append(s), {len(proposed_links)} link(s), {len(proposed_nodes)} node(s))")

def log_audit(target_date, status="success"):
    """
    Logs the success of the consolidation to local audit json
    """
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
    log_entry = {
        "date": target_date,
        "timestamp": datetime.now().isoformat(),
        "status": status,
        "policy_version": policy_version,
    }
    
    entries = []
    if os.path.exists(AUDIT_LOG_PATH):
        try:
            with open(AUDIT_LOG_PATH, 'r') as f:
                entries = json.load(f)
        except Exception:
            pass
            
    entries.append(log_entry)
    with open(AUDIT_LOG_PATH, 'w') as f:
        json.dump(entries, f, indent=4)

def run_dream_cycle(target_date=None):
    if not target_date:
        # Default to yesterday
        target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        
    print(f"🌙 Starting Hermes dreaming/consolidation cycle (Policy v{policy_version}) for: {target_date}...")
    
    raw_msgs = get_messages_for_date(target_date)
    if not raw_msgs:
        print(f"📭 No conversation signals found in state.db for {target_date}. Skipping.")
        return
        
    transcript = clean_messages(raw_msgs)
    
    # Check if we have the API key
    if not GOOGLE_API_KEY:
        print(f"🔑 No GOOGLE_API_KEY found. Printing cleaned transcript for {target_date} to stdout (Agent-driven mode).")
        print(f"--- START TRANSCRIPT {target_date} ---")
        print(transcript)
        print(f"--- END TRANSCRIPT {target_date} ---")
        return
    
    try:
        print("💡 Maker Step: Generating proposed Daily Log...")
        draft_data = call_ai_consolidator(transcript, target_date)
        
        print("🔍 Checker Step: Auditing and refining the proposed Daily Log...")
        data = verify_and_refine_consolidation(draft_data, transcript, target_date)
        
        write_daily_log_to_vault(target_date, data)
        emit_proposal(target_date, data)
        log_audit(target_date, "success")
        print("🎉 Dreaming cycle successfully completed.")
    except Exception as e:
        print(f"❌ Error during dreaming cycle: {e}", file=sys.stderr)
        log_audit(target_date, f"failed: {str(e)}")
        raise  # surface non-zero exit so launchd / cron alerting fires

if __name__ == "__main__":
    # If a date argument is passed, use it, otherwise run for yesterday (default).
    target = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        run_dream_cycle(target)
    except Exception:
        sys.exit(1)
