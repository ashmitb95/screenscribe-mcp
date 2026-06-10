# screenscribe

**Turn any video into something your agent can act on.**

The best how-to knowledge — live demos, conference talks, tutorials, walkthroughs — is trapped in video: you can watch it, but you can't *use* it. screenscribe pulls what's actually on screen (code, diagrams, steps, data) into structured, queryable knowledge and hands it to an LLM or agent as a first-class input. So "watch this 20-minute tutorial" becomes "here's exactly what it shows — now do it."

**Gemini watches the video; Claude describes the frames and answers.** Standalone CLI + MCP server (Claude Code, Cursor, Windsurf, any MCP client).

---

## Why it matters

Transcripts give you the words. The value in technical video is almost always *visual* — the code on screen, the diagram, the UI, the step nobody narrates.

- **Watch → execute.** Point an agent at a tutorial and have it do the thing: *"based on the architecture in this video, refactor my router to match."* The video becomes an input to your build, not a tab you alt-tab to.
- **Reads what's shown, not just said** — on-screen code, slides, diagrams — via vision models (Gemini watches the video, Claude describes the key frames).
- **Structured + timestamped output** an agent acts on, not prose to re-digest.
- **Agent-native** — an MCP server, so video drops straight into your coding loop.
- **Persistent sessions** — process a video once, query it forever; no re-pasting, no context-limit pain.
- **Source transparency** — every answer tells you whether it drew from the transcript, the whole-video analysis, or described frames.

It's a spectrum: start free with the transcript, add a cheap whole-video analysis, or extract and describe the actual frames when you need the images.

---

## What it does

1. **Analyzes** a video at the depth you need — transcript, a cheap whole-video Gemini analysis, key frames, or presentation slides
2. **Describes** each extracted frame with a vision-capable LLM, cross-referenced against the transcript
3. **Persists** a session at `~/.video-analyzer/{video_id}/` — runs once, cached forever
4. **Answers** any free-form question about the video, optionally with your own files as context

---

## How it works

screenscribe is built on two models, each doing what it's best at:

- **Gemini — sees the video.** The YouTube URL is handed to Gemini, which natively watches the video (frames + audio) and returns the precise timestamps where something visually important is on screen. This is what makes frame selection accurate and content-agnostic — it works on any video, not just screen recordings. Cheap (~$0.02–0.05 per video at low media resolution) and fast.
- **Claude — describes and reasons.** A Claude vision model describes each extracted frame; a Claude model answers your questions using the transcript, the frame descriptions, and any project files you inject.

ffmpeg extracts Gemini's chosen timestamps, snapping each forward to the moment the on-screen visual has settled and de-duplicating near-identical frames via perceptual hashing.

A lightweight **transcript layer** runs alongside the pair: fetch a video's transcript instantly and for free (handy for quick agentic lookups over MCP), and — if no Gemini key is configured — fall back to transcript-based frame selection so the tool still works.

### Levels of analysis

Pick the depth you need — they get cheaper to more expensive, lighter to heavier:

| Level | Command / tool | Cost | What you get |
| ----- | -------------- | ---- | ------------ |
| Transcript | `extract --transcript-only` / `extract_transcript` | free | the words, no visuals |
| **Whole-video analysis** | **`analyze` / `analyze_video`** | **~$0.03** | **Gemini watches the entire video → summary, timestamped sections, key moments, on-screen text — no frames, no download** |
| Frames | `extract` / `extract_video` | ~$0.50–1 | the above *plus* the actual key frames as PNGs, each described by Claude Vision |
| Slides | `slides` / `extract_slides` | ~$0.03 | complete on-screen visuals saved as standalone PNG images |

`analyze` is usually the sweet spot: whole-video visual understanding for the price of a transcript. Reach for `extract`/`slides` when you specifically need the frame **images** on disk.

---

## Architecture

```
screenscribe/
├── pyproject.toml         Package metadata + console scripts (screenscribe, screenscribe-mcp)
├── src/screenscribe/
│   ├── main.py            CLI — extract / analyze / slides / ask / sessions subcommands
│   ├── server.py          MCP server — 7 tools for any MCP client
│   ├── analyzer.py        Vision LLM describes frames in batches
│   ├── asker.py           LLM answers questions with session + context
│   ├── downloader.py      yt-dlp video download + youtube-transcript-api fetch
│   ├── frame_extractor.py ffmpeg extraction + snap-to-stable + perceptual dedup
│   ├── ffmpeg_paths.py    Resolves ffmpeg/ffprobe — system binary, else bundled static-ffmpeg
│   ├── gemini_selector.py Gemini watches the video to pick frames (primary)
│   ├── gemini_analyzer.py Gemini whole-video structured analysis (analyze tier)
│   ├── transcript_selector.py Transcript-based frame/slide selection (fallback)
│   ├── session.py         Session persistence at ~/.video-analyzer/
│   ├── context.py         Universal context loader (files, dirs, URLs, stdin)
│   └── config.py          Model names, thresholds, batch sizes
├── tests/
├── .env.example
└── .gitignore
```

**Session storage** (global, outside the repo):

```
~/.video-analyzer/
└── {video_id}/
    ├── session.json    # metadata + transcript + frame descriptions
    ├── frames/         # extracted PNG frames (from extract)
    ├── slides/         # presentation-quality frames (from slides)
    └── video.*         # downloaded video file
```

---

## Quick start

Requires [`uv`](https://docs.astral.sh/uv/) (or plain `pip`). **No `ffmpeg` install needed** — a static binary is fetched automatically the first time it's required (your system `ffmpeg` is used instead if you have one).

**Try it in 10 seconds, no API key:**

```bash
# Transcript only — fast, free, no key, no video download
uvx screenscribe extract "https://youtu.be/dQw4w9WgXcQ" --transcript-only
uvx screenscribe sessions
```

**Add keys for the visual tiers.** screenscribe reads them from your shell environment first, so if you already export them, nothing else to do:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # Claude — frame descriptions + answering
export GEMINI_API_KEY=...             # Gemini — watches the video to pick frames

uvx screenscribe analyze "https://youtu.be/dQw4w9WgXcQ"   # whole-video analysis (~$0.03)
```

It degrades gracefully: with only `ANTHROPIC_API_KEY` it still works, falling back to transcript-based frame selection. For the full experience, set both. ([Get a Gemini key](https://aistudio.google.com/apikey).)

> A `.env` file in the working directory also works — handy for project-local keys. Shell-exported keys take precedence over `.env`.

**Add to your agent as an MCP server (one line):**

```bash
claude mcp add screenscribe -- uvx screenscribe-mcp
```

(Other clients: see [Register the MCP server](#register-the-mcp-server) below.)

<details>
<summary><b>Run from source (development)</b></summary>

```bash
git clone git@github.com:ashmitb95/video-analyzer-llm.git
cd video-analyzer-llm
python3 -m venv venv && source venv/bin/activate
pip install -e .

cp .env.example .env   # add your keys, or export them in your shell
screenscribe sessions
pytest
```

</details>

---

## CLI usage

### Extract a video (run once per video)

Two modes, from lightest to heaviest:

```bash
# Transcript only — fast, free, no video download
screenscribe extract "https://youtu.be/dQw4w9WgXcQ" --transcript-only

# Full extraction — downloads video, extracts frames, describes with Vision
screenscribe extract "https://youtu.be/dQw4w9WgXcQ"
```

(Prefix any command with `uvx ` to run without installing, e.g. `uvx screenscribe extract …`.)

Options:

```
--transcript-only   Fetch transcript only — no video download or frame analysis
--threshold 0.1     Scene change sensitivity 0–1 (default 0.1, lower = more frames)
--interval 3.0      Min seconds between frames (default 3.0)
--max-frames 25     Max frames from transcript analysis (default 25)
--force             Re-extract even if session already exists
--resume            Resume from last completed step
```

### Analyze a whole video (cheap, no frames)

```bash
screenscribe analyze "https://youtu.be/dQw4w9WgXcQ"
```

Gemini watches the entire video and produces a structured analysis — summary, timestamped sections, key moments, and on-screen text — saved to `~/.video-analyzer/{id}/gemini_analysis.json`. No download or frame extraction. Then `ask` uses it as a source. Options: `--focus`, `--time-range`, `--force`.

### Extract presentation slides

```bash
screenscribe slides "https://youtu.be/dQw4w9WgXcQ"
```

Identifies complete, self-contained visuals — diagrams, scenes, charts, code, key moments, summaries — that would work as standalone images. Extracts them into `~/.video-analyzer/{id}/slides/`.

Options:

```
--max-slides 15     Max slides to extract (default 15)
--force             Re-extract even if slides already exist
```

### Ask anything about a video

```bash
screenscribe ask <session_id> "What are the main concepts covered?"

# Inject your own project files as context:
screenscribe ask <session_id> "Implement the pattern from the video" \
    --context ./src/app.py \
    --context ./src/utils/

# Pipe in notes via stdin:
screenscribe ask <session_id> "Compare with my notes" --stdin < notes.md
```

`--context` accepts: file paths, directory paths, HTTP/HTTPS URLs, or raw text strings. Repeatable.

### List all sessions

```bash
screenscribe sessions
```

---

## MCP server

The MCP server exposes screenscribe as a set of tools that any MCP-compatible client can call. The server handles video knowledge; the client provides conversation context.

Works with: Claude Code, Cursor, Windsurf, Continue, custom MCP agents, or any client that speaks the [Model Context Protocol](https://modelcontextprotocol.io).

### Tools exposed

| Tool                      | Description                                                                                                  |
| ------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `extract_transcript(url)` | Fetch transcript only — fast, free, no API cost. Default for most questions.                                 |
| `analyze_video(url)`        | Gemini watches the whole video → structured analysis (summary, sections, key moments). Cheap; no frames.    |
| `extract_video(url)`      | Full visual processing — Gemini picks frames, ffmpeg extracts, Claude Vision describes. When you need the images. |
| `extract_slides(url)`     | Extract presentation-quality slide frames. Returns paths to PNGs on disk.                                    |
| `get_video_analysis(id)`  | Read Gemini's whole-video analysis for a session.                                                            |
| `get_session(session_id)` | Return session content (transcript, frame descriptions, Gemini analysis) with `analysis_source` metadata.    |
| `list_sessions()`         | List all processed videos.                                                                                   |

The client picks the right tool based on context. For most questions `extract_transcript` is sufficient; `extract_video` when visual analysis is needed; `extract_slides` for screenshots or a deck.

### Register the MCP server

No paths, no venv, no JSON surgery — `uvx` runs the published package on demand.

**Claude Code** (one line):

```bash
claude mcp add screenscribe -- uvx screenscribe-mcp
```

**Cursor / Windsurf / Continue** (`.cursor/mcp.json` or your client's MCP config):

```json
{
  "mcpServers": {
    "screenscribe": {
      "command": "uvx",
      "args": ["screenscribe-mcp"]
    }
  }
}
```

Pass keys through if your client doesn't inherit your shell environment — add an `"env"` block:

```json
{
  "mcpServers": {
    "screenscribe": {
      "command": "uvx",
      "args": ["screenscribe-mcp"],
      "env": { "ANTHROPIC_API_KEY": "sk-ant-...", "GEMINI_API_KEY": "..." }
    }
  }
}
```

The server reads `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` from the environment (or a `.env` in the working directory). Restart your client after updating the config.

### Example usage

Open your MCP client inside any project, then ask naturally:

```
"Summarise the key points from https://youtu.be/dQw4w9WgXcQ"
```

```
"Extract slides from https://youtu.be/dQw4w9WgXcQ and list what each one covers"
```

```
"Based on the architecture explained in https://youtu.be/dQw4w9WgXcQ,
 refactor my src/api/router.py to follow that pattern"
```

The client will:

1. Call `extract_transcript(url)`, `analyze_video(url)`, or `extract_video(url)` depending on what's needed — cached after the first run
2. Call `get_session(id)` — loads transcript (+ Gemini analysis and frame descriptions if available), with `analysis_source` metadata indicating what data the answer is based on
3. Read your project files for context
4. Answer your question, citing whether it drew from transcript alone or transcript + visual analysis

---

## Configuration (`src/screenscribe/config.py`)

| Setting                        | Default             | Description                                                   |
| ------------------------------ | ------------------- | ------------------------------------------------------------- |
| `SCENE_THRESHOLD`              | `0.1`               | Scene change sensitivity. Lower = more frames captured.       |
| `MIN_FRAME_INTERVAL`           | `3.0s`              | Minimum gap between frames.                                   |
| `TRANSCRIPT_WINDOW`            | `15.0s`             | Transcript context pulled around each frame (±15s).           |
| `CLAUDE_MODEL`                 | `claude-sonnet-4-6` | Model for frame descriptions (vision).                        |
| `SYNTHESIS_MODEL`              | `claude-opus-4-6`   | Model for `ask` synthesis (8192 tokens).                      |
| `FRAME_SELECTION_MODEL`        | `claude-haiku-4-5`  | Cheap text model for transcript-driven frame/slide selection. |
| `FRAME_SELECTION_MAX`          | `25`                | Max frames from transcript analysis.                          |
| `SLIDE_SELECTION_MAX`          | `15`                | Max slides to extract.                                        |
| `SLIDE_SELECTION_MIN_INTERVAL` | `10.0s`             | Min gap between slides (wider than frames for diversity).     |
| `MAX_FRAMES_PER_BATCH`         | `8`                 | Frames per vision API call.                                   |
| `IMAGE_MAX_WIDTH`              | `1280px`            | Frames resized to this width before API call.                 |
| `GEMINI_MODEL`                 | `gemini-3.5-flash`  | Gemini model that watches the video for frame selection (when `GEMINI_API_KEY` is set). |
| `GEMINI_MEDIA_RESOLUTION_LOW`  | `True`              | Low-res video sampling (~100 tok/s) — cheaper; set `False` for finer visual detail. |
| `MAX_INLINE_TRANSCRIPT_CHARS`  | `100000`            | Max transcript chars `get_session` returns inline before pointing to the file instead. `None` = unlimited. |
| `FRAME_DESCRIPTION_MAX_TOKENS_PER_FRAME` | `1024`    | Output token budget per frame for vision descriptions (scales with batch size).      |

Models are configurable in `src/screenscribe/config.py`. Frame descriptions and answering use Anthropic's Claude; frame *selection* uses Gemini when a key is set (otherwise a Claude text model on the transcript).

---

## API keys

The base setup uses two keys:

- **`ANTHROPIC_API_KEY` (Claude)** — frame descriptions (`extract`) and answering (`ask`). Required.
- **`GEMINI_API_KEY` (Gemini)** — watches the video to select frames for `extract` and `slides`. Strongly recommended; without it, selection falls back to a cheap Claude text model reading the transcript. Get one from [Google AI Studio](https://aistudio.google.com/apikey).

These commands make **no** API calls and need no key:

- `extract --transcript-only` — fetches the transcript from YouTube only
- `get_session` / `list_sessions` — reads from disk

---

## Notes

- Works on **any kind of video** — selection and descriptions are content-agnostic. Use a question's `focus` (or the `--focus` flag) to steer toward a specific subject when you want to.
- `get_session` returns the **full transcript** (no silent truncation). For very long videos it returns a preview plus a `transcript_path` to the on-disk file and a `transcript_truncated` flag, rather than dropping data silently.
- Sessions are **machine-local** (`~/.video-analyzer/`). Installing on a new machine means re-running `extract` once per video.
- The `output/` directory in the project root is legacy and ignored by git. All current session data lives at `~/.video-analyzer/`.
- `youtube-transcript-api` v1.2.4+ uses an instance-based API: `YouTubeTranscriptApi().fetch(video_id)`. If you see `AttributeError: get_transcript`, you're on an old version — `pip install -U youtube-transcript-api`.
