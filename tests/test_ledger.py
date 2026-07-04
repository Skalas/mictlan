"""Ledger contract: shards + legacy union reads, atomic shard updates."""
import json

from mictlan import ledger


def _write_ledger_file(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")


def test_ledger_keys_unions_legacy_and_all_shards(tmp_path):
    _write_ledger_file(ledger.legacy_path(tmp_path), {"claude-web:aaaa1111": {}})
    _write_ledger_file(ledger.shard_dir(tmp_path) / "laptop.json", {"claude-web:bbbb2222": {}})
    _write_ledger_file(ledger.shard_dir(tmp_path) / "mini.json", {"claude-jsonl:cccc3333": {}})

    keys = ledger.ledger_keys(tmp_path)

    # A stager reading only the legacy file misses shard entries — the union must not.
    assert keys == {"claude-web:aaaa1111", "claude-web:bbbb2222", "claude-jsonl:cccc3333"}


def test_update_shard_merges_into_existing_shard(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAM_SHARD", "testhost")
    ledger.update_shard(tmp_path, {"claude-web:aaaa1111": {"date": "2026-07-03"}})
    ledger.update_shard(tmp_path, {"claude-web:bbbb2222": {"date": "2026-07-03"}})

    entries = ledger.merged_entries(tmp_path)
    assert set(entries) == {"claude-web:aaaa1111", "claude-web:bbbb2222"}
    # No stray tmp files left behind by the atomic write.
    assert list(ledger.shard_dir(tmp_path).glob("*.tmp")) == []


def test_update_shard_with_no_entries_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAM_SHARD", "testhost")
    ledger.update_shard(tmp_path, {})
    assert not ledger.shard_dir(tmp_path).exists()
