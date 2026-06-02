# Scene change sensitivity: 0.0–1.0
# Lower = more sensitive. Chart screen recordings have subtle changes (lines drawn,
# zones highlighted) — 0.1 captures these without flooding with near-duplicates.
SCENE_THRESHOLD = 0.1

# Minimum seconds between two captured frames.
# Prevents bursting frames during slow zoom/pan animations.
MIN_FRAME_INTERVAL = 3.0

# Seconds of transcript to pull around each frame's timestamp (before + after).
TRANSCRIPT_WINDOW = 15.0

# Claude model for frame description (image batches — cheaper, fast).
CLAUDE_MODEL = "claude-sonnet-4-6"

# Claude model for final strategy synthesis (long code generation — use best available).
SYNTHESIS_MODEL = "claude-opus-4-6"

# How many frames to send per Claude API call.
# Each call = 1 image analysis batch + 1 synthesis call at the end.
MAX_FRAMES_PER_BATCH = 8

# Resize frames to this width (px) before sending to the API.
# Reduces token cost without losing chart readability.
IMAGE_MAX_WIDTH = 1280

# ── Transcript-driven frame selection ──────────────────────────────────────────
# Cheap text-only model to analyze transcript and pick visually important moments.
FRAME_SELECTION_MODEL = "claude-haiku-4-5-20251001"

# Maximum frames to select from transcript analysis.
FRAME_SELECTION_MAX = 25

# Minimum seconds between two transcript-selected frames.
FRAME_SELECTION_MIN_INTERVAL = 5.0

# ── Slide extraction ──────────────────────────────────────────────────────────
# Maximum slides to select from transcript analysis.
SLIDE_SELECTION_MAX = 15

# Minimum seconds between two selected slides.
SLIDE_SELECTION_MIN_INTERVAL = 10.0

# ── MCP response sizing ───────────────────────────────────────────────────────
# Max transcript characters returned INLINE by the get_session MCP tool. The full
# transcript is always persisted at <session>/transcript.json; when it exceeds
# this cap, get_session returns a preview plus `transcript_path` and
# `transcript_truncated: true` — never a silent cut. Set to None for no cap
# (always return the full transcript inline).
MAX_INLINE_TRANSCRIPT_CHARS = 100000

# Per-frame output token budget for Vision frame descriptions. A batch's
# max_tokens scales with the number of frames in it so descriptions are not cut
# off mid-sentence (previously a flat 2000 for up to 8 frames ≈ 250 tokens/frame).
FRAME_DESCRIPTION_MAX_TOKENS_PER_FRAME = 1024

# ── Gemini frame selection ────────────────────────────────────────────────────
# When GEMINI_API_KEY is set, Gemini watches the actual video to pick frames
# (sees the pixels) instead of guessing from the transcript. ffmpeg still extracts
# the chosen frames as PNGs. Falls back to transcript-based selection when no key.
# gemini-3.5-flash gives sharper, more precise picks; fall back to "gemini-2.5-flash"
# if your key lacks access to it.
GEMINI_MODEL = "gemini-3.5-flash"

# Low media resolution ≈ 100 tokens/sec of video (vs ~300 at default) — much
# cheaper and plenty for locating moments. Set False for finer visual detail.
GEMINI_MEDIA_RESOLUTION_LOW = True
