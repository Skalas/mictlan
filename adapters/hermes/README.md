# Hermes adapter

Dreams over Miguel's **Telegram dialogue + finance** (`~/.hermes/state.db`,
`finance.db`). Runs on the Mac Mini.

- Entry: `dream_cycle.py` (deployed to `~/.hermes/scripts/` via `make deploy-mini`).
- Model: **`gemini-3.5-flash` only** (no GPT path). Single transport `call_gemini()`.
- Key auth via `x-goog-api-key` header (never in the URL — avoids leaking into logs).
- Egress over IPv4 so the IP-allowlisted Google key works (OpenClaw already does).
- Loads the coexistence policy fail-closed before any write (`mictlan.policy`).

TODO (cutover): refactor `dream_cycle.py` to import `mictlan.policy`,
`mictlan.ledger`, and emit `mictlan.schema.DreamProposal` instead of writing
markdown directly, so it shares the one engine.
