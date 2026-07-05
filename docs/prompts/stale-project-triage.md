# Prompt: stale-project triage (run with a cheap model)

Paste everything below the line into a fresh Claude Code session (`claude --model haiku`
or sonnet) started anywhere. It needs no MCP and no context beyond the vault.

---

You are running a **stale-project triage** over my Obsidian vault at
`${MICTLAN_VAULT:-$HOME/Documents/Obsidian Vault}`. The vault is git-tracked; work
directly, everything is revertible.

## Goal
`_index/projects.md` should tell the truth. Projects whose frontmatter says
`status: active` but that nobody touched in 30+ days must be re-statused by me
(the human), one batch at a time.

## Hard rules
1. You may ONLY change the frontmatter `status:` field (and add `end: 'YYYY-MM-DD'`
   when I say archived). Never touch note bodies, `updated:`, links, or any other
   field. Never delete or move files.
2. Never write to `_system/`, `_index/`, or `.obsidian/`.
3. I decide every disposition. You batch, summarize, and apply — you never guess.

## Procedure
1. Baseline: `cd` into the vault, run `git status --short`; if dirty, stop and show me.
2. Collect the working set: every `notes/*.md` with `type: project`, `status: active`,
   and `updated:` older than 30 days ago. (Cross-check against the "Stale active
   projects" section of `_index/health.md` — counts should roughly match.)
3. Sort by `org:` then by `updated:` (oldest first). Present them to me in batches
   of ~10 as a table: slug · org · last updated · one-line gist (first sentence of
   the body or its `## Estado actual`). For each, I answer one of:
   - **dormant** — real project, paused; might come back
   - **archived** — over; also set `end:` to its `updated:` date unless I give one
   - **active** — genuinely alive (leave untouched)
   - **not-a-project** — it's really a document/reference; set `type: ref` instead
     of touching status (mention this option only when the note looks like a spec,
     PRD, or sub-document of a parent project)
4. Apply each batch's decisions immediately after I answer (Edit tool, minimal
   diffs), then continue to the next batch. Track a running tally.
5. When all batches are done:
   - Run: `uv run --project ~/github/skalas/mictlan python -m mictlan.reindex`
   - Show me the summary line from `_index/health.md` (stale count should have
     collapsed) plus `git diff --stat`.
   - Ask me whether to commit. Commit message:
     `triage: project status honesty pass — <N> dormant, <M> archived, <K> reclassified`
     ending with your Co-Authored-By line.

## Tone
Terse. Tables, not prose. Don't summarize what a project was in more than one line.
If a note is empty or unreadable, put it in the batch flagged `(empty)` — I'll
probably archive it.
