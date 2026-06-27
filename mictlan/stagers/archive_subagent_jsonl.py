"""Bulk-archive Claude Code Task-tool subagent invocations.

These are the raw exploration sessions Claude Code spawns via the Task tool.
Their source_ids start with `agent-`, and the durable synthesis (decisions,
merged findings, action taken) is already captured in the parent Claude Code
conversation. The subagent log is the path, not the destination — appending
them as separate H2s into project notes mostly adds noise.

This script marks each pending `agent-*` conversation in
`_system/ingestion/staging/claude-code/` as archived in the ledger so the
digest pipeline stops surfacing them. After ledger entry is recorded the
staged file is unlinked — the parent conversation already captures any
durable synthesis, and the raw JSONL still lives at `~/.claude/projects/*`
if you ever need to re-process.

    uv run --with pyyaml _system/scripts/archive_subagent_jsonl.py
    uv run --with pyyaml _system/scripts/archive_subagent_jsonl.py --confirm
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import mictlan.orchestrate as od  # noqa: E402


from mictlan.paths import VAULT as VAULT_DEFAULT
REASON = "subagent invocation — synthesis lives in parent conversation"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=VAULT_DEFAULT)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required to actually write the ledger. Without it, dry-run.",
    )
    args = parser.parse_args(argv)
    vault = args.vault.resolve()
    od._bind_analyzer_to_vault(vault)

    pending = od.discover_pending(vault, source_filter="claude-code")
    targets: list[tuple[str, dict, Path]] = []
    for p in pending:
        raw = json.loads(p.read_text(encoding="utf-8"))
        sid = raw.get("source_id", "")
        if not sid.startswith("agent-"):
            continue
        key = od.ledger_key(raw["source"], sid)
        targets.append((key, raw, p))

    print(f"would archive {len(targets)} subagent conversation(s) under "
          f"action='archived' in processed.json")
    for key, raw, _ in targets[:5]:
        print(f"  {key}  ({raw.get('source_id','')})")
    if len(targets) > 5:
        print(f"  … and {len(targets) - 5} more")

    if not args.confirm:
        print("\ndry-run; pass --confirm to write the ledger.")
        return 0

    today = date.today().isoformat()
    new_entries: dict[str, dict] = {}
    for key, raw, _ in targets:
        new_entries[key] = {
            "source": raw["source"],
            "source_id": raw["source_id"],
            "action": "archived",
            "noted_at": today,
            "created": [],
            "appended": [],
            "skip_reason": REASON,
        }
    # Persist to THIS machine's ledger shard BEFORE unlinking, so a crash can
    # never leave a staged file removed without its ledger record.
    od._ledger.update_shard(vault, new_entries)
    removed = 0
    for key, raw, path in targets:
        # Drop the staged copy — the parent conversation captures synthesis.
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"  warn: could not unlink {path}: {e}", file=sys.stderr)
    print(f"\narchived {len(targets)} entries in processed.json (removed {removed} staged files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
