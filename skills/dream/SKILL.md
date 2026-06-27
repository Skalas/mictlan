---
name: dream
description: "Nightly consolidation pass: stage new Claude Code + Claude web sessions, auto-apply safe appends to existing notes, propose new connections, write a dream journal. Designed to run both interactively (manual) and headless (launchd 3am)."
---

You are running the **dream cycle** — the vault's sleep-pass that turns the day's conversations into durable knowledge.

This command works in two modes:

- **Interactive** (user typed `/dream`) — pause on risky decisions, confirm before applying.
- **Headless** (invoked via `claude -p "/dream"` from launchd) — never pause; auto-apply what's safe, park risky decisions in the dream journal for morning review.

Detect mode by inspecting the conversation context: if there's no prior user turn beyond the slash invocation, treat as headless.

## Trust posture (cannot violate)

| Outcome | Headless mode | Interactive mode |
|---|---|---|
| Append dated H2 to an **existing** entity note (target_slug resolves) | **AUTO-APPLY** | AUTO-APPLY (no prompt) |
| Create a **new** note (target_slug didn't exist) | **PROPOSE** (write into dream journal as `proposed_new_notes`) | Ask user |
| Propose a new `[[wikilink]]` between two existing notes | **PROPOSE** | Ask user |
| Archive a conversation triaged as ephemeral | **AUTO-APPLY** | AUTO-APPLY |
| Skip a guardrailed conversation (legal/financial/wedding) | **PARK** in dream journal as `held_for_review` | Ask user |
| Anything that would touch `_system/`, `_index/`, or `.obsidian/` | **REFUSE** | REFUSE |

Never bend these rules to "save a step." A questionable auto-apply is worse than a deferred proposal.

## Step 0a: Load the coexistence policy (FIRST — before anything else)

This command is the **Claude Code instance of the `dream-cycle` instructivo**
(`get_workflow("dream-cycle")`). The steps below are its local implementation;
the shared rules every agent must agree on live in ONE place — the policy — not
in this file. Pull them at run start:

1. Call `mcp__brain__get_consolidation_policy()`. If MCP is unavailable, read
   `_system/dream-policy.md` directly (you're cd'd into the vault by Step 0).
2. Bind from its frontmatter: `policy_version`, `ingest_boundaries["Claude Code"]`,
   `guardrail_slug_prefixes`, `guardrail_slugs_exact`, `protected_paths`,
   `max_stale_days`.
3. **Degradation (cannot violate):**
   - Policy missing / `.icloud` placeholder → **FAIL CLOSED**: abort, write a
     single-line dream journal `policy_unavailable`, exit non-zero.
   - Policy readable but `policy_date` older than `max_stale_days` → proceed
     **PROPOSE-ONLY**: stamp `policy_stale: true` in the journal and downgrade
     every auto-apply that touches shared state to held-for-review.
4. **Attribution is unchanged:** this pipeline's appends keep their existing
   format `## <date> — <heading> <!-- src:<source>:<shortid> -->`. The `src:`
   marker IS Claude Code's attribution (policy §1) — do NOT add an agent
   signature or per-section policy stamp; that would change the digest format.
5. Record `Policy: v<policy_version>` once per run in the Step 7 dream-journal
   `## Summary` (per-run stamp, not per-section).

The guardrail list and protected-path list below are no longer authoritative
here — they come from the policy. This file describes only *how Claude Code's
pipeline runs*; *what all agents must agree on* lives in the policy.

## Step 0: Preflight

```bash
cd "/Users/skalas/Documents/Obsidian Vault"
```

Run these checks. If any fail, abort and write a single-line dream journal explaining why:

1. **Vault accessible**: `test -f _system/CLAUDE.md` — confirms we're in the vault.
2. **No iCloud placeholders in critical paths**:
   ```bash
   find notes daily _system -name '*.icloud' 2>/dev/null | head -5
   ```
   If non-empty, run `brctl download .` and wait 30s, then re-check. If still placeholders exist, abort.
3. **No iCloud conflict files**:
   ```bash
   find . -name '* (Conflicted Copy*' -not -path './.git/*' 2>/dev/null | head -5
   ```
   If non-empty: in interactive mode, surface them and ask the user to resolve before continuing. In headless mode, abort and write the conflict list to the dream journal.
4. **No orphan proposals from prior runs**: list `_system/ingestion/proposed/` and confirm it contains nothing beyond `_manifest.json`, OR that every `<key>.json`/`<key>.prompt.md` present is referenced by the current manifest. Anything else is debris from a crashed prior run.
   ```bash
   python3 -c "
   import json, os
   pdir = '_system/ingestion/proposed'
   m = json.load(open(f'{pdir}/_manifest.json')) if os.path.exists(f'{pdir}/_manifest.json') else {}
   tracked = {os.path.basename(e['proposal_path']) for e in m.values()} | {os.path.basename(e['prompt_path']) for e in m.values()} | {'_manifest.json'}
   present = set(os.listdir(pdir)) if os.path.isdir(pdir) else set()
   orphans = present - tracked
   print('orphans:', sorted(orphans) if orphans else 'none')
   "
   ```
   If orphans exist: in headless mode park as `prior_run_pending` and exit early; in interactive mode ask whether to apply (if they correspond to ledger-pending keys) or delete (if they don't).

## Step 1: Stage new sources

Pull new conversations from each source. Each stager is idempotent and uses `_system/ingestion/processed.json` + the bootstrap ledger to skip what it's already seen — **but the scanners still walk file metadata for everything in scope**, so without a cutoff a full-history scan can take minutes and waste CPU. Always cap the lookback for nightly runs.

Default lookback window:

- `SINCE` = today − 7 days (ISO date). Overlap with prior runs is fine; the ledger dedups.
- `LIMIT` = 200 (safety cap).

```bash
SINCE=$(date -v-7d +%Y-%m-%d)
uv run --project ~/github/skalas/mictlan python -m mictlan.stagers.claude_code --since "$SINCE" --limit 200
uv run --project ~/github/skalas/mictlan --with playwright python -m mictlan.stagers.fetch_claude_web --since "$SINCE" --limit 200
uv run --project ~/github/skalas/mictlan python -m mictlan.stagers.cursor --since "$SINCE" --limit 200
```

Cursor sessions (`~/.cursor/projects/*/agent-transcripts/`) stage into `staging/cursor-code/` with source `cursor-jsonl`. Same downstream pipeline as Claude Code — pure parsing, no LLM calls or vault writes. The stager is idempotent against the same `processed.json` ledger.

The web side now uses `fetch_claude_web.py` — a Playwright + cookie-persisted scraper that pulls directly from claude.ai. No manual export needed. Cookies live at `_system/ingestion/.claude-web-cookies.json`; if they've expired the script exits non-zero and you must run a one-time interactive `--login` (interactive mode: prompt the user; headless: skip web and note `claude_web_login_required` in the dream journal).

The legacy export-based stager (`stage_claude_web.py --src <path>`) is still available for one-off backfills from a downloaded export, but it's no longer the default — fetch is.

For first-time / bootstrap runs (backlog larger than 7 days), the user invokes `/dream` with an explicit window — e.g., "`/dream since=2026-04-01`" — and you pass that through as `--since`. Never default to "no cutoff."

Capture the counts of newly-staged items per source (code, web, cursor). If all three return zero, you can skip Steps 2–5 and jump to the REM pass (Step 6) — there may still be unlinked entities to surface from earlier appends.

## Step 1.5: Archive subagent transcripts

Claude Code spawns subagent sessions via the Task tool. Their JSONL files land in staging with `agent-*` filenames, but **their synthesis already lives in the parent conversation** — digesting them as standalone H2 entries adds noise. Mark them as archived in the ledger so the digest pipeline skips them.

```bash
uv run --project ~/github/skalas/mictlan python -m mictlan.stagers.archive_subagent_jsonl --confirm
```

This is idempotent (only NEW `agent-*` files get added to the ledger). Capture the count of newly-archived entries; report it in the dream journal under `Skipped (ephemeral)` → `subagent transcripts: <N>`.

This step is **claude-code only**. Cursor also spawns subagents (`agent-transcripts/<id>/subagents/`), but `stage_cursor.py` excludes those at discovery time, so they never reach staging — no archive pass needed for Cursor.

After this step, `orchestrate_digest.py status` should report the **real** pending count (parent sessions only). If pending is still > 50, scope down: use `orchestrate_digest.py prepare --limit 20` and process the rest on subsequent nights.

## Step 2: Triage

```bash
uv run --project ~/github/skalas/mictlan python -m mictlan.triage
```

This classifies staged conversations into `durable`, `ephemeral`, and `uncertain`. Capture the lists; you'll need them for Steps 3 and 7.

## Step 3: Slow-wave consolidation (auto-apply safe, propose risky)

For every `durable` conversation, run the existing digest preparation step but **with no batch-size cap** — we want all of tonight's durable content proposed in parallel:

```bash
uv run --project ~/github/skalas/mictlan python -m mictlan.orchestrate prepare --limit 999
cat _system/ingestion/proposed/_manifest.json
```

For each `<key>` in the manifest, dispatch a subagent (general-purpose) **in parallel** with the same instructions as `/digest`'s Step 3: read the prompt file, return ONLY a JSON object matching the schema, no prose.

After all subagents return, classify each proposal:

```
proposal is SAFE if all of:
  - target_slug exists at notes/<target_slug>.md
  - skip_reason == "none"
  - target_slug is NOT guardrailed per the policy (Step 0a): slug matches no `guardrail_slugs_exact` and starts with no `guardrail_slug_prefixes`
  - no existing H2 with the same date in the target note (avoid double-dating)

proposal is RISKY otherwise (creates a new note, has skip_reason, hits a guardrail).
```

For SAFE proposals: write the JSON to `_system/ingestion/proposed/<key>.json` and **mark for auto-apply**.

For RISKY proposals: write the JSON to `_system/ingestion/proposed/<key>.json` and **mark for journal**.

## Step 4: Apply safe proposals

Apply **only** the safe keys via `apply --only` (comma-separated). Risky keys stay in `proposed/` and in the manifest, untouched — no file-shuffling. `--only` scopes both the pre-flight lint and the apply loop to the named keys, so held-aside proposals don't trip "proposal JSON missing", and the final manifest write-back preserves them.

Dry-run first:

```bash
SAFE="claude-jsonl:<safe1>,claude-jsonl:<safe2>"  # comma-separated, no spaces
uv run --project ~/github/skalas/mictlan python -m mictlan.orchestrate apply --only "$SAFE"
```

Inspect the `[dry] …` output. If anything looks wrong (unexpected creates, missing appends), in interactive mode pause; in headless mode abort and write the dry-run output verbatim into the dream journal under `errors`. Do not retry blindly.

If the dry-run looks correct:

```bash
uv run --project ~/github/skalas/mictlan python -m mictlan.orchestrate apply --only "$SAFE" --confirm
```

This mutates `notes/`, updates `_system/ingestion/processed.json`, and runs reindex. Applied keys self-clean (their `.json`/`.prompt.md`/staging files are removed and they drop out of the manifest); the risky keys remain for the journal record and morning review.

> **Lint note.** An append whose `[[wikilink]]` points at a note created in the *same* proposal's `creates[]` now resolves correctly (no false ERROR). A genuinely broken wikilink (target neither in the vault nor in this proposal's creates) still blocks — use `--lint-warn-only` only when you've confirmed the linter itself is wrong.

**Post-apply assertion.** After `apply --only "$SAFE" --confirm`, `_system/ingestion/proposed/` should contain `_manifest.json` plus only the **risky** keys you deliberately held back (their `.json`/`.prompt.md`), and the manifest should list exactly those risky keys. If anything *else* remains (a safe key that should have self-cleaned), the orchestrator's self-cleanup regressed — record the leftover filenames verbatim in the dream journal under `errors` and do not claim a clean run.

```bash
ls _system/ingestion/proposed/ | grep -v '^_manifest.json$' || echo "clean"
```

## Step 5: Archive ephemeral conversations

For each conversation triaged as `ephemeral`:

```bash
# in interactive: just add a frontmatter flag, don't move (user might disagree)
# in headless: same — flagging is reversible, moving is not
```

Set frontmatter `status: archived` on each ephemeral conversation file via direct Edit. Bump `updated:` to today. Reindex picks it up on the final pass.

## Step 6: REM — cross-link recombination (PROPOSE ONLY)

This is the creative-recombination pass. It never writes to entity notes — it only produces proposals for the dream journal.

For each note touched today (from Step 4's apply output + any manual edits since yesterday's dream):

1. Extract the entities mentioned in today's new H2 section (look for `[[wikilink]]` syntax and capitalized noun phrases that look like names/orgs).
2. For each pair `(touched_note, mentioned_entity)`:
   - If `mentioned_entity` already exists as a note AND it's not already in `touched_note.frontmatter.links` → candidate **new link**.
   - If `mentioned_entity` doesn't exist as any note AND has been seen ≥ 3 times across recent conversations → candidate **new note stub**.
3. Use the brain MCP `search_notes` tool to verify existence (handles aliases that grep would miss).

Collect candidates as two lists: `proposed_links` and `proposed_new_notes`. Do not apply anything in this step.

**v1 limitation worth flagging in the journal**: this pass uses keyword matching. Connections that need semantic understanding (e.g., "Diego's new responsibilities sound adjacent to project X even though they share no keywords") will be missed until the vector DB lands.

## Step 6.5: Backlog review (INTERACTIVE ONLY — the approval gate)

Headless dream can only *propose* new nodes (Step 0a trust posture); it never
creates them. Without a recurring human pass, those proposals pile up unactioned
in old journals forever. This step is that pass — **run it only in interactive
mode** (headless skips it entirely and keeps parking).

It surfaces the FULL accumulated backlog — every still-pending proposal across
all journal history, not just tonight's — so you approve it all in one place.

1. Aggregate every pending proposal (drops anything already covered by a real note):
   ```bash
   uv run --project ~/github/skalas/mictlan python -m mictlan.pending
   ```
   This walks all `dreams/*.md`, harvests candidate slugs from `Held for review`
   + `Proposed new note stubs`, and removes any that now resolve to a note
   (direct slug, de-hyphenated, or `<slug>-cafe` alias). Output is JSON:
   `{pending_count, pending: [{slug, type, times_seen, first_seen, last_seen}]}`.

2. Present the list to the user as a single batch, highest `times_seen` first.
   For each candidate, offer: **create** (new stub) · **fold** (into an existing
   note as a section) · **reject** (it's covered/noise — note which existing note
   covers it) · **defer** (still a watch-item, leave it).

3. Apply the user's decisions:
   - **create** → write `notes/<slug>.md` with full frontmatter per the doctrine
     (`get_doctrine()`): `id`, `type`, `created`/`updated` = today, `status: active`,
     a 2–4 sentence grounded evergreen summary, and `[[wikilinks]]` to related
     entities. Ground the summary in actual vault mentions — never fabricate.
   - **fold** → append a dated H2 section to the chosen existing note, bump `updated:`.
   - **reject** / **defer** → no write; record the disposition in the journal.

4. After any writes, run `uv run --project ~/github/skalas/mictlan python -m mictlan.reindex` once (Step 8 can be skipped
   if this already ran it).

5. Once the other agents' shims land on the Mac Mini, their proposal surfaces
   join here too — this step is the single cross-agent approval gate, by design
   replacing any separate weekly review.

Record the outcome in the journal (Step 7) under a new `## Backlog review`
section: how many were pending, and the create/fold/reject/defer disposition of each.

## Step 7: Write the dream journal

Write to `dreams/<YYYY-MM-DD>.md` (create the `dreams/` folder if it doesn't exist). Use the Write tool directly — the brain MCP doesn't yet have a `dream` kind.

Schema:

```markdown
---
id: <YYYY-MM-DD>
type: dream
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
status: active
mode: interactive | headless
---

# Dream — <YYYY-MM-DD>

## Summary
- Staged: <N> from claude-code, <M> from claude-web, <K> from cursor
- Durable: <N>, ephemeral: <N>, uncertain: <N>
- Auto-applied appends: <N>
- Held for review: <N>
- Errors: <N>

## Auto-applied
For each safe proposal that was applied:
- [[<target_slug>]] — `<section_date>` — <first 140 chars of content>

## Held for review
For each risky proposal:
- `<key>` → would create [[<slug>]] / hits guardrail / has skip_reason="<reason>"
  - first 200 chars of proposed content
  - Decision needed: apply | edit | reject

## Backlog review (interactive only)
Pending at start: <N>. For each candidate decided this pass:
- `<slug>` (seen <N>× since <date>) → created [[<slug>]] | folded into [[<note>]] | rejected (covered by [[<note>]]) | deferred

## Proposed new links (REM)
For each candidate cross-link:
- [[<note-a>]] ↔ [[<note-b>]] — saw "<entity>" mentioned in both today
  - Confidence: low | medium (string match only — no semantic check)

## Proposed new note stubs (REM)
For each repeated unlinked entity:
- "<entity name>" — seen <N> times in <M> conversations since <date>
  - Suggested slug: `<kebab-slug>`
  - Suggested type: person | project | topic | ref

## Errors
Any tracebacks or unexpected output from Steps 0–6. Verbatim.

## Skipped (ephemeral)
List of conversation IDs archived this run.
```

Spanish/English: match the language of the source content where it makes sense. Frontmatter values stay English. The journal is mostly for you-the-user — write it as you'd want to read it over coffee.

## Step 8: Reindex

The `apply --confirm` in Step 4 already ran reindex, but if Step 5 or Step 6 touched files (status flips, journal write), run it again to keep MOCs current:

```bash
uv run --project ~/github/skalas/mictlan python -m mictlan.reindex
```

## Step 9: Commit (interactive only — never headless)

In interactive mode, surface a `git status` + `git diff --stat` and ask whether to commit. Standard commit message:

```
dream: <date> — <N> auto-applied, <M> held for review

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

In **headless mode, do NOT commit.** The morning review is when commit decisions happen. Leave the working tree dirty; the dream journal is the audit trail.

## Step 10: Exit summary

Print a one-line summary to stdout (matters for launchd's StandardOutPath log):

```
DREAM <YYYY-MM-DD>: staged=<N>, durable=<N>, applied=<N>, held=<N>, errors=<N> → dreams/<date>.md
```

## Failure modes — known pitfalls

- **Subagent returns prose instead of JSON.** Strip ```json``` fences; if still not parseable, mark that key as an error in the journal and continue with the rest.
- **Two conversations target the same note + same date.** Merge their content into one H2 before applying (order: longer-content first). Note this in the journal under `Auto-applied`.
- **`stage_*.py` partial failure.** If one source fails but the other succeeds, continue with what staged successfully. Don't abort the whole dream over one bad fetch.
- **Reindex script fails.** This is rare but blocking. Write the stderr to the dream journal under `errors` and exit non-zero so launchd's log surfaces it.
- **You drift from these instructions.** Don't. The dream cycle is supposed to be *boring*. Surprises here become silent corruption in the brain.
