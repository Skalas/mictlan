---
id: 0001-centralize-dreaming-mictlan
type: ref
tags: [adr, architecture, dreaming, mictlan, brain]
created: 2026-06-27
updated: 2026-06-27
status: active
context: personal
links: []
---

# ADR 0001 — Centralize the dreaming engine into a single repo (`mictlan`)

**Status:** Accepted (2026-06-27)
**Deciders:** Miguel Escalante
**Supersedes:** the scattered per-agent dreaming code (vault `_system/scripts/`, `~/.claude/commands/dream.md`, Hermes `~/.hermes/scripts/dream_cycle.py`, OpenClaw compiled bundle)

## Context

The "dreaming" (nightly memory consolidation) ecosystem has three independent
writer agents over one Obsidian Vault (SSOT), served read-only by **brain-MCP**:

- **Claude Code** `/dream` (laptop) — ingests Claude Code / Claude.ai / Cursor.
- **OpenClaw / Nico** (Mac Mini) — ingests OpenClaw assistant sessions.
- **Hermes** (Mac Mini) — ingests Telegram dialogue + `finance.db`.

The design is sound at the *governance* layer: one policy (`dream-policy.md`),
one doctrine (`_system/CLAUDE.md`), one architecture map — each is a single file
served by one brain tool, so agents inherit changes without redeploys.

But the **engine code is scattered and divergent**, and the architecture map
already flags it as the top gap: *"Nico and Hermes shims … must be applied ON
THE MAC MINI (their code isn't on the laptop). Until then, those two agents
still run their old duplicated rules."* Concretely, today:

- The stagers, ledger, analyzer, orchestrator, and `mictlan_policy.py` live in
  the **vault** (`_system/scripts/`) — content storage doubling as code home.
- The `/dream` procedure lives in **claude config** (`~/.claude/commands/`).
- Hermes' `dream_cycle.py` is a **separate copy** on the Mac Mini that, until
  2026-06-27, even preferred a different LLM (GPT) for the same job.
- OpenClaw's dreaming is a **compiled bundle** with no shared source.

Symptoms of the drift: model assignment differed per agent (GPT vs Gemini), a
leaked API key surfaced in a Hermes audit log, cross-agent linking is keyword-only
(no semantic dedup), and there is no shared *proposal contract* — each agent
writes bespoke markdown that never reconciles.

## Decision

Create a single repository **`mictlan`** (`github/skalas/mictlan`), a sibling
to `brain-mcp`, as the **one home for the dreaming engine, flow, and skills**.
Modeled on `metate`'s engine-vs-profile split:

- **Engine (generic, installed once)** — `mictlan/` Python package: the policy
  loader, sharded dedup ledger, stagers, analyzer, orchestrator, vault writer
  (append-only / signed / guardrailed), reindex, and two **new** pieces:
  1. a **common proposal schema** — the envelope every agent's dream emits
     (`appends`, `proposed_nodes`, `proposed_links`, `entities`), and
  2. a **semantic entity-resolution gate** — dedups proposed nodes against the
     existing graph via brain-MCP `search_semantic` before any node is created.
- **Adapters (thin, per-agent)** — `adapters/{claude_code,hermes,openclaw}/`:
  source discovery + parse → emit the common schema. Nothing else is duplicated.
- **Skill** — `skills/dream/` (the Claude Code `/dream` interface).

Dreaming becomes: **many source-specific dreamers → one common proposal schema →
one semantic-dedup + human-approval gate → graph write.** Fan-in of *proposals*,
not of *processing*. Node creation stays **propose-only** with a single human
approval gate (policy §5) — `mictlan` does NOT adopt auto-graph construction.

The vault keeps governance docs (policy/doctrine/architecture) as SSOT; the
engine *reads* them via brain-MCP. Single model across all dreamers:
`gemini-3.5-flash` (OpenClaw may fall back to other models only on failure).

## What moves where (cutover — staged, after repo validation)

| Lives today in | Moves to | Note |
|---|---|---|
| vault `_system/scripts/*.py` (engine) | `mictlan/` package | vault keeps only `dream-policy.md`, doctrine, MOC scripts as needed |
| `~/.claude/commands/dream.md` | `mictlan/skills/dream/` | installed via `install.sh`; **leaves claude config / kits** |
| Hermes `~/.hermes/scripts/dream_cycle.py` | `adapters/hermes/` | deployed to the mini from the repo |
| OpenClaw dreaming config/shim | `adapters/openclaw/` | shim documented; engine bundle stays vendor |
| `claude-kits` dreaming refs | removed | dreaming is no longer a kit concern |

Cutover is **copy-first, delete-later**: the engine is copied into `mictlan`
and validated before anything is removed from its old home, so nightly dreams
never break mid-migration.

## Consequences

**Positive**
- One source of truth for dreaming code → the policy stops being enforced by hand.
- A common proposal schema makes the three agents reconcilable for the first time.
- Semantic dedup before node creation directly improves graph coherence.
- Versioned, testable, deployable to both machines (laptop + mini) from git.
- Removes dreaming from `claude-kits` / claude config — kits stay about team
  defaults, not personal memory machinery.

**Negative / risks**
- Two-machine deployment to coordinate (laptop + Mac Mini) — handled by `install.sh` + `Makefile` deploy targets.
- OpenClaw's dreaming is a compiled bundle; `mictlan` can only own its *config/shim*, not its source, until OpenClaw exposes a plugin seam.
- Migration touches working nightly jobs → mitigated by copy-first/delete-later and per-agent validation.

## Follow-ups (separate, gated work)
1. **Reprocess the existing graph** to enhance it (semantic dedup of existing
   nodes, typed edges, area hubs). Must run **dry-run + human approval** — it is a
   bulk mutation and falls under the propose-only philosophy. See ADR 0002 (TBD).
2. **Areas-of-life model**: add an orthogonal `area:` axis (work / restaurants /
   wedding / personal / rnd / health / finance) + per-area hub notes + `reindex`
   rollups. Distinct from `context:` (sensitivity). See ADR 0002 (TBD).
3. **Typed relationships** (`relations:` frontmatter) where they pay off
   (org-charts, wedding vendors, restaurant attributes).

## Alternatives considered

- **One monolithic ingester** (single process consuming all sources). Rejected:
  different sources need different parsers; one process across two machines is a
  sync bottleneck and a single point of failure.
- **Adopt Cognee** as the memory backend (auto-extract entities/relationships
  into a graph DB + vectors). Rejected as a wholesale dependency: its
  auto-construction suits throwaway RAG, not a brain read years later, where a
  wrong auto-node is debt. Its *lessons* (typed graph, content-hash incremental
  ingest, hybrid retrieval) are adopted selectively; the brain already provides
  graph+vector serving via brain-MCP.
- **Leave code in the vault `_system/scripts/`.** Rejected: conflates content
  storage (iCloud, not git) with engine code (needs versioning, tests, CI, and
  deployment to a second machine).
