"""
screenscribe MCP server.

Built on Gemini (watches the video to pick frames) + Claude (describes
frames and answers questions), with a free transcript layer alongside.

Exposes tools to any MCP client (Claude Code, Claude Desktop, etc.):
  extract_transcript(url)    — fast: fetch transcript only (no API cost)
  analyze_video(url)         — cheap: Gemini watches the whole video → structured analysis
  extract_video(url)         — full: Gemini picks frames, ffmpeg extracts, Claude Vision describes
  extract_slides(url)        — Gemini picks complete on-screen visuals, extracted as PNGs
  get_video_analysis(id)     — read Gemini's whole-video analysis
  get_session(session_id)    — return session data with analysis source metadata
  list_sessions()            — list all processed videos

Claude Code usage:
  claude mcp add screenscribe -- uvx screenscribe-mcp

  Or add to ~/.claude.json directly:
    {
      "mcpServers": {
        "screenscribe": {
          "command": "uvx",
          "args": ["screenscribe-mcp"],
          "env": { "ANTHROPIC_API_KEY": "sk-ant-...", "GEMINI_API_KEY": "..." }
        }
      }
    }
  (Keys are also read from the shell environment / a .env in the working dir.)

Then in Claude Code, just mention a YouTube URL — Claude will call
extract_video automatically if needed, then use get_session to answer
questions with full repo context.
"""

import json
import re

from dotenv import load_dotenv
load_dotenv()

from mcp.server.fastmcp import FastMCP

from screenscribe.analyzer import describe_frames
from screenscribe.config import (
    CLAUDE_MODEL,
    FRAME_SELECTION_MAX,
    FRAME_SELECTION_MIN_INTERVAL,
    FRAME_SELECTION_MODEL,
    GEMINI_MEDIA_RESOLUTION_LOW,
    GEMINI_MODEL,
    IMAGE_MAX_WIDTH,
    MAX_FRAMES_PER_BATCH,
    MAX_INLINE_TRANSCRIPT_CHARS,
    SLIDE_SELECTION_MAX,
    SLIDE_SELECTION_MIN_INTERVAL,
    TRANSCRIPT_WINDOW,
)
from screenscribe.downloader import download_video, fetch_transcript
from screenscribe.frame_extractor import extract_frames_at_timestamps
from screenscribe.session import (
    frames_dir as session_frames_dir,
    list_sessions as _list_sessions,
    load_analysis,
    load_session,
    save_analysis,
    save_session,
    session_dir,
    session_exists,
    slides_dir as session_slides_dir,
)

mcp = FastMCP("screenscribe")


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
    Full visual analysis of a YouTube video: Gemini watches the video to
    pick the key frames, ffmpeg extracts them, and Claude Vision describes
    each one. Slow (~2 min) and uses API credits. Works on any kind of
    video, not just screen recordings.

    Only use this when the user specifically needs visual analysis
    (what's shown on screen, diagrams, demonstrations, UI, scenes, etc.).
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

        # Identify key visual moments — Gemini watches the video when a key is
        # set, otherwise falls back to transcript-based picking.
        from screenscribe.gemini_selector import select_frames
        video_duration = (
            transcript[-1]["start"] + transcript[-1].get("duration", 0)
            if transcript else 0.0
        )
        selections = select_frames(
            url,
            transcript,
            transcript_model=FRAME_SELECTION_MODEL,
            gemini_model=GEMINI_MODEL,
            max_frames=FRAME_SELECTION_MAX,
            min_interval=FRAME_SELECTION_MIN_INTERVAL,
            chapters=chapters,
            focus=focus,
            time_range=time_range,
            timestamps=timestamps,
            video_duration=video_duration,
            media_resolution_low=GEMINI_MEDIA_RESOLUTION_LOW,
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

    Gemini watches the video to find moments where a complete,
    self-contained visual is on screen (diagram, chart, scene, code,
    summary), then ffmpeg extracts those frames as PNG images. Faster
    and cheaper than extract_video — frame selection only, no Claude
    Vision descriptions.

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

        # Select slide-worthy moments — Gemini watches the video when a key is
        # set, otherwise falls back to transcript-based picking.
        from screenscribe.gemini_selector import select_slides
        video_duration = (
            transcript[-1]["start"] + transcript[-1].get("duration", 0)
            if transcript else 0.0
        )
        selections = select_slides(
            url,
            transcript,
            transcript_model=FRAME_SELECTION_MODEL,
            gemini_model=GEMINI_MODEL,
            max_slides=SLIDE_SELECTION_MAX,
            min_interval=SLIDE_SELECTION_MIN_INTERVAL,
            chapters=chapters,
            focus=focus,
            time_range=time_range,
            timestamps=timestamps,
            video_duration=video_duration,
            media_resolution_low=GEMINI_MEDIA_RESOLUTION_LOW,
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
def analyze_video(url: str, focus: str = "", time_range: str = "") -> str:
    """
    Whole-video visual analysis with Gemini — cheap and fast. Gemini watches the
    entire video and returns a structured understanding: a summary, a section/
    topic breakdown with timestamps, the key moments (with what is on screen),
    and notable on-screen text/data. No download, no frame extraction, no Claude
    Vision pass.

    This is the sweet spot between extract_transcript (free, words only) and
    extract_video (frames described by Claude Vision): whole-video visual
    coverage for a fraction of the cost. Use it to summarise, outline, or answer
    "what is shown / what happens" questions. Use extract_video / extract_slides
    only when you need the actual frame images saved on disk.

    Optional:
    - focus: pay special attention to a subject (e.g. "the demo", "pricing").
    - time_range: restrict to a portion, "START-END" in seconds or MM:SS.

    Returns: session_id; read the full analysis with get_video_analysis.
    """
    video_id = _extract_video_id(url)
    has_custom = bool(focus or time_range)

    if load_analysis(video_id) is not None and not has_custom:
        return json.dumps({
            "status": "already_analyzed",
            "session_id": video_id,
            "message": f"Analysis already exists. Use get_video_analysis('{video_id}').",
        })

    try:
        from screenscribe.gemini_analyzer import analyze_video_with_gemini, gemini_available
        if not gemini_available():
            return json.dumps({
                "status": "error",
                "message": "analyze_video requires GEMINI_API_KEY (Gemini watches the video).",
            })

        analysis = analyze_video_with_gemini(
            url, GEMINI_MODEL, focus=focus, time_range=time_range,
            media_resolution_low=GEMINI_MEDIA_RESOLUTION_LOW,
        )
        if not has_custom:
            save_analysis(video_id, analysis)

        # Ensure a session exists (with transcript) so get_session/ask can use it.
        if not session_exists(video_id):
            s_dir = session_dir(video_id)
            s_dir.mkdir(parents=True, exist_ok=True)
            try:
                transcript = fetch_transcript(video_id, s_dir)
            except Exception:
                transcript = []
            duration = 0.0
            if transcript:
                last = transcript[-1]
                duration = last["start"] + last.get("duration", 0)
            save_session(
                video_id=video_id, url=url, title=_get_title(url),
                duration=duration, transcript=transcript,
                frame_descriptions=[], frames=[],
            )

        resp = {
            "status": "success",
            "session_id": video_id,
            "summary": analysis.get("summary", ""),
            "section_count": len(analysis.get("sections", [])),
            "key_moment_count": len(analysis.get("key_moments", [])),
            "message": f"Analyzed. Read the full analysis with get_video_analysis('{video_id}').",
        }
        if has_custom:
            resp["analysis"] = analysis  # one-off (not cached) — return inline
        return json.dumps(resp)
    except ValueError as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def get_video_analysis(session_id: str) -> str:
    """
    Return Gemini's structured whole-video analysis for a session — summary,
    sections (with timestamps), key moments, and on-screen text — if
    analyze_video has been run for it.
    """
    analysis = load_analysis(session_id)
    if analysis is None:
        return json.dumps({
            "error": f"No Gemini analysis for '{session_id}'.",
            "hint": "Run analyze_video(url) first.",
        })
    return json.dumps({"session_id": session_id, "analysis": analysis})


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
    gemini_analysis = load_analysis(session_id)

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
    elif gemini_analysis:
        analysis_source = {
            "type": "transcript + Gemini whole-video analysis",
            "note": "Gemini watched the whole video; see gemini_analysis for the visual content.",
        }
    else:
        analysis_source = {
            "type": "transcript only",
            "note": "No visual analysis was performed. Answers are based solely on the transcript.",
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
        "gemini_analysis": gemini_analysis,
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


def run():
    """Console-script entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    run()
