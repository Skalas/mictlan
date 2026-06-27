"""Source stagers — one per ingestion source.

Each stager discovers and parses one source into the staging area, idempotently,
using the shared sharded dedup ledger. Stagers do NOT call the LLM and do NOT
write to the vault — they only produce clean transcripts/records for the engine.
"""
