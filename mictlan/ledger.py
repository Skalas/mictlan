#!/usr/bin/env python3
"""Sharded dedup ledger for the consolidation pipeline.

Historically a single `_system/ingestion/processed.json` was read-modify-written
whole. On an iCloud-synced vault, two machines running the pipeline can clobber
each other's batch (last-writer-wins on the whole file). Fix: each machine writes
ONLY its own shard at `_system/ingestion/processed/<host>.json`; readers union
all shards plus the legacy file. No locks, no coordination — sharding removes the
shared writable object entirely.

Backward compatible: the legacy `processed.json` is still READ (never written
again), so historical dedup entries keep counting. To migrate it into a shard,
just leave it — the union handles it.

Keys are content-derived (`<source>:<shortid>`), so a key appearing in two shards
is the same logical entry; union-with-last-wins is safe.
"""
from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path


def _ingestion(vault: Path) -> Path:
    return vault / "_system" / "ingestion"


def legacy_path(vault: Path) -> Path:
    return _ingestion(vault) / "processed.json"


def shard_dir(vault: Path) -> Path:
    return _ingestion(vault) / "processed"


def current_shard_name() -> str:
    """Per-machine shard id. Override with $DREAM_SHARD (e.g. per-source shards)."""
    host = os.environ.get("DREAM_SHARD") or socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9_-]", "-", host) or "unknown"


def current_shard_path(vault: Path) -> Path:
    return shard_dir(vault) / f"{current_shard_name()}.json"


def _read_entries(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("entries") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def merged_entries(vault: Path) -> dict:
    """Union of legacy processed.json + every shard under processed/."""
    out: dict = dict(_read_entries(legacy_path(vault)))
    sd = shard_dir(vault)
    if sd.is_dir():
        for p in sorted(sd.glob("*.json")):
            out.update(_read_entries(p))
    return out


def ledger_keys(vault: Path) -> set[str]:
    """Set of all processed keys across legacy + shards. Used by stagers to skip."""
    return set(merged_entries(vault).keys())


def update_shard(vault: Path, new_entries: dict) -> None:
    """Merge new_entries into THIS machine's shard only, and write just that shard."""
    if not new_entries:
        return
    sd = shard_dir(vault)
    sd.mkdir(parents=True, exist_ok=True)
    p = current_shard_path(vault)
    existing = _read_entries(p)
    existing.update(new_entries)
    p.write_text(
        json.dumps({"version": 1, "entries": existing}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
