#!/usr/bin/env python3
"""Validate every note's frontmatter against _system/schemas/frontmatter.json.

Run: uv run --with pyyaml,jsonschema _system/scripts/validate.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft7Validator

from mictlan.paths import VAULT
SCHEMA = VAULT / "_system" / "schemas" / "frontmatter.json"
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def main() -> int:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = Draft7Validator(schema)
    bad = 0
    total = 0
    for folder in ("notes", "meetings", "daily", "conversations"):
        for p in sorted((VAULT / folder).glob("*.md")):
            total += 1
            text = p.read_text(encoding="utf-8")
            m = FRONTMATTER_RE.match(text)
            if not m:
                print(f"NO FRONTMATTER: {p.relative_to(VAULT)}")
                bad += 1
                continue
            try:
                fm = yaml.safe_load(m.group(1)) or {}
            except yaml.YAMLError as e:
                print(f"YAML ERROR: {p.relative_to(VAULT)} — {e}")
                bad += 1
                continue
            errors = sorted(validator.iter_errors(fm), key=lambda e: e.path)
            if errors:
                bad += 1
                for err in errors:
                    print(f"{p.relative_to(VAULT)} :: {'.'.join(map(str, err.path)) or '(root)'} :: {err.message}")
    print(f"\n{total - bad}/{total} valid, {bad} invalid")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
