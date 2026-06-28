"""Single source of vault path resolution for the whole engine.

Every module derives its paths from here instead of computing them relative to
its own file location (the old `_system/scripts/` assumption, which breaks once
the code lives in this package). Override with the MICTLAN_VAULT env var;
otherwise fall back to the conventional iCloud path present on both the laptop
and the Mac Mini.
"""

from __future__ import annotations

import os
import pathlib

DEFAULT_VAULT = os.path.expanduser("~/Documents/Obsidian Vault")
VAULT = pathlib.Path(os.environ.get("MICTLAN_VAULT", DEFAULT_VAULT))

# brain-mcp owns the vault's vector index (.vectors.db); after a dream writes
# notes we invoke its reindexer. Override with MICTLAN_BRAIN_MCP.
DEFAULT_BRAIN_MCP = os.path.expanduser("~/github/skalas/brain-mcp")
BRAIN_MCP_DIR = pathlib.Path(os.environ.get("MICTLAN_BRAIN_MCP", DEFAULT_BRAIN_MCP))

NOTES = VAULT / "notes"
CONVERSATIONS = VAULT / "conversations"
DREAMS = VAULT / "dreams"
DAILY = VAULT / "daily"
INDEX = VAULT / "_index"
SYSTEM = VAULT / "_system"
INGESTION = SYSTEM / "ingestion"
STAGING = INGESTION / "staging"
PROPOSED = INGESTION / "proposed"
SCHEMAS = SYSTEM / "schemas"
RECIPES = SYSTEM / "recipes"
POLICY_PATH = SYSTEM / "dream-policy.md"
