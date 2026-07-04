# Hermes adapter

Dreams over Miguel's **Telegram dialogue + finance** (`~/.hermes/state.db`,
`finance.db`). Runs on the Mac Mini.

- Entry: `dream_cycle.py` (deployed to `~/.hermes/scripts/` via `make deploy-mini`).
- Model: **`gemini-3.5-flash` only** (no GPT path). Single transport `call_gemini()`.
- Key auth via `x-goog-api-key` header (never in the URL — avoids leaking into logs).
- Egress over IPv4 so the IP-allowlisted Google key works (OpenClaw already does).
- Loads the coexistence policy fail-closed at the start of EVERY run (never at
  import — the gateway is long-lived, and a version bump in the vault must take
  effect without a restart).
- Emits a `mictlan.schema.DreamProposal` envelope to the sink; appends are never
  self-declared durable, so everything Hermes proposes is held at the human
  approval gate.
- **Deliberate exemption:** the daily log (`daily/<date>.md`) is written
  directly — a Hermes-owned namespace of dated operational summaries outside the
  single-writer contract (see the root README, "The flow").
- Hermes never touches the dedup ledger; only the consolidator's apply step
  (laptop) writes ledger shards.
