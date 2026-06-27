# Mictlán

> *Mictlán* — the Aztec land of the dead, reached by a nine-level journey. Here,
> the nightly journey each day's memories make to reach their durable rest.

The centralized **dreaming engine** — nightly memory consolidation for the whole
agent ecosystem. One engine, many thin per-agent adapters, one Obsidian Vault
(SSOT) served read-only by [`brain-mcp`](../brain-mcp).

Before `mictlan`, the dreaming code was scattered across the vault
(`_system/scripts/`), claude config (`~/.claude/commands/dream.md`), the Mac Mini
(Hermes `dream_cycle.py`), and an OpenClaw bundle — three agents drifting apart,
with the shared policy enforced by hand. `mictlan` is the one home. See
[`docs/adr/0001`](docs/adr/0001-centralize-dreaming-mictlan.md).

## The flow

```
many source-specific dreamers   →   common proposal schema   →   one semantic-dedup
(Claude Code · Hermes · Nico)        (mictlan.schema)            + human-approval gate   →   graph write
   fan-in of PROPOSALS, not of PROCESSING                          (node creation, with you)
```

- **Appends** to an existing note are the only auto-applyable output (durable,
  non-guardrailed, signed).
- **New nodes / links** are always **propose-only** → reconciled by the
  resolution gate → approved by a human once. Mictlán never auto-builds the graph.

## Architecture: engine vs adapter (à la `metate`)

```
mictlan/                 the engine — generic, installed once
├─ policy.py               load + sign the coexistence policy (fail-closed)
├─ ledger.py               sharded dedup ledger (per-host, union reads)
├─ schema.py        ★NEW   the common proposal envelope every dreamer emits
├─ proposals.py     ★NEW   semantic entity-resolution + approval-gate backlog
├─ triage.py              durable / ephemeral / uncertain
├─ analyzer.py            digest prompt branching on session mode
├─ orchestrate.py        prepare / apply proposals
├─ lint.py               proposal lint (wikilink resolvability, dating)
├─ pending.py            aggregate still-pending proposals across journals
├─ reindex.py            regenerate MOCs + sync frontmatter links:
├─ validate.py           schema conformance
└─ stagers/              one per source (claude_code, claude_web, cursor, …)

adapters/                  thin per-agent: discover + parse → emit DreamProposal
├─ claude_code/           (uses skills/dream)
├─ hermes/                dream_cycle.py  (Telegram + finance, Mac Mini)
└─ openclaw/              Nico shim/config (compiled bundle — config only)

skills/dream/              the Claude Code /dream skill (installed to ~/.claude)
docs/adr/                  decision records
tests/
```

**One model across all dreamers:** `gemini-3.5-flash`. OpenClaw may fall back to
other models *only on failure*; Hermes and Claude Code use it exclusively.

## Governance stays in the vault

`mictlan` is engine code; the *rules* remain single files in the vault, served
by brain-MCP and read at every run:

- `dream-policy.md` — coexistence rules (attribution, guardrails, ingest
  boundaries, propose-only). Loaded via `mictlan.policy` (fail-closed).
- `_system/CLAUDE.md` — vault write conventions (doctrine).
- `architecture.md` — the topology map.

Edit the file, bump its version → every agent inherits it on its next run.

## Install / deploy

```bash
make install          # symlink the /dream skill into ~/.claude, install engine (uv)
make deploy-mini      # rsync engine + hermes/openclaw adapters to the Mac Mini
make test             # pytest
```

## Status

v0.1 — scaffolded by copy-first migration (engine copied in; nothing deleted from
old homes yet). Cutover (removing old copies, redeploying the mini) and the graph
reprocess are gated follow-ups — see the ADR.
