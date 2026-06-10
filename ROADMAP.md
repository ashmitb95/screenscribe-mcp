# ROADMAP

Strategic frame: **build the things that get *more* valuable as base models get better at video.**
"Understand this one video" is being commoditized by the frontier models — deepening raw
single-video comprehension is building *against* the tide. Everything *around* a single video —
memory across many, synthesis, the bridge to action — only compounds as models improve. That's
the durable surface area.

This document captures four threads, how they slot into the **current** architecture, their
dependencies, effort, and open questions. It is a plan, not yet executed.

---

## Where the code is today (grounding)

- **Flat module layout** — every module (`main.py`, `server.py`, `analyzer.py`, …) sits at the
  repo root with flat imports (`from analyzer import …`). No `pyproject.toml`, no `setup.py`,
  just `requirements.txt`. `server.py` even does `sys.path.insert(0, _HERE)` to make imports work.
- **Two entry points:** the CLI `main()` ([main.py:522](main.py#L522), argparse, subcommands
  `extract / ask / analyze / slides / sessions`) and the MCP server
  ([server.py:639](server.py#L639), FastMCP, `mcp.run()`).
- **Four extraction tiers**, in cost order:
  1. `extract_transcript` — free, words only.
  2. `analyze_video` — Gemini watches the whole video → **structured dict** (`summary`,
     `sections` w/ timestamps, `key_moments`, on-screen text). Cheap, no frames.
     ([gemini_analyzer.py](gemini_analyzer.py))
  3. `extract_video` — Gemini picks frames → ffmpeg extracts → Claude Vision describes. Slow.
  4. `extract_slides` — frame selection only, saves PNGs.
- **Sessions** persist to `~/.video-analyzer/{video_id}/` (`session.json` + `frames/` + video).
  One video per session. ([session.py:16](session.py#L16))
- **Context injection is input-only today.** `load_context()` pulls files / dirs / URLs / stdin
  into the prompt for `ask`. ([context.py:29](context.py#L29)) The execution bridge is the
  *output* side of this same machinery.
- ffmpeg/ffprobe are invoked as **bare binary names** in subprocess calls
  ([frame_extractor.py:85](frame_extractor.py#L85), [:124](frame_extractor.py#L124),
  [:151](frame_extractor.py#L151)) — a silent system dependency.

---

## Dependency graph

```
        ┌─────────────────────────┐
        │ 4. Packaging / uvx      │  independent of 1–3; unblocks adoption of ALL of them
        └─────────────────────────┘

        ┌─────────────────────────┐
        │ 3. Schema extraction    │  generalizes analyze_video's structured output
        └───────────┬─────────────┘  → foundational primitive
                    │ (soft dep: typed knowledge ⇒ better)
          ┌─────────┴─────────┐
          ▼                   ▼
┌───────────────────┐  ┌───────────────────┐
│ 1. Cross-video    │  │ 2. Execution      │
│    synthesis      │  │    bridge         │
└───────────────────┘  └───────────────────┘
```

- **4 is orthogonal** — no code dependency on 1–3, but it's the multiplier on every one of them
  (a feature behind the venv dance reaches no one).
- **3 is the keystone** — both 1 and 2 are dramatically better over *typed* knowledge than over
  prose. `analyze_video` already emits a structured dict, so 1 and 2 *can* be built first on that
  ad-hoc shape; but doing 3 first gives them a real schema to stand on.

---

## Recommended sequence

**4 → 3 → 1 → 2.** — **DECIDED: 4 (packaging) goes first.** The rest (1–3) are reachable today
with extra manual steps; packaging is the bottleneck on adoption and on re-running everything else,
so it leads.

Then the schema primitive (3), because it's a small generalization of code that already exists and
it raises the ceiling for both headline features. Then synthesis (1) — the proven, hand-rolled
need — and finally the execution bridge (2), the "watch → produces" endgame.

## Naming — DECIDED: `screenscribe`

- **PyPI `screenscribe`** — ✅ available. **npm `screenscribe`** (for the extension) — ✅ available.
- **GitHub** — ⚠️ generic/crowded: several small repos already use the name, incl.
  `GreyssonEnterprises/screenscribe` (a near-identical "video/audio → structured notes" CLI). The
  name is *usable* (package registries are clear) but **not distinctive** — it won't own the search
  term, which dents the discovery angle. A free, distinctive modifier (`screenscribe-mcp`,
  `screenscribe-ai`) remains available everywhere if distinctiveness wins over brevity. **Open.**

---

## 1. Cross-video synthesis — the product bet

**What:** point it at a playlist / channel / list of URLs / topic → it extracts each video, then
runs a **cross-video aggregation pass** that produces a single distilled artifact (the common
patterns, the comparison, the shared vocabulary/grammar). This is the thing you hand-rolled
manually across nine videos.

**Why model-proof:** pure orchestration + synthesis. Better base models = better per-video
extraction *and* better aggregation. It never becomes redundant; it gets sharper.

**How it slots in:**
- New tier `synthesize` (CLI subcommand + `synthesize_playlist` MCP tool).
- Stage 1 — **fan-out**: resolve the playlist/channel to URLs (yt-dlp already a dep), run the
  existing per-video pipeline on each. `analyze_video` (tier 2) is the right default engine —
  cheap, structured, no frame cost at scale.
- Stage 2 — **aggregate**: a new synthesis call over the N structured analyses → distilled output.
- Persist as a new artifact type — either a synthesis "session" or a `~/.video-analyzer/synthesis/`
  namespace (today's session model is strictly one-video-per-dir; this is the first thing that
  breaks that assumption — see open questions).

**Effort:** ~1–2 days for a usable v1 on top of `analyze_video`. Fan-out is mostly plumbing;
the aggregation prompt + artifact schema is the real design work.

**Open questions:**
- Artifact shape: freeform distilled prose, or a typed comparison (feeds directly into item 3)?
- Dedup / clustering across videos before synthesis, or let the model do it in one pass? (N large
  analyses may blow the context window — may need map-reduce / hierarchical merge.)
- Cost ceiling + caching: re-running a 30-video channel shouldn't re-pay for unchanged videos.
  Reuse the existing per-video session cache; only the aggregation re-runs.
- Storage model for multi-video artifacts (the session schema is single-video today).

---

## 2. Execution bridge — make "watch → do" literal

**What:** take extracted knowledge and **emit grounded artifacts** — code, a config, a runnable
scaffold, a diff against the user's repo. "From this API walkthrough, generate the client in my
codebase."

**Why model-proof:** the quality of grounded generation rises directly with model capability; the
*grounding* (real frames/transcript + the user's real files) is the durable moat.

**How it slots in:**
- This is the **output half of `load_context`**. Input injection already exists
  ([context.py](context.py)); extend the same source-loading machinery so the user's files become
  both context *and* the target surface for a generated diff.
- New mode on `ask` (or a sibling `produce` command / MCP tool): same session + context inputs,
  but the output contract is an artifact (file, diff, scaffold) instead of prose.
- In MCP form this is especially natural — Claude Code is already the executor; the tool returns
  the grounded artifact and the agent applies it.

**Effort:** ~1–2 days. The generation call is small; the work is the output contract (how to emit
a clean diff/file set) and wiring it through both CLI and MCP.

**Open questions:**
- Output format: raw file content, unified diff, or a structured "file ops" list an agent applies?
- Does the tool *write* files, or only *return* artifacts for the caller to apply? (MCP server
  writing to a user's repo is a trust/safety line — default to returning, not writing.)
- Strongly benefits from item 3: a typed extraction ("the final config", "code state per step")
  is far more groundable than prose.

---

## 3. Schema-driven extraction — typed, not prose

**What:** a mode where the caller hands a **shape** and the tool fills it: "extract every CLI
command shown," "extract the final config file," "extract the code state at each step as a diff
sequence." Prose is for humans; structured output is what an agent can automate on.

**Why it's the keystone:** this turns the tool from a reading aid into an **automation primitive**,
and it's the substrate that makes 1 (synthesis over typed records) and 2 (grounding from typed
extraction) much stronger.

**How it slots in:**
- `analyze_video` *already* returns a structured dict — this generalizes that from a fixed shape
  to a **caller-supplied JSON schema**. Gemini and Claude both support constrained/JSON output.
- New param on the analyze/extract tiers: `schema` (a JSON Schema or a named preset like
  `cli_commands`, `final_config`, `step_diffs`). Output validates against it.
- Ship a few presets so it's useful before anyone writes a schema by hand.

**Effort:** ~1 day for v1 with a handful of presets; generalizing `analyze_video`'s prompt to take
an arbitrary schema is the bulk of it. Validation/retry-on-mismatch is the polish.

**Open questions:**
- Preset library vs. free-form schema first? (Presets prove value faster.)
- Which engine — Gemini (whole-video, cheap) or Claude over frames (visual precision)? Probably
  schema-dependent; "code on screen at each step" wants frames, "list of topics" wants Gemini.
- Schema for *temporal* extraction (per-step / per-section sequences) is the interesting, harder
  case vs. a flat one-shot extraction.

---

## 4. Packaging & distribution — the adoption unlock (the 80/20)

**What:** kill the install friction. Today: `git clone` → venv → activate →
`pip install -r` → hand-edit MCP JSON with absolute paths to the venv python and `server.py`.
Target: `uvx video-analyzer-llm analyze <url>` and `claude mcp add video-analyzer -- uvx
video-analyzer-llm-mcp`.

**Why first (execution-wise):** it's the only thread with a finite, well-trodden spec, and it's the
multiplier on every other feature. It's also a re-run of a playbook that already worked
(claude-notifier got traction off a one-command, discoverable install).

### Sub-steps, in order

1. **Restructure into a package** — `src/video_analyzer_llm/`, move all modules in, rewrite the
   flat imports to package-relative, drop the `sys.path.insert` hack in `server.py`. **This is the
   bulk of the work** — it touches every module's imports, not just one new file. Tests under
   `tests/` will need their imports updated too.
2. ✅ **DONE — `pyproject.toml`** with two `console_scripts`:
   - `screenscribe = screenscribe.main:main`
   - `screenscribe-mcp = screenscribe.server:run` (added a `run()` wrapper around `mcp.run()`).
   Distribution name `screenscribe-mcp`; import package `screenscribe`; src layout; editable install
   verified (`screenscribe --help`, `python -m screenscribe`, MCP server imports with 7 tools).
3. ✅ **DONE — Bundle ffmpeg.** Used **`static-ffmpeg`** (ships both ffmpeg *and* ffprobe, sidesteps
   the imageio-ffmpeg ffprobe gap). New resolver [`ffmpeg_paths.py`](src/screenscribe/ffmpeg_paths.py):
   prefers system binaries (`shutil.which`), falls back to static-ffmpeg's bundled pair, caches per
   process. `frame_extractor.py` calls go through `ffmpeg_bin()`/`ffprobe_bin()`; `downloader.py`
   passes `ffmpeg_dir()` to yt-dlp's `ffmpeg_location` (the `bestvideo+bestaudio` merge needs it).
   Both branches + all three call sites verified on a synthetic clip.
4. **Lower the key friction** — ◐ *partial:* both entry points now call plain `load_dotenv()`, so
   **shell env wins** (dotenv doesn't override existing vars) — a dev who exports the keys needs no
   `.env`. *Remaining:* lead docs with the **zero-key transcript on-ramp** ("try in 10s, add keys for
   frames"); optional `screenscribe setup` first-run that prompts and writes the key file.
5. ✅ **DONE — Document the one-liners.** README rewritten: `screenscribe` brand throughout, a
   `uvx`-first Quick Start with the zero-key transcript on-ramp, `claude mcp add screenscribe -- uvx
   screenscribe-mcp` registration (replaced the venv/JSON-surgery blocks), updated architecture tree
   and config paths. All `python main.py …` examples → `screenscribe …`, including user-facing CLI
   output strings (banners, "now ask" hints) and the `main.py`/`server.py` module docstrings.
   *Note:* the session dir stays `~/.video-analyzer/` on purpose — renaming it would orphan existing
   local sessions; revisit only with a migration step.
6. **Publish to PyPI** — ⏳ *the gate, NOT yet done — needs your PyPI account + token, and it's an
   irreversible public release, so it waits for your explicit go-ahead.* The MCP directories and the
   extension all point *at* the published package, so nothing downstream can happen until this lands.
   Wheel already builds clean and installs+runs in an isolated env (verified).

**Effort:** ~1–2 days total for steps 1–6. Step 1 (restructure) dominates; everything after is
small and well-trodden.

### Discovery stack (after PyPI lands) — cheapest-first

**Marketplace = discovery (intent-driven catalog traffic). Demo = conversion (only works on people
who already found you). Don't conflate them.** The VS Code marketplace is what drove
claude-notifier; this repo's analog is the MCP directories + an extension wrapper.

1. **List in every MCP directory + awesome-list** — smithery.ai, official MCP Registry, mcp.so,
   glama.ai, PulseMCP; PRs into `awesome-mcp-servers` / `awesome-claude-code`. Cheap, day-one, and
   being early in a fast-growing catalog compounds. *Forced order: needs the published, installable
   package to point at — so it runs after PyPI, not in parallel.* Honest caveat: these channels
   today carry a fraction of the VS Code marketplace's traffic — right channel, emerging market.
2. **PyPI keywords/classifiers + problem-phrase README** — target the searches ("youtube to code,"
   "extract code from a tutorial," "video to transcript"). Organic, compounds slowly.
3. **Thin VS Code / Cursor extension whose job is one-click "add screenscribe to your agent"** —
   the highest-leverage play because it's the *proven* channel (your only tool with traction is the
   one in the marketplace) and it kills activation friction at the same time. Mechanism is concrete:
   the extension registers the MCP server config for the user — Cursor **"Add to Cursor" deeplinks**
   (`cursor://anysphere.cursor-deeplink/mcp/install?...`) and VS Code's `vscode:mcp/install` /
   native MCP config — so one button replaces hand-editing MCP JSON with absolute paths. It just
   registers `uvx screenscribe-mcp`, so it **depends on PyPI** and is the *last* step of item 4,
   not the first. Extra surface to maintain, but it's the "do what already worked" move.

**Demo site** stays in the toolkit — filed under *convert the people the above bring in*, not *find
them*. Same for a hosted MCP (Smithery hosts it → add by URL): an evaluation-friction reducer, after
the uvx move.
