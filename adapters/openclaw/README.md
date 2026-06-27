# OpenClaw / Nico adapter

Dreams over the **OpenClaw assistant sessions** (`~/.openclaw/agents/main/sessions/`).
Runs on the Mac Mini. Light / Deep / REM passes; **Deep is the sole writer**.

OpenClaw's dreaming is a **compiled bundle** (`dist-dreaming-*.cjs`), so
`mictlan` owns only its **configuration**, not its source:

- The dreaming model is `topLevelModel` → the agent primary = `google/gemini-3.5-flash`,
  with other models as **fallbacks only on failure**. No dreaming-specific override
  is set, which is the desired state (one model for consolidation).
- Config lives in `~/.openclaw/openclaw.json`. This adapter documents the expected
  shape; it does not generate the managed `service-env` file.

TODO (if OpenClaw exposes a plugin seam): have Deep emit `mictlan.schema`
and route its proposed nodes through the shared resolution gate.
