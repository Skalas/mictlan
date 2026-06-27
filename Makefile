VAULT ?= $(HOME)/Documents/Obsidian Vault
MINI  ?= macmini
CLAUDE_CMDS ?= $(HOME)/.claude/commands

.PHONY: help install test deploy-mini link-skill check

help:
	@echo "mictlan — targets:"
	@echo "  make install      uv sync + install the /dream skill into ~/.claude"
	@echo "  make test         run pytest"
	@echo "  make link-skill   symlink skills/dream/SKILL.md -> ~/.claude/commands/dream.md"
	@echo "  make deploy-mini  rsync engine + hermes/openclaw adapters to the Mac Mini"
	@echo "  make check        py_compile the engine"

install:
	uv sync
	$(MAKE) link-skill

# Single source for the /dream skill: claude config points at the repo, not a copy.
link-skill:
	ln -sf "$(CURDIR)/skills/dream/SKILL.md" "$(CLAUDE_CMDS)/dream.md"
	@echo "linked /dream -> $(CURDIR)/skills/dream/SKILL.md"

test:
	uv run --with pydantic --with pyyaml pytest -q

check:
	python3 -m py_compile mictlan/*.py mictlan/stagers/*.py adapters/hermes/*.py

# Deploy the shared engine + the two Mac-Mini adapters from this repo.
deploy-mini:
	rsync -av --delete mictlan/ $(MINI):mictlan/mictlan/
	rsync -av adapters/hermes/dream_cycle.py $(MINI):.hermes/scripts/dream_cycle.py
	rsync -av adapters/openclaw/ $(MINI):mictlan/adapters/openclaw/
	@echo "deployed engine + adapters to $(MINI)"
