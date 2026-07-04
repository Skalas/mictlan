# Mictlán

![Mictlán: the dreaming engine](docs/assets/banner.png)

> *Mictlán* — the Aztec land of the dead, reached by a nine-level journey. Here,
> the nightly journey each day's memories make to reach their durable rest.

The centralized **dreaming engine** — nightly memory consolidation for the whole
agent ecosystem. One engine, many thin per-agent adapters, one Obsidian Vault
(SSOT) served read-only by [`brain-mcp`](https://github.com/Skalas/brain-mcp).

Before `mictlan`, the dreaming code was scattered across the vault
(`_system/scripts/`), claude config (`~/.claude/commands/dream.md`), the Mac Mini
(Hermes `dream_cycle.py`), and an OpenClaw bundle — three agents drifting apart,
with the shared policy enforced by hand. `mictlan` is the one home. See
[`docs/adr/0001`](docs/adr/0001-centralize-dreaming-mictlan.md).

## The flow

```
many source-specific dreamers   →   common proposal schema   →   one semantic-dedup
(Claude Code · Hermes)               (mictlan.schema)            + human-approval gate   →   graph write
   fan-in of PROPOSALS, not of PROCESSING                          (node creation, with you)
```

- **Appends** to an existing note are the only auto-applyable output — and only
  when the envelope affirmatively marks them `durable` (the schema default is
  `False`: fail closed, held for human review).
- **New nodes / links** are always **propose-only** → reconciled by the
  resolution gate → approved by a human once. Mictlán never auto-builds the graph.
- **Exemption — `daily/`:** Hermes writes its own daily log
  (`daily/<date>.md`) directly. It's a Hermes-owned namespace of dated
  operational summaries, not graph knowledge, so it sits deliberately outside
  the single-writer contract. Everything else from Hermes goes through the sink.

## Architecture: engine vs adapter (à la `metate`)

```
mictlan/                 the engine — generic, installed once
├─ paths.py                single source of vault path resolution (MICTLAN_VAULT)
├─ policy.py               load the coexistence policy (fail-closed) + attribution headings
├─ ledger.py               sharded dedup ledger (per-host, union reads)
├─ schema.py               the common proposal envelope every dreamer emits
├─ proposals.py            semantic entity-resolution + approval-gate backlog
├─ inbox.py                drain the sink: validate, bridge safe appends, archive
├─ triage.py              standalone tag-cluster report over conversations/ (not in the pipeline)
├─ analyzer.py            vault I/O + idempotent apply path + digest prompts
├─ orchestrate.py        prepare / apply proposals
├─ lint.py               proposal lint (wikilink resolvability, dating)
├─ pending.py            aggregate still-pending proposals across journals
├─ reindex.py            regenerate MOCs + sync frontmatter links:
├─ validate.py           schema conformance
└─ stagers/              one per source (claude_code, claude_web, cursor, …)

adapters/                  thin per-agent: discover + parse → emit DreamProposal
├─ claude_code/           (uses skills/dream)
└─ hermes/                dream_cycle.py  (Telegram, Mac Mini) → emits to the sink

skills/dream/              the Claude Code /dream skill (installed to ~/.claude)
docs/adr/                  decision records
tests/
```

**One model across all dreamers:** `gemini-3.5-flash`, used exclusively by Hermes
and Claude Code. (OpenClaw/Nico was retired 2026-06-27; the fleet is now those two.)

## Governance stays in the vault

`mictlan` is engine code; the *rules* remain single files in the vault, served
by brain-MCP and read at every run:

- `dream-policy.md` — the single-consolidator contract (provenance, guardrails,
  ingest boundaries, sink, propose-only). Loaded via `mictlan.policy` (fail-closed).
- `_system/CLAUDE.md` — vault write conventions (doctrine).
- `architecture.md` — the topology map.

Edit the file, bump its version → every agent inherits it on its next run.

## Installation

Prereqs: [`uv`](https://docs.astral.sh/uv/) and `git`. Set `MICTLAN_VAULT` if your
vault isn't at `~/Documents/Obsidian Vault`.

### One line (humans)

```bash
git clone git@github.com:Skalas/mictlan.git ~/github/skalas/mictlan && ~/github/skalas/mictlan/install.sh
```

Installs the engine (`uv sync`) and links the `/dream` skill into Claude Code. Then type `/dream`.

### Per agent harness

Each harness installs the same engine; only the entry point differs.

**Claude Code** (laptop) — engine + the `/dream` skill:

```bash
cd ~/github/skalas/mictlan && make install
```

Symlinks `~/.claude/commands/dream.md` → the repo (repo stays the source of truth). The skill's steps call the engine as `uv run --project ~/github/skalas/mictlan python -m mictlan.*`.

**Hermes** (Mac Mini) — its `dream_cycle.py` imports `mictlan`, so the package must be installed on the mini:

```bash
# on the Mac Mini, first time:
git clone git@github.com:Skalas/mictlan.git ~/github/skalas/mictlan
cd ~/github/skalas/mictlan && make install-hermes
```

Runs `uv sync` and symlinks `~/.hermes/scripts/dream_cycle.py` → the repo adapter. The nightly runner must invoke it through the project env:

```bash
uv run --project ~/github/skalas/mictlan python ~/.hermes/scripts/dream_cycle.py [YYYY-MM-DD]
```

> OpenClaw / Nico was retired on 2026-06-27 and is no longer part of the fleet.

### Updating the mini from the laptop

```bash
make deploy-mini      # ssh the mini → git pull → make install-hermes
make test             # uv run --extra dev pytest
```

## Status

v0.2 — single-consolidator cutover landed: Hermes emits `DreamProposal` envelopes
to the sink (Claude Code `/dream` is the sole vault writer, with the documented
`daily/` exemption above), the duplicate vault policy loader is deleted, and
OpenClaw/Nico is retired. The graph reprocess is a gated follow-up, tracked in
ADR 0001's follow-ups section (ADR 0002 to be written when it's picked up).
