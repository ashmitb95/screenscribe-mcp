# AGENTS.md

Guidance for AI agents (and humans) working in this repo. Read before editing.

## What this is

**screenscribe** — extract *typed* data from videos (schema-driven) and synthesize across many.
Powered by **Gemini** (it watches the video); the calling agent does the reasoning. Ships a CLI and
an MCP server. Python package under `src/screenscribe/`.

## Setup & test

- Use the venv at `./venv`. Install editable: `./venv/bin/pip install -e .`
- **Run tests:** `./venv/bin/python -m pytest -q`
- **TDD:** write the failing test first, then the minimal code. Tests are **SDK-free** — they mock
  `gemini_selector._call_gemini` / `_call_gemini_text` / `gemini_available` and patch
  `session.SESSIONS_DIR` to a `tmp_path`. **No live API calls in the suite.**
- `GEMINI_API_KEY` lives in `.env` (gitignored) or the shell env — the **only** key needed.
  Transcript mode and explicit `--timestamps` need no key.
- ffmpeg/ffprobe are bundled via `static-ffmpeg`; the resolver prefers a system binary if present.

## Architecture (`src/screenscribe/`)

- `resolver.py` — `(channel | playlist | list | video URL) → video IDs`. Canonical `parse_video_id`
  (other modules delegate here — don't re-add the regex). Surfaces per-video stubs (title, view_count).
- `structured_extractor.py` — schema-driven typed extraction (`extract_structured`), batch fan-out
  (`extract_structured_batch`), schema resolution + validation; per-video presets in `schemas/`.
- `synthesis.py` — cross-video: `categorize` (title classification) + compounding `synthesize_pass`;
  aggregate presets in `schemas/aggregate/`.
- `gemini_selector.py` — `_call_gemini` (uploads the video) and `_call_gemini_text` (text→structured,
  no video); frame selection.
- `gemini_analyzer.py` — whole-video structured analysis. `frame_extractor.py` — ffmpeg extraction +
  snap-to-stable + perceptual dedup. `ffmpeg_paths.py` — binary resolver. `downloader.py` — yt-dlp +
  transcript. `session.py` — persistence at `~/.video-analyzer/`. `main.py` — CLI. `server.py` — MCP
  (9 tools). `config.py` — Gemini model + thresholds.

## Conventions (important — these are easy to regress)

- **Gemini-only.** The engine has **no `anthropic`/Claude dependency** — the calling agent does the
  reasoning (incl. viewing extracted frames). Do not reintroduce a server-side reasoning model.
- **Never a silent cut.** Always account for dropped/skipped/truncated data *explicitly* — transcript
  truncation flags, resolver `skipped`/`total_found`, synthesis `extraction_failed`, a structured
  `{"status":"invalid"}` instead of malformed output. Mirror this principle in new code.
- **Schema-driven + validated.** Typed JSON is validated with `jsonschema`; on mismatch, retry once,
  then return a structured error — never pass malformed data off as success. Cache per `(video, schema)`.
- **Surgical changes.** Match surrounding style; don't refactor unrelated code or "improve" adjacent
  things. Every changed line should trace to the task.

## Git & docs

- **Commit as the user. Never add a Claude / AI co-author trailer.**
- **`docs/` is gitignored** — local planning, specs, and dogfooding artifacts live there and are
  **not committed**. The repo ships clean, generic code only; keep planning/experiment output out of git.
- Feature work: branch → PR → squash-merge to `main`. Don't push straight to `main`.

## Cost

~$0.03 per video extraction (cached; re-runs free). Cross-video synthesis paces work in capped
per-category passes (`top_n`) so it scales to a whole channel without one giant prompt.
