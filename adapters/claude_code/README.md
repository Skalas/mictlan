# Claude Code adapter

Dreams over what Miguel **builds and thinks**: Claude Code JSONL, Claude.ai web,
and Cursor JSONL. Runs on the laptop via the `/dream` skill
([`skills/dream/SKILL.md`](../../skills/dream/SKILL.md)).

- Cursor is a **source** here (staged by `mictlan.stagers.cursor`), not its own
  dreamer.
- Hosts the **single cross-agent human-approval gate** (`/dream` Step 6.5): the
  one place new nodes are created, after the resolution gate dedups them.
- Model: `gemini-3.5-flash` (sub-agent digests).
