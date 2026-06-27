VAULT          ?= $(HOME)/Documents/Obsidian Vault
MINI           ?= macmini
CLAUDE_CMDS    ?= $(HOME)/.claude/commands
HERMES_SCRIPTS ?= $(HOME)/.hermes/scripts
REPO_PATH      ?= $(HOME)/github/skalas/mictlan

.PHONY: help install install-hermes install-openclaw link-skill deploy-mini test check

help:
	@echo "mictlan — install targets:"
	@echo "  make install           Claude Code (laptop): uv sync + link the /dream skill"
	@echo "  make install-hermes    Hermes (Mac Mini): uv sync + symlink dream_cycle.py"
	@echo "  make install-openclaw  OpenClaw: print the expected dreaming config (no package)"
	@echo "  make deploy-mini       ssh the mini, git pull, reinstall Hermes there"
	@echo "  make test / make check"

# Claude Code (laptop): engine + the /dream skill.
install:
	uv sync
	$(MAKE) link-skill

# Single source for the /dream skill: claude config points at the repo, not a copy.
link-skill:
	mkdir -p "$(CLAUDE_CMDS)"
	ln -sf "$(CURDIR)/skills/dream/SKILL.md" "$(CLAUDE_CMDS)/dream.md"
	@echo "linked /dream -> $(CURDIR)/skills/dream/SKILL.md"

# Hermes (Mac Mini): the adapter imports `mictlan`, so the package MUST be installed here.
install-hermes:
	uv sync
	mkdir -p "$(HERMES_SCRIPTS)"
	ln -sf "$(CURDIR)/adapters/hermes/dream_cycle.py" "$(HERMES_SCRIPTS)/dream_cycle.py"
	@echo "linked dream_cycle.py -> $(CURDIR)/adapters/hermes/dream_cycle.py"
	@echo "run via: uv run --project $(CURDIR) python $(HERMES_SCRIPTS)/dream_cycle.py [YYYY-MM-DD]"

# OpenClaw / Nico: dreaming is a compiled bundle — nothing to install, only config to verify.
install-openclaw:
	@echo "OpenClaw dreaming is a compiled bundle — no installable package."
	@echo "Verify ~/.openclaw/openclaw.json:"
	@echo "  - model.primary = google/gemini-3.5-flash"
	@echo "  - no dreaming-specific model override (fallbacks apply only on failure)"

# From the laptop: fast-forward the mini's clone and reinstall Hermes on it.
deploy-mini:
	ssh $(MINI) 'cd $(REPO_PATH) && git pull --ff-only && make install-hermes'
	@echo "mini updated + Hermes reinstalled"

test:
	uv run --extra dev pytest -q

check:
	python3 -m py_compile mictlan/*.py mictlan/stagers/*.py adapters/hermes/*.py
