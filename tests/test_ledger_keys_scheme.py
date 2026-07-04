"""The stagers' skip-keys and the orchestrator's ledger keys must agree — a
mismatch means already-applied conversations get re-staged forever (the exact
claude-web regression fixed in #12).
"""
from mictlan.orchestrate import ledger_key


def test_claude_web_stager_skip_key_matches_orchestrator():
    uuid = "5b3a9c01-1234-5678-9abc-def012345678"
    # claude_web.py / fetch_claude_web.py skip with source_id[:8] (uuids have no
    # hyphen before position 8, so this equals the orchestrator's normalization)
    assert ledger_key("claude-web", uuid) == f"claude-web:{uuid[:8]}"


def test_claude_jsonl_agent_ids_keep_disambiguating_length():
    assert ledger_key("claude-jsonl", "agent-a100c8d026ec34ca1") == "claude-jsonl:agent-a100c8d026"
    assert ledger_key("claude-jsonl", "00102f5b-aaaa-bbbb") == "claude-jsonl:00102f5b"


def test_cursor_key_is_first_8():
    assert ledger_key("cursor-jsonl", "25075591-aa74-40ba") == "cursor-jsonl:25075591"
