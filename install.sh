#!/usr/bin/env bash
# mictlan installer — engine + /dream skill, single-source (symlink, no copies).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_CMDS="${HOME}/.claude/commands"

echo "==> mictlan install from ${ROOT}"

# 1. Python engine (uv)
if command -v uv >/dev/null 2>&1; then
  ( cd "$ROOT" && uv sync )
else
  echo "!! uv not found — install uv first (https://docs.astral.sh/uv/)"; exit 1
fi

# 2. /dream skill -> claude config (symlink so the repo stays the source of truth)
mkdir -p "$CLAUDE_CMDS"
ln -sf "${ROOT}/skills/dream/SKILL.md" "${CLAUDE_CMDS}/dream.md"
echo "==> linked /dream -> ${ROOT}/skills/dream/SKILL.md"

echo "==> Claude Code ready — type /dream."
echo "    Mac Mini harnesses: run 'make install-hermes' / 'make install-openclaw' on the mini."
