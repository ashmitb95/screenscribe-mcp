"""
video-analyzer MCP server.

Exposes tools to any MCP client (Claude Code, Claude Desktop, etc.):
  extract_transcript(url)    — fast: fetch transcript only (no API cost)
  extract_video(url)         — full: download video, extract frames, describe with Vision
  extract_slides(url)        — extract presentation-quality slide frames
  get_session(session_id)    — return session data with analysis source metadata
  list_sessions()            — list all processed videos

Claude Code usage:
  Add to ~/.claude.json or settings:
    {
      "mcpServers": {
        "video-analyzer": {
          "command": "python",
          "args": ["/home/ashmit/projects/video-analyzer/server.py"],
          "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }
        }
      }
    }

Then in Claude Code, just mention a YouTube URL — Claude will call
extract_video automatically if needed, then use get_session to answer
questions with full repo context.
"""

import json
import sys
from pathlib import Path

# Add project dir to path so imports work when run directly
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

# Load .env from the project directory (picks up ANTHROPIC_API_KEY)
try:
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env")
except ImportError:
    pass

from mcp.server.fastmcp import FastMCP

from analyzer import describe_frames
from config import (
    CLAUDE_MODEL,
    FRAME_SELECTION_MAX,
    FRAME_SELECTION_MIN_INTERVAL,
    FRAME_SELECTION_MODEL,
    IMAGE_MAX_WIDTH,
    MAX_FRAMES_PER_BATCH,
    MAX_INLINE_TRANSCRIPT_CHARS,
    SLIDE_SELECTION_MAX,
    SLIDE_SELECTION_MIN_INTERVAL,
    TRANSCRIPT_WINDOW,
)
from downloader import download_video, fetch_transcript
from frame_extractor import extract_frames_at_timestamps
from session import (
    frames_dir as session_frames_dir,
    list_sessions as _list_sessions,
    load_session,
    save_session,
    session_dir,
    session_exists,
    slides_dir as session_slides_dir,
)

import re

mcp = FastMCP("video-analyzer")


def _extract_video_id(url: str) -> str:
    patterns = [
        r"youtu\.be/([^?&/]+)",
        r"youtube\.com/watch\?v=([^&]+)",
        r"youtube\.com/shorts/([^?&/]+)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    raise ValueError(f"Cannot extract video ID from: {url}")


def _get_title(url: str) -> str:
    """Fetch video title without downloading."""
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("title", "Unknown")
    except Exception:
        return "Unknown"


@mcp.tool()
def extract_transcript(url: str) -> str:
    """
    Fetch the transcript of a YouTube video. Fast and free — no video
    download, no frame analysis, no API credits used.

    Use this by default when a user shares a YouTube URL and wants to
    discuss, summarize, or ask questions about its content. For most
    videos the transcript alone is sufficient.

    Only use extract_video instead if the user specifically needs
    visual/frame analysis (e.g. "what's shown on screen", charts,
    diagrams, code on screen).

    Returns: session_id to use with get_session.
    """
    video_id = _extract_video_id(url)

    if session_exists(video_id):
        session = load_session(video_id)
        return json.dumps({
            "status": "already_extracted",
            "session_id": video_id,
            "title": session.get("title", "Unknown"),
            "frame_count": session.get("frame_count", 0),
            "message": f"Session already exists. Use get_session('{video_id}') to access it.",
        })

    s_dir = session_dir(video_id)
    title = _get_title(url)

    s_dir.mkdir(parents=True, exist_ok=True)
    transcript = fetch_transcript(video_id, s_dir)

    duration = 0.0
    if transcript:
        last = transcript[-1]
        duration = last["start"] + last["duration"]

    save_session(
        video_id=video_id,
        url=url,
        title=title,
        duration=duration,
        transcript=transcript,
        frame_descriptions=[],
        frames=[],
    )

    return json.dumps({
        "status": "success",
        "session_id": video_id,
        "title": title,
        "frame_count": 0,
        "duration_seconds": duration,
        "mode": "transcript_only",
        "transcript_segments": len(transcript),
        "message": f"Transcript-only session ready. Call get_session('{video_id}') to access the content.",
    })


@mcp.tool()
def extract_video(
    url: str,
    focus: str = "",
    time_range: str = "",
    timestamps: str = "",
) -> str:
    """
    Full visual processing of a YouTube video: downloads the video,
    extracts key frames, and generates visual descriptions using
    Claude Vision. Slow (~2 min) and uses API credits.

    Only use this when the user specifically needs visual analysis
    (e.g. charts, diagrams, code shown on screen, UI elements).
    For most questions about a video, extract_transcript is sufficient.

    Optional parameters for customization:
    - focus: Natural language instruction to narrow what frames to
      extract. Examples: "only code examples", "architecture diagrams".
      The AI will prioritize moments matching this description.
    - time_range: Restrict extraction to a portion of the video.
      Format: "START-END" where START/END are seconds or MM:SS.
      Examples: "300-900", "5:00-15:00".
    - timestamps: Comma-separated exact timestamps to extract,
      bypassing AI selection. Format: seconds or MM:SS.
      Examples: "330,600", "5:30,10:00".

    Returns: session_id to use with get_session.
    """
    video_id = _extract_video_id(url)
    has_custom_params = bool(focus or time_range or timestamps)

    if session_exists(video_id) and not has_custom_params:
        session = load_session(video_id)
        return json.dumps({
            "status": "already_extracted",
            "session_id": video_id,
            "title": session.get("title", "Unknown"),
            "frame_count": session.get("frame_count", 0),
            "message": f"Session already exists. Use get_session('{video_id}') to access it.",
        })

    try:
        s_dir = session_dir(video_id)
        f_dir = session_frames_dir(video_id)
        title = _get_title(url)

        video_path, _, chapters = download_video(url, s_dir)
        transcript = fetch_transcript(video_id, s_dir)

        # Analyze transcript for key visual moments
        from transcript_selector import select_frames_from_transcript
        selections = select_frames_from_transcript(
            transcript=transcript,
            model=FRAME_SELECTION_MODEL,
            max_frames=FRAME_SELECTION_MAX,
            min_interval=FRAME_SELECTION_MIN_INTERVAL,
            chapters=chapters,
            focus=focus,
            time_range=time_range,
            timestamps=timestamps,
        )

        # Extract targeted frames
        frames = extract_frames_at_timestamps(
            video_path=video_path,
            frames_dir=f_dir,
            selections=selections,
            max_width=IMAGE_MAX_WIDTH,
            save_metadata=not has_custom_params,
        )

        if not frames:
            return json.dumps({"status": "error", "message": "No frames extracted."})

        # Describe frames
        descriptions = describe_frames(
            frames=frames,
            transcript=transcript,
            model=CLAUDE_MODEL,
            transcript_window=TRANSCRIPT_WINDOW,
            batch_size=MAX_FRAMES_PER_BATCH,
        )

        duration = frames[-1]["timestamp"] if frames else 0.0

        if not has_custom_params:
            save_session(
                video_id=video_id,
                url=url,
                title=title,
                duration=duration,
                transcript=transcript,
                frame_descriptions=descriptions,
                frames=frames,
            )

        return json.dumps({
            "status": "success",
            "session_id": video_id,
            "title": title,
            "frame_count": len(frames),
            "duration_seconds": duration,
            "message": f"Session ready. Call get_session('{video_id}') to access the content.",
        })
    except ValueError as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def extract_slides(
    url: str,
    focus: str = "",
    time_range: str = "",
    timestamps: str = "",
) -> str:
    """
    Extract presentation-quality slides from a YouTube video.

    Analyzes the transcript to find moments where complete diagrams,
    charts, code, or summaries are shown on screen, then extracts
    those frames as PNG images. Faster and cheaper than extract_video
    — only uses a cheap text model for selection, no Vision API.

    Use this when the user needs visual aids, key screenshots, or
    a slide deck from a video.

    Optional parameters for customization:
    - focus: Natural language instruction to narrow what slides to
      extract. Examples: "only code examples", "architecture diagrams",
      "the section about authentication". When set, the AI prioritizes
      moments matching this description.
    - time_range: Restrict extraction to a portion of the video.
      Format: "START-END" where START/END are seconds or MM:SS.
      Examples: "300-900", "5:00-15:00". Only extracts slides
      within this window.
    - timestamps: Comma-separated list of exact timestamps to extract,
      bypassing AI selection entirely. Format: seconds or MM:SS.
      Examples: "330,600", "5:30,10:00". Use when you know exactly
      which moments to capture.

    Returns: slide paths, timestamps, and descriptions.
    """
    video_id = _extract_video_id(url)
    s_dir = session_dir(video_id)
    sl_dir = session_slides_dir(video_id)
    has_custom_params = bool(focus or time_range or timestamps)

    # Cache check: only use cache when no custom extraction parameters
    slides_meta = sl_dir / "frames.json"
    if slides_meta.exists() and not has_custom_params:
        cached_slides = json.loads(slides_meta.read_text())
        if cached_slides:
            return json.dumps({
                "status": "cached",
                "session_id": video_id,
                "slide_count": len(cached_slides),
                "slides": [
                    {
                        "index": i + 1,
                        "timestamp": s["timestamp"],
                        "path": s["path"],
                        "reason": s.get("reason", ""),
                    }
                    for i, s in enumerate(cached_slides)
                ],
                "message": f"Slides already extracted. {len(cached_slides)} slides available.",
            })

    try:
        # Ensure video is downloaded
        video_path = None
        if s_dir.exists():
            candidates = [f for f in s_dir.iterdir()
                          if f.suffix in ('.mp4', '.mkv', '.webm') and f.stem != 'thumbnail']
            if candidates:
                video_path = candidates[0]

        if video_path is None:
            video_path, _, chapters = download_video(url, s_dir)
        else:
            chapters_file = s_dir / "chapters.json"
            chapters = json.loads(chapters_file.read_text()) if chapters_file.exists() else []

        # Ensure transcript is available
        transcript_file = s_dir / "transcript.json"
        if transcript_file.exists():
            transcript = json.loads(transcript_file.read_text())
        else:
            transcript = fetch_transcript(video_id, s_dir)

        if not transcript:
            return json.dumps({
                "status": "error",
                "message": "No transcript available for this video. Cannot select slides.",
            })

        # Select slide-worthy moments
        from transcript_selector import select_slides_from_transcript
        selections = select_slides_from_transcript(
            transcript=transcript,
            model=FRAME_SELECTION_MODEL,
            max_slides=SLIDE_SELECTION_MAX,
            min_interval=SLIDE_SELECTION_MIN_INTERVAL,
            chapters=chapters,
            focus=focus,
            time_range=time_range,
            timestamps=timestamps,
        )

        if not selections:
            return json.dumps({
                "status": "error",
                "message": "Could not identify any slide-worthy moments from transcript.",
            })

        # Extract frames into slides/ directory
        slides = extract_frames_at_timestamps(
            video_path=video_path,
            frames_dir=sl_dir,
            selections=selections,
            max_width=IMAGE_MAX_WIDTH,
            save_metadata=not has_custom_params,
        )

        if not slides:
            return json.dumps({"status": "error", "message": "No slide frames extracted."})

        # Ensure a basic session exists (for list_sessions / get_session)
        if not session_exists(video_id):
            title = _get_title(url)
            duration = 0.0
            if transcript:
                last = transcript[-1]
                duration = last["start"] + last.get("duration", 0)
            save_session(
                video_id=video_id,
                url=url,
                title=title,
                duration=duration,
                transcript=transcript,
                frame_descriptions=[],
                frames=[],
            )

        return json.dumps({
            "status": "success",
            "session_id": video_id,
            "slide_count": len(slides),
            "slides": [
                {
                    "index": i + 1,
                    "timestamp": s["timestamp"],
                    "path": s["path"],
                    "reason": s.get("reason", ""),
                }
                for i, s in enumerate(slides)
            ],
            "message": f"Extracted {len(slides)} slides. Paths point to PNG files on disk.",
        })
    except ValueError as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def get_session(session_id: str) -> str:
    """
    Return the full processed content of a video session:
    transcript and (if available) frame-by-frame visual descriptions.

    The response includes an 'analysis_source' field that tells you
    exactly what data is available. ALWAYS mention the source when
    answering questions — e.g. "Based on the transcript..." or
    "Based on transcript + visual analysis of N frames...".

    Use the returned content to answer questions about the video.
    You (Claude) provide any codebase/project context from the
    current conversation — no need to pass it here.

    Args:
        session_id: The video ID returned by extract_transcript, extract_video, or list_sessions.
    """
    try:
        session = load_session(session_id)
    except FileNotFoundError:
        return json.dumps({
            "error": f"No session found for '{session_id}'.",
            "hint": "Run extract_transcript(url) or extract_video(url) first.",
        })

    full_transcript = " ".join(seg["text"] for seg in session["transcript"])
    frame_descriptions = session.get("frame_descriptions", [])
    frames = session.get("frames", [])

    # Build analysis source metadata
    if frame_descriptions:
        analysis_source = {
            "type": "transcript + video analysis",
            "frames_analyzed": len(frames),
            "frame_timestamps": [
                {"timestamp": f["timestamp"], "reason": f.get("reason", "")}
                for f in frames
            ],
        }
    else:
        analysis_source = {
            "type": "transcript only",
            "note": "No visual/frame analysis was performed. Answers are based solely on the transcript.",
        }

    # Return the full transcript inline. Only when it exceeds a generous cap do
    # we return a preview and point to the on-disk file — never a silent cut.
    cap = MAX_INLINE_TRANSCRIPT_CHARS
    truncated = cap is not None and len(full_transcript) > cap
    transcript_inline = full_transcript[:cap] if truncated else full_transcript

    return json.dumps({
        "video_id": session["video_id"],
        "title": session.get("title", "Unknown"),
        "url": session.get("url", ""),
        "duration_seconds": session.get("duration", 0),
        "frame_count": session.get("frame_count", 0),
        "extracted_at": session.get("extracted_at", ""),
        "analysis_source": analysis_source,
        "frame_descriptions": frame_descriptions,
        "transcript": transcript_inline,
        "transcript_chars": len(full_transcript),
        "transcript_truncated": truncated,
        "transcript_path": str(session_dir(session["video_id"]) / "transcript.json"),
    })


@mcp.tool()
def list_sessions() -> str:
    """
    List all videos that have been processed and are available to query.
    Returns session IDs, titles, durations, and extraction timestamps.
    """
    sessions = _list_sessions()
    if not sessions:
        return json.dumps({
            "sessions": [],
            "message": "No sessions yet. Run extract_video(url) to process a video.",
        })
    return json.dumps({"sessions": sessions})


if __name__ == "__main__":
    mcp.run()
