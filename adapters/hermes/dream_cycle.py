import os
import sys
import sqlite3
import json
import re
from datetime import datetime, timedelta
import httpx

# Configuration
DB_PATH = os.path.expanduser("~/.hermes/state.db")
VAULT_PATH = os.path.expanduser("~/Documents/Obsidian Vault")
DAILY_DIR = os.path.join(VAULT_PATH, "daily")
NOTES_DIR = os.path.join(VAULT_PATH, "notes")
AUDIT_LOG_PATH = os.path.expanduser("~/.hermes/logs/dream_audit.json")

# Single model for the whole agent fleet (Hermes + OpenClaw share one key, one model).
GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# --- 0. Policy Loading Gate (FAIL CLOSED) ---
# Policy loader comes from the shared engine (requires `mictlan` installed here).
try:
    from mictlan.policy import load_policy, sign, PolicyUnavailable
    policy = load_policy()
    policy_version = policy.version
    heading_signature = policy._d["heading_signature"]
    ingest_boundary_hermes = policy.boundary("Hermes") # usually ["~/.hermes/"]
    protected_paths = policy._d.get("protected_paths", ["_system/", "_index/", ".obsidian/"])
    max_stale_days = policy.max_stale_days
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

# Degradation handling: check if policy is stale
POLICY_STALE = policy.is_stale # If stale, we proceed in propose-only mode (marked in state.db)

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
        raise ValueError(f"Unexpected response structure from Gemini API: {resp_data}") from e

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
    
    # Target date timestamps in UTC
    start_dt = datetime.strptime(target_date, "%Y-%m-%d")
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

def apply_deep_promotions(target_date, insights):
    """
    Durable promotion: appends a H2 dated section to existing Obsidian project notes
    """
    if POLICY_STALE:
        print("⚠️ Coexistence policy is STALE (max_stale_days threshold exceeded). PROPOSE-ONLY mode active. Skipping auto-appends.")
        for item in insights:
            note_name = item.get("target_note")
            if note_name:
                print(f"  [PROPOSED APPEND for [[{note_name}]]: {item['insight']}")
        return

    for item in insights:
        note_name = item.get("target_note")
        if not note_name:
            continue
            
        # Check if the note exists in Vault
        possible_paths = [
            os.path.join(NOTES_DIR, f"{note_name}.md"),
            os.path.join(NOTES_DIR, f"project-{note_name}.md"),
            os.path.join(NOTES_DIR, f"topic-{note_name}.md"),
            os.path.join(NOTES_DIR, f"person-{note_name}.md"),
        ]
        
        target_path = None
        for path in possible_paths:
            if os.path.exists(path):
                target_path = path
                break
                
        if not target_path:
            # Let's see if we can find any note matching this file name in the Vault
            for root, dirs, files in os.walk(VAULT_PATH):
                for file in files:
                    if file.lower() == f"{note_name.lower()}.md":
                        target_path = os.path.join(root, file)
                        break
                if target_path:
                    break
                    
        if target_path:
            # Check protected paths guardrail (REFUSE)
            rel_path = os.path.relpath(target_path, VAULT_PATH)
            if any(rel_path.startswith(p) for p in protected_paths):
                print(f"🚫 REFUSE: Target path {rel_path} resides inside a protected directory. Action blocked.")
                continue

            # Append section safely
            with open(target_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            sig_block = sign(policy, "Hermes", target_date)
                
            # Check if this agent's section for the target date already exists
            short_sig = f"## {target_date} — Hermes"
            if short_sig in content or sig_block in content:
                print(f"ℹ️ Section {short_sig} already exists in [[{note_name}]]. Skipping append.")
                continue
                
            append_block = f"\n\n{sig_block}\n- **Insight:** {item['insight']}\n- **Evidencia:** {item['evidence']}\n- **Acción:** {item['action']} (#hermes)"
            
            with open(target_path, 'a', encoding='utf-8') as f:
                f.write(append_block)
                
            print(f"✅ Appended dated insight to [[{note_name}]] at {target_path}")
        else:
            print(f"⚠️ Warning: Could not find note [[{note_name}]] in Vault to promote insight.")

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
        "policy_stale": POLICY_STALE
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
        apply_deep_promotions(target_date, data.get("insights", []))
        log_audit(target_date, "success")
        print("🎉 Dreaming cycle successfully completed.")
    except Exception as e:
        print(f"❌ Error during dreaming cycle: {e}", file=sys.stderr)
        log_audit(target_date, f"failed: {str(e)}")

if __name__ == "__main__":
    # If a date argument is passed, use it, otherwise run for today
    target = sys.argv[1] if len(sys.argv) > 1 else None
    run_dream_cycle(target)
