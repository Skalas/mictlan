"""Orchestrate the digest pipeline end-to-end.

Three subcommands map to the three-step review-before-apply workflow:

    prepare  — pick N pending staged conversations, build analysis prompts under
               _system/ingestion/proposed/<source>-<shortid>.prompt.md, write a
               manifest. No vault mutations.
    apply    — read filled proposal JSONs at _system/ingestion/proposed/<key>.json,
               run analyzer.apply() against the vault, update processed.json,
               then run reindex.sh. Vault mutations gated on --confirm.
    status   — print pending / proposed / applied counts.

The LLM step (turning a prompt into a JSON proposal) lives between `prepare` and
`apply` and is performed by Claude Code subagents driven by the /digest slash
command. This script never calls an LLM directly.

    uv run --with pyyaml _system/scripts/orchestrate_digest.py status
    uv run --with pyyaml _system/scripts/orchestrate_digest.py prepare --limit 1
    uv run --with pyyaml _system/scripts/orchestrate_digest.py apply --confirm
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyzer  # noqa: E402
from analyzer import (  # noqa: E402
    ConversationUnit,
    Turn,
    apply,
    build_analysis_prompt,
    build_vault_context,
    parse_proposal,
)
import lint_proposals  # noqa: E402
import ledger as _ledger  # noqa: E402  (sharded dedup ledger)


VAULT_DEFAULT = Path(__file__).resolve().parents[2]


def _bind_analyzer_to_vault(vault: Path) -> None:
    """Repoint analyzer's module-level path constants at a specific vault.

    analyzer.py was written before we had multi-vault test harnesses; its
    VAULT / NOTES / CONVERSATIONS / STATE_PATH / RECIPE_PATH constants are
    captured at import time. Tests build a tmp mini-vault and run this script
    against it; without re-binding, build_vault_context would still scan the
    real production vault.
    """
    analyzer.VAULT = vault
    analyzer.NOTES = vault / "notes"
    analyzer.CONVERSATIONS = vault / "conversations"
    analyzer.STATE_PATH = vault / "_system" / "ingestion" / "state.json"
    analyzer.RECIPE_PATH = vault / "_system" / "recipes" / "conversation-append-pass.md"


def staging_dirs(vault: Path) -> list[Path]:
    base = vault / "_system" / "ingestion" / "staging"
    return [base / "claude-web", base / "claude-code", base / "cursor-code"]


def proposed_dir(vault: Path) -> Path:
    return vault / "_system" / "ingestion" / "proposed"


def load_ledger(vault: Path) -> dict:
    """Read-side view: union of the legacy processed.json + every per-machine
    shard under processed/. Writes go to this machine's shard via _ledger.update_shard."""
    return {"version": 1, "entries": _ledger.merged_entries(vault)}


def shortid(source: str, source_id: str) -> str:
    """Return the canonical short identifier used in vault source-hash markers.

    Standard Claude conversations use UUID source_ids, where the first 8 hex
    chars are unique enough to disambiguate. But Claude Code's Task tool
    invocations carry source_ids like `agent-a100c8d026ec34ca1` — the first 8
    chars (`agent-aX`) only span 16 possible values, so 100+ subagent
    conversations collapse into 16 keys and overwrite each other in the
    manifest. For those, keep enough of the id to be unique.
    """
    if source == "claude-jsonl":
        if source_id.startswith("agent-"):
            return source_id[:16]
        return source_id[:8]
    if source == "claude-web":
        # claude-web ids in vault files are typically the first 8 chars of UUID
        return source_id.replace("-", "")[:8]
    if source == "cursor-jsonl":
        return source_id[:8]
    return source_id[:8]


def ledger_key(source: str, source_id: str) -> str:
    return f"{source}:{shortid(source, source_id)}"


def load_unit_from_staging(path: Path) -> ConversationUnit:
    raw = json.loads(path.read_text(encoding="utf-8"))
    turns = [
        Turn(role=t["role"], content=t["content"], timestamp=t.get("timestamp"))
        for t in (raw.get("turns") or [])
    ]
    extra = {k: v for k, v in raw.items() if k not in {
        "source", "source_id", "started_at", "title", "turns", "cwd",
    }}
    return ConversationUnit(
        source=raw["source"],
        source_id=raw["source_id"],
        started_at=raw.get("started_at") or "",
        title=raw.get("title"),
        turns=turns,
        cwd=raw.get("cwd"),
        extra=extra,
    )


def discover_pending(vault: Path, source_filter: str | None = None) -> list[Path]:
    ledger = load_ledger(vault)
    entries = ledger.get("entries") or {}
    pending: list[Path] = []
    for d in staging_dirs(vault):
        if not d.exists():
            continue
        if source_filter and source_filter not in d.name:
            continue
        for p in sorted(d.glob("*.json")):
            # Skip metadata files (start with underscore) and any non-conversation
            # artifacts that happen to live in staging/ — _projects.json,
            # _memories.txt, etc.
            if p.name.startswith("_"):
                continue
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            source = raw.get("source") or ""
            source_id = raw.get("source_id") or ""
            if not source or not source_id:
                continue
            key = ledger_key(source, source_id)
            if key in entries:
                continue
            pending.append(p)
    return pending


# ---------- prepare ----------


def cmd_prepare(args) -> int:
    vault = args.vault.resolve()
    pending = discover_pending(vault, args.source)
    if not pending:
        print("no pending conversations")
        return 0
    pending = pending[: args.limit]

    pdir = proposed_dir(vault)
    pdir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    for staging_path in pending:
        unit = load_unit_from_staging(staging_path)
        key = ledger_key(unit.source, unit.source_id)
        prompt_path = pdir / f"{key}.prompt.md"
        ctx = build_vault_context(unit)
        prompt = build_analysis_prompt(unit, ctx)
        analyzer.write_if_changed(prompt_path, prompt)
        manifest[key] = {
            "source": unit.source,
            "source_id": unit.source_id,
            "staging_path": str(staging_path.relative_to(vault)),
            "prompt_path": str(prompt_path.relative_to(vault)),
            "proposal_path": str((pdir / f"{key}.json").relative_to(vault)),
            "title": unit.title or "",
            "started_at": unit.started_at,
        }
    manifest_path = pdir / "_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(manifest)} prompt(s) under {pdir.relative_to(vault)}/")
    for key, entry in manifest.items():
        print(f"  {key}  {entry['title'][:80]}")
    print(f"\nnext: have a subagent read each *.prompt.md and write {pdir.name}/<key>.json,")
    print(f"then run: uv run _system/scripts/orchestrate_digest.py apply --confirm")
    return 0


# ---------- apply ----------


def cmd_apply(args) -> int:
    vault = args.vault.resolve()
    pdir = proposed_dir(vault)
    manifest_path = pdir / "_manifest.json"
    if not manifest_path.exists():
        print(f"no manifest at {manifest_path}", file=sys.stderr)
        return 1
    full_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # --only scopes this run to a subset of manifest keys. The rest stay
    # untouched in the manifest (held aside for separate review). This is the
    # supported alternative to hand-trimming _manifest.json: the lint and apply
    # loops see only the subset, but the final manifest write-back preserves
    # the held keys.
    only_keys: set[str] | None = None
    if args.only:
        only_keys = {k.strip() for k in args.only.split(",") if k.strip()}
        unknown = only_keys - set(full_manifest)
        if unknown:
            print(f"--only names keys not in manifest: {sorted(unknown)}", file=sys.stderr)
            return 1
        manifest = {k: v for k, v in full_manifest.items() if k in only_keys}
    else:
        manifest = dict(full_manifest)

    # Pre-flight lint: catches mechanical bugs (broken wikilinks, missing
    # target slugs, mismatched dates, duplicate source hashes) that the
    # human review would otherwise have to catch. ERRORs block; WARNs print.
    if not args.skip_lint:
        findings = lint_proposals.lint_all(vault, only=only_keys)
        errors = [f for f in findings if f.severity == "ERROR"]
        warns = [f for f in findings if f.severity == "WARN"]
        if findings:
            print("lint:")
            for f in findings:
                print(f.fmt())
            print(f"  → {len(errors)} error(s), {len(warns)} warning(s)\n")
        if errors and not args.lint_warn_only:
            print("apply blocked by lint errors. Re-run with --lint-warn-only to override.", file=sys.stderr)
            return 1

    ledger = load_ledger(vault)
    today = date.today().isoformat()
    applied_keys: list[str] = []
    new_entries: dict = {}  # written to THIS machine's shard at the end
    errors: list[str] = []

    # Dedup against the ledger: manifest entries already marked applied/skipped
    # are stale (e.g. a previous run crashed between apply and manifest reset).
    # Re-applying would either double-write or trip the duplicate-source-hash
    # lint guard — handle it here as the primary check instead.
    ledger_entries = ledger.get("entries") or {}
    stale_keys = [k for k in manifest if ledger_entries.get(k, {}).get("action") in ("applied", "skipped")]
    for k in stale_keys:
        action = ledger_entries[k]["action"]
        noted = ledger_entries[k].get("noted_at", "?")
        print(f"[skip] {k}: already {action} on {noted}")
        if args.confirm:
            # Clean up the stale proposal/prompt files so they don't reappear next run.
            for pkey in ("proposal_path", "prompt_path"):
                p = vault / manifest[k][pkey]
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                except OSError as e:
                    print(f"  warn: could not unlink {p.relative_to(vault)}: {e}", file=sys.stderr)
        manifest.pop(k, None)

    for key, entry in sorted(manifest.items()):
        proposal_path = vault / entry["proposal_path"]
        if not proposal_path.exists():
            errors.append(f"{key}: proposal JSON missing at {proposal_path.relative_to(vault)}")
            continue
        try:
            payload = json.loads(proposal_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"{key}: invalid JSON ({e})")
            continue
        try:
            # Use shortid (matching existing vault marker convention) as
            # source_id for parse_proposal — that's what ends up in `<!-- src:... -->`.
            sid = shortid(entry["source"], entry["source_id"])
            update = parse_proposal(payload, entry["source"], sid)
        except ValueError as e:
            errors.append(f"{key}: schema error ({e})")
            continue

        if not args.confirm:
            n_creates = len(update.creates)
            n_appends = len(update.appends)
            skip = update.skip_reason or ""
            print(f"[dry] {key}: creates={n_creates} appends={n_appends} skip={skip}")
            continue

        report = apply(update, today=today)
        # apply() puts skip_reason into report.errors as a soft signal — that's
        # an applied skip, not a failure. Real errors are anything else.
        for err in report.errors:
            if err.startswith("skipped:"):
                continue
            errors.append(f"{key}: {err}")
        # Ledger entry: mark applied regardless of skip vs write — we don't want
        # to re-analyze. Distinguish via `action`.
        action = "skipped" if update.skip_reason else "applied"
        entry_record = {
            "source": entry["source"],
            "source_id": entry["source_id"],
            "action": action,
            "noted_at": today,
            "created": report.created,
            "appended": report.appended,
            "skip_reason": update.skip_reason,
        }
        ledger.setdefault("entries", {})[key] = entry_record
        new_entries[key] = entry_record
        applied_keys.append(key)
        print(
            f"[apply] {key}: created={len(report.created)} "
            f"appended={len(report.appended)} skip={update.skip_reason or '—'}"
        )

        # Drop ephemeral working files. The ledger records what we did; the
        # source JSONL still exists at ~/.claude/projects/* (claude-code) or in
        # the claude.ai export. Keeping these would grow linearly with no
        # recovery value — re-run stage_*.py + prepare to regenerate if needed.
        ephemeral = [
            vault / entry["staging_path"],
            vault / entry["proposal_path"],
            vault / entry["prompt_path"],
        ]
        for p in ephemeral:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"  warn: could not unlink {p.relative_to(vault)}: {e}", file=sys.stderr)

    if args.confirm and (applied_keys or stale_keys):
        _ledger.update_shard(vault, new_entries)
        # Reset the manifest so it reflects only still-pending work. Keys that
        # errored mid-apply stay in the manifest for retry; everything we
        # successfully processed (or detected as stale) is dropped. Base this
        # on the FULL manifest so --only held-aside keys survive the write-back.
        processed = set(applied_keys) | set(stale_keys)
        remaining = {k: v for k, v in full_manifest.items() if k not in processed}
        manifest_path.write_text(
            json.dumps(remaining, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        reindex = vault / "_system" / "scripts" / "reindex.sh"
        if reindex.exists():
            print(f"\nrunning {reindex.relative_to(vault)} …")
            subprocess.run([str(reindex)], check=False, cwd=str(vault))

    if errors:
        print(f"\n{len(errors)} error(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    return 0


# ---------- status ----------


def cmd_status(args) -> int:
    vault = args.vault.resolve()
    ledger = load_ledger(vault)
    entries = ledger.get("entries") or {}
    pending = discover_pending(vault)
    pdir = proposed_dir(vault)
    manifest = {}
    if (pdir / "_manifest.json").exists():
        manifest = json.loads((pdir / "_manifest.json").read_text(encoding="utf-8"))
    proposed_filled = sum(
        1 for k in manifest if (vault / manifest[k]["proposal_path"]).exists()
    )
    print(f"ledger entries:      {len(entries)}")
    print(f"pending in staging:  {len(pending)}")
    print(f"prompts proposed:    {len(manifest)}")
    print(f"proposals filled:    {proposed_filled} / {len(manifest)}")
    return 0


# ---------- main ----------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=VAULT_DEFAULT)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp_prepare = sub.add_parser("prepare", help="Build prompts for N pending conversations.")
    sp_prepare.add_argument("--limit", type=int, default=1)
    sp_prepare.add_argument(
        "--source",
        choices=["claude-web", "claude-code", "cursor-code"],
        default=None,
    )
    sp_prepare.set_defaults(func=cmd_prepare)

    sp_apply = sub.add_parser("apply", help="Apply filled proposal JSONs to the vault.")
    sp_apply.add_argument(
        "--confirm",
        action="store_true",
        help="Required to actually mutate the vault. Without it, dry-run only.",
    )
    sp_apply.add_argument(
        "--skip-lint",
        action="store_true",
        help="Skip pre-flight lint. Use only if the linter is itself broken.",
    )
    sp_apply.add_argument(
        "--lint-warn-only",
        action="store_true",
        help="Don't block apply on lint ERRORs — still print them. Escape hatch.",
    )
    sp_apply.add_argument(
        "--only",
        default=None,
        metavar="KEY[,KEY...]",
        help="Apply only these manifest keys (comma-separated). Others stay "
             "untouched in the manifest — use to land safe proposals while "
             "holding risky ones for review, instead of hand-trimming _manifest.json.",
    )
    sp_apply.set_defaults(func=cmd_apply)

    sp_status = sub.add_parser("status", help="Show pipeline state.")
    sp_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    _bind_analyzer_to_vault(args.vault.resolve())
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
