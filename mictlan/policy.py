#!/usr/bin/env python3
"""Shared loader for the consolidation coexistence policy.

One source of truth, two transports: MCP agents call
`mcp__brain__get_consolidation_policy()`; everyone else (Hermes, cron, linters)
imports this module. Both read the SAME file — `_system/dream-policy.md` — so the
served policy and the file-read policy can never diverge.

Usage:
    from mictlan.policy import load_policy, sign, PolicyUnavailable
    pol = load_policy()                 # raises PolicyUnavailable -> caller FAILS CLOSED
    heading = sign(pol, "Hermes", "2026-06-25")
    if pol.is_guardrailed("financial-q2"): ...

CLI (for shell agents / debugging):
    python3 -m mictlan.policy            # prints the policy as JSON, exit 0
    python3 -m mictlan.policy --check    # validates, prints status, exit 0/1

NOTE (cutover TODO): VAULT below is resolved relative to the OLD vault layout
(`_system/scripts/<file>`). In the repo the path differs — wire VAULT from an
env var or brain-MCP before deleting the vault copy. See the enhancement issue.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys

try:
    import yaml
except ModuleNotFoundError:  # uv run --with pyyaml
    print("mictlan.policy: pyyaml required (uv run --with pyyaml ...)", file=sys.stderr)
    raise

# Resolve the vault relative to this script: _system/scripts/ -> vault root.
VAULT = pathlib.Path(__file__).resolve().parents[2]
POLICY_PATH = VAULT / "_system" / "dream-policy.md"


class PolicyUnavailable(Exception):
    """Policy file is missing or an undownloaded iCloud placeholder. FAIL CLOSED."""


class Policy:
    def __init__(self, data: dict):
        self._d = data
        self.version = data["policy_version"]
        self.date = _dt.date.fromisoformat(data["policy_date"])
        self.max_stale_days = int(data.get("max_stale_days", 7))

    @property
    def is_stale(self) -> bool:
        return (_dt.date.today() - self.date).days > self.max_stale_days

    @property
    def agents(self) -> list[str]:
        return self._d["agents"]

    def boundary(self, agent: str) -> list[str]:
        return self._d["ingest_boundaries"].get(agent, [])

    def is_guardrailed(self, slug: str) -> bool:
        if slug in self._d.get("guardrail_slugs_exact", []):
            return True
        return any(slug.startswith(p) for p in self._d.get("guardrail_slug_prefixes", []))

    def is_protected(self, path: str) -> bool:
        return any(path.startswith(p) for p in self._d.get("protected_paths", []))

    def as_dict(self) -> dict:
        return dict(self._d)


def _read_frontmatter(text: str) -> dict:
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise PolicyUnavailable("policy file has no YAML frontmatter")
    return yaml.safe_load(parts[1]) or {}


def load_policy(path: pathlib.Path = POLICY_PATH) -> Policy:
    """Load + validate the policy. Raises PolicyUnavailable so callers fail closed."""
    icloud_stub = path.with_name("." + path.name + ".icloud")
    if icloud_stub.exists() and not path.exists():
        raise PolicyUnavailable(f"policy is an undownloaded iCloud placeholder: {icloud_stub}")
    if not path.exists():
        raise PolicyUnavailable(f"policy file missing: {path}")
    try:
        data = _read_frontmatter(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - any parse failure is fail-closed
        raise PolicyUnavailable(f"policy unparseable: {e}") from e
    for key in ("policy_version", "policy_date", "agents", "heading_signature"):
        if key not in data:
            raise PolicyUnavailable(f"policy missing required key: {key}")
    return Policy(data)


def sign(pol: Policy, agent: str, date: str) -> str:
    """Return the canonical signed heading + version stamp for `agent` on `date`."""
    if agent not in pol.agents:
        raise ValueError(f"unregistered agent {agent!r}; must be one of {pol.agents}")
    sig = pol._d["heading_signature"].format(date=date, agent=agent)
    return f"{sig}\n<!-- policy:v{pol.version} -->"


def _main(argv: list[str]) -> int:
    check = "--check" in argv
    try:
        pol = load_policy()
    except PolicyUnavailable as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    status = {
        "ok": True,
        "policy_version": pol.version,
        "policy_date": pol.date.isoformat(),
        "stale": pol.is_stale,
        "agents": pol.agents,
    }
    print(json.dumps(status if check else pol.as_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
