"""
video-analyzer v0.2

Commands:
  extract <url>              Process a video once — downloads, extracts frames,
                             generates descriptions, saves a session.
  extract <url> --transcript-only
                             Fetch transcript only — no video download, no
                             frame analysis. Fast and free (no API calls).
  slides <url>               Extract presentation-quality slide frames.
  ask <session_id> <question> Ask anything about a processed video.
  sessions                   List all processed videos.

Examples:
  python main.py extract "https://youtu.be/RnP08K2SAZs"
  python main.py extract "https://youtu.be/RnP08K2SAZs" --transcript-only
  python main.py extract "https://youtu.be/RnP08K2SAZs" --focus "architecture diagrams"
  python main.py extract "https://youtu.be/RnP08K2SAZs" --time-range "5:00-15:00"
  python main.py slides "https://youtu.be/RnP08K2SAZs"
  python main.py slides "https://youtu.be/RnP08K2SAZs" --focus "code examples"
  python main.py slides "https://youtu.be/RnP08K2SAZs" --timestamps "5:30,10:00,22:15"
  python main.py ask RnP08K2SAZs "What is the entry trigger?"
  python main.py ask RnP08K2SAZs "implement this as a BaseStrategy" \\
      --context ~/algo-bot/backend/core/strategy.py \\
      --context ~/algo-bot/backend/core/liquidity_detector.py
  python main.py ask RnP08K2SAZs "how does this relate?" --stdin < my_notes.md
  python main.py sessions
"""

import argparse
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import yt_dlp

from analyzer import describe_frames
from asker import ask as ask_session
from config import (
    CLAUDE_MODEL,
    FRAME_SELECTION_MAX,
    FRAME_SELECTION_MIN_INTERVAL,
    FRAME_SELECTION_MODEL,
    GEMINI_MEDIA_RESOLUTION_LOW,
    GEMINI_MODEL,
    IMAGE_MAX_WIDTH,
    MAX_FRAMES_PER_BATCH,
    MIN_FRAME_INTERVAL,
    SCENE_THRESHOLD,
    SLIDE_SELECTION_MAX,
    SLIDE_SELECTION_MIN_INTERVAL,
    SYNTHESIS_MODEL,
    TRANSCRIPT_WINDOW,
)
from context import load_context
from downloader import download_video, fetch_transcript
from frame_extractor import extract_frames, extract_frames_at_timestamps
from session import (
    frames_dir as session_frames_dir,
    list_sessions,
    load_session,
    save_session,
    session_dir,
    session_exists,
)
from gemini_selector import select_frames, select_slides


def extract_video_id(url: str) -> str:
    patterns = [
        r"youtu\.be/([^?&/]+)",
        r"youtube\.com/watch\?v=([^&]+)",
        r"youtube\.com/embed/([^?&/]+)",
        r"youtube\.com/shorts/([^?&/]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError(f"Cannot extract video ID from: {url}")


def get_video_title(url: str) -> str:
    """Fetch video title without downloading."""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("title", "Unknown")
    except Exception:
        return "Unknown"


# ── extract ───────────────────────────────────────────────────────────────────

def cmd_extract(args):
    video_id = extract_video_id(args.url)
    s_dir = session_dir(video_id)
    f_dir = session_frames_dir(video_id)
    progress_file = s_dir / "descriptions_progress.jsonl"
    use_transcript_select = not args.no_transcript_select
    transcript_only = args.transcript_only

    if transcript_only:
        steps = "1"
    elif use_transcript_select:
        steps = "4"
    else:
        steps = "3"

    print(f"\n{'=' * 44}")
    print(f"  video-analyzer extract")
    print(f"  Video ID : {video_id}")
    print(f"  Session  : {s_dir}")
    if transcript_only:
        print(f"  Mode     : transcript-only (no frames)")
    elif use_transcript_select:
        print(f"  Mode     : transcript-driven ({args.max_frames} max frames)")
    else:
        print(f"  Mode     : scene-detection (legacy)")
    if args.resume:
        print(f"  Resume   : yes")
    if args.focus:
        print(f"  Focus    : {args.focus}")
    if args.time_range:
        print(f"  Range    : {args.time_range}")
    if args.timestamps:
        print(f"  Stamps   : {args.timestamps}")
    print(f"{'=' * 44}\n")

    if session_exists(video_id) and not args.force and not args.resume:
        print(f"Session already exists. Use --force to re-extract.")
        print(f"Run: python main.py ask {video_id} \"your question\"")
        return

    # ── Transcript-only mode ──────────────────────────────────────────────
    if transcript_only:
        print(f"[1/1] Fetching transcript...")
        title = get_video_title(args.url)
        s_dir.mkdir(parents=True, exist_ok=True)
        transcript = fetch_transcript(video_id, s_dir)

        # Estimate duration from last transcript segment
        duration = 0.0
        if transcript:
            last = transcript[-1]
            duration = last["start"] + last["duration"]

        session_path = save_session(
            video_id=video_id,
            url=args.url,
            title=title,
            duration=duration,
            transcript=transcript,
            frame_descriptions=[],
            frames=[],
        )

        print(f"\n{'=' * 44}")
        print(f"  DONE — transcript-only session saved")
        print(f"  {session_path}")
        print(f"  Transcript: {len(transcript)} segments, {duration:.0f}s")
        print(f"\n  Now ask anything:")
        print(f"  python main.py ask {video_id} \"your question\"")
        print(f"{'=' * 44}\n")
        return

    # ── Full extraction (frames + descriptions) ──────────────────────────
    if args.resume:
        # ── Resume: load existing frames + transcript from disk ──
        frames_json = f_dir / "frames.json"
        transcript_json = s_dir / "transcript.json"

        if not frames_json.exists() or not transcript_json.exists():
            print("ERROR: Cannot resume — frames.json or transcript.json not found.")
            print("Run without --resume first to complete steps 1-3.")
            sys.exit(1)

        print(f"[1/{steps}] Downloading video and transcript... SKIPPED (resume)")
        title = get_video_title(args.url)
        transcript = json.loads(transcript_json.read_text())
        chapters_json = s_dir / "chapters.json"
        chapters = json.loads(chapters_json.read_text()) if chapters_json.exists() else []
        print(f"  Loaded transcript: {len(transcript)} segments")

        if use_transcript_select:
            print(f"\n[2/{steps}] Analyzing transcript... SKIPPED (resume)")

        step_frames = "3" if use_transcript_select else "2"
        print(f"\n[{step_frames}/{steps}] Extracting key frames... SKIPPED (resume)")
        frames = json.loads(frames_json.read_text())
        print(f"  Loaded frames: {len(frames)} from {frames_json}")

        # Load any partial description progress
        existing_descriptions = []
        if progress_file.exists():
            for line in progress_file.read_text().splitlines():
                if line.strip():
                    existing_descriptions.append(json.loads(line))
            if existing_descriptions:
                print(f"  Resuming descriptions: {len(existing_descriptions)} batches already done")
    else:
        # ── Fresh run ──
        # 1 — Download
        print(f"[1/{steps}] Downloading video and transcript...")
        title = get_video_title(args.url)
        video_path, _, chapters = download_video(args.url, s_dir)
        transcript = fetch_transcript(video_id, s_dir)

        if use_transcript_select:
            # 2 — Identify key visual moments (Gemini watches the video if a key
            #     is set; otherwise falls back to transcript-based picking).
            print(f"\n[2/{steps}] Identifying key visual moments...")
            video_duration = (
                transcript[-1]["start"] + transcript[-1].get("duration", 0)
                if transcript else 0.0
            )
            selections = select_frames(
                args.url,
                transcript,
                transcript_model=FRAME_SELECTION_MODEL,
                gemini_model=GEMINI_MODEL,
                max_frames=args.max_frames,
                min_interval=FRAME_SELECTION_MIN_INTERVAL,
                chapters=chapters,
                focus=args.focus,
                time_range=args.time_range,
                timestamps=args.timestamps,
                video_duration=video_duration,
                media_resolution_low=GEMINI_MEDIA_RESOLUTION_LOW,
            )
            print(f"  Identified {len(selections)} key moments")
            for i, sel in enumerate(selections, 1):
                print(f"    {i:2d}. {sel['timestamp']:.1f}s — {sel['reason']}")

            # 3 — Extract targeted frames
            print(f"\n[3/{steps}] Extracting {len(selections)} targeted frames...")
            frames = extract_frames_at_timestamps(
                video_path=video_path,
                frames_dir=f_dir,
                selections=selections,
                max_width=IMAGE_MAX_WIDTH,
            )
        else:
            # Legacy: scene detection + fallback
            print(f"\n[2/{steps}] Extracting key frames (scene detection)...")
            frames = extract_frames(
                video_path=video_path,
                frames_dir=f_dir,
                threshold=args.threshold,
                min_interval=args.interval,
                max_width=IMAGE_MAX_WIDTH,
            )

        existing_descriptions = []
        # Clear any stale progress file from a previous failed run
        if progress_file.exists():
            progress_file.unlink()

    if not frames:
        print("ERROR: No frames extracted.")
        sys.exit(1)

    # Final step — Describe frames
    step_describe = steps
    print(f"\n[{step_describe}/{steps}] Describing {len(frames)} frames with {CLAUDE_MODEL}...")
    descriptions = describe_frames(
        frames=frames,
        transcript=transcript,
        model=CLAUDE_MODEL,
        transcript_window=TRANSCRIPT_WINDOW,
        batch_size=MAX_FRAMES_PER_BATCH,
        progress_file=progress_file,
        existing_descriptions=existing_descriptions,
    )

    # Get video duration from frames metadata
    duration = frames[-1]["timestamp"] if frames else 0.0

    # Save session
    session_path = save_session(
        video_id=video_id,
        url=args.url,
        title=title,
        duration=duration,
        transcript=transcript,
        frame_descriptions=descriptions,
        frames=frames,
    )

    # Clean up progress file on success
    if progress_file.exists():
        progress_file.unlink()

    print(f"\n{'=' * 44}")
    print(f"  DONE — session saved")
    print(f"  {session_path}")
    print(f"\n  Now ask anything:")
    print(f"  python main.py ask {video_id} \"your question\"")
    print(f"{'=' * 44}\n")


# ── ask ───────────────────────────────────────────────────────────────────────

def cmd_ask(args):
    try:
        session = load_session(args.session_id)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    context = load_context(args.context or [], from_stdin=args.stdin)

    print(f"\nAsking about: {session.get('title', args.session_id)}")
    if context:
        print(f"Context: {len(context)} chars injected")
    print()

    answer = ask_session(
        session=session,
        question=args.question,
        context=context,
        model=SYNTHESIS_MODEL,
    )

    print(answer)

    # Save to session queries log
    queries_path = session_dir(args.session_id) / "queries.jsonl"
    with open(queries_path, "a") as f:
        f.write(json.dumps({
            "question": args.question,
            "context_sources": args.context or [],
            "answer": answer,
        }) + "\n")


# ── sessions ──────────────────────────────────────────────────────────────────

def cmd_sessions(_args):
    sessions = list_sessions()
    if not sessions:
        print("No sessions found. Run: python main.py extract <url>")
        return

    print(f"\n{'─' * 60}")
    print(f"  {'SESSION ID':<20} {'FRAMES':>6}  {'DUR':>5}  TITLE")
    print(f"{'─' * 60}")
    for s in sessions:
        dur = f"{int(s['duration'] // 60)}m{int(s['duration'] % 60):02d}s"
        title = s['title'][:30] + ("…" if len(s['title']) > 30 else "")
        print(f"  {s['session_id']:<20} {s['frames']:>6}  {dur:>5}  {title}")
    print(f"{'─' * 60}\n")


# ── slides ────────────────────────────────────────────────────────────────────

def cmd_slides(args):
    video_id = extract_video_id(args.url)
    s_dir = session_dir(video_id)
    sl_dir = s_dir / "slides"

    has_custom_params = bool(args.focus or args.time_range or args.timestamps)

    print(f"\n{'=' * 44}")
    print(f"  video-analyzer slides")
    print(f"  Video ID : {video_id}")
    print(f"  Session  : {s_dir}")
    print(f"  Max      : {args.max_slides} slides")
    if args.focus:
        print(f"  Focus    : {args.focus}")
    if args.time_range:
        print(f"  Range    : {args.time_range}")
    if args.timestamps:
        print(f"  Stamps   : {args.timestamps}")
    print(f"{'=' * 44}\n")

    # Cache check — skip cache when custom params are set
    slides_meta = sl_dir / "frames.json"
    if slides_meta.exists() and not args.force and not has_custom_params:
        slides = json.loads(slides_meta.read_text())
        print(f"Slides already extracted ({len(slides)} slides).")
        print(f"Use --force to re-extract.\n")
        for i, s in enumerate(slides, 1):
            print(f"  {i:2d}. {s['timestamp']:.1f}s — {s.get('reason', '')}")
            print(f"      {s['path']}")
        return

    # Step 1: Ensure video + transcript
    print(f"[1/3] Ensuring video and transcript...")

    video_path = None
    if s_dir.exists():
        candidates = [f for f in s_dir.iterdir()
                      if f.suffix in ('.mp4', '.mkv', '.webm') and f.stem != 'thumbnail']
        if candidates:
            video_path = candidates[0]
            print(f"  Video found: {video_path.name}")

    if video_path is None:
        title = get_video_title(args.url)
        video_path, _, chapters = download_video(args.url, s_dir)
    else:
        title = get_video_title(args.url)
        chapters_file = s_dir / "chapters.json"
        chapters = json.loads(chapters_file.read_text()) if chapters_file.exists() else []

    transcript_file = s_dir / "transcript.json"
    if transcript_file.exists():
        transcript = json.loads(transcript_file.read_text())
        print(f"  Transcript found: {len(transcript)} segments")
    else:
        transcript = fetch_transcript(video_id, s_dir)

    # Step 2: Select slide moments (Gemini watches the video if a key is set;
    # otherwise falls back to transcript-based picking).
    print(f"\n[2/3] Identifying slide-worthy moments...")
    video_duration = (
        transcript[-1]["start"] + transcript[-1].get("duration", 0)
        if transcript else 0.0
    )
    selections = select_slides(
        args.url,
        transcript,
        transcript_model=FRAME_SELECTION_MODEL,
        gemini_model=GEMINI_MODEL,
        max_slides=args.max_slides,
        min_interval=SLIDE_SELECTION_MIN_INTERVAL,
        chapters=chapters,
        focus=args.focus,
        time_range=args.time_range,
        timestamps=args.timestamps,
        video_duration=video_duration,
        media_resolution_low=GEMINI_MEDIA_RESOLUTION_LOW,
    )
    print(f"  Identified {len(selections)} slide moments:")
    for i, sel in enumerate(selections, 1):
        print(f"    {i:2d}. {sel['timestamp']:.1f}s — {sel['reason']}")

    # Step 3: Extract frames
    print(f"\n[3/3] Extracting {len(selections)} slides...")
    slides = extract_frames_at_timestamps(
        video_path=video_path,
        frames_dir=sl_dir,
        selections=selections,
        max_width=IMAGE_MAX_WIDTH,
    )

    # Ensure session exists
    if not session_exists(video_id):
        duration = transcript[-1]["start"] + transcript[-1].get("duration", 0) if transcript else 0.0
        save_session(
            video_id=video_id, url=args.url, title=title,
            duration=duration, transcript=transcript,
            frame_descriptions=[], frames=[],
        )

    print(f"\n{'=' * 44}")
    print(f"  DONE — {len(slides)} slides extracted")
    print(f"  {sl_dir}/")
    for i, s in enumerate(slides, 1):
        print(f"    {i:2d}. {s['timestamp']:.1f}s — {s.get('reason', '')}")
    print(f"{'=' * 44}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="video-analyzer",
        description="Turn instructional videos into queryable knowledge sessions.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # extract
    p_extract = sub.add_parser("extract", help="Process a video (run once)")
    p_extract.add_argument("url", help="YouTube URL")
    p_extract.add_argument("--threshold", type=float, default=SCENE_THRESHOLD,
                           help=f"Scene change sensitivity 0–1 (default {SCENE_THRESHOLD})")
    p_extract.add_argument("--interval", type=float, default=MIN_FRAME_INTERVAL,
                           help=f"Min seconds between frames (default {MIN_FRAME_INTERVAL})")
    p_extract.add_argument("--max-frames", type=int, default=FRAME_SELECTION_MAX,
                           help=f"Max frames from transcript analysis (default {FRAME_SELECTION_MAX})")
    p_extract.add_argument("--transcript-only", action="store_true",
                           help="Fetch transcript only — no video download or frame analysis")
    p_extract.add_argument("--no-transcript-select", action="store_true",
                           help="Skip transcript analysis — use legacy scene detection")
    p_extract.add_argument("--force", action="store_true",
                           help="Re-extract even if session already exists")
    p_extract.add_argument("--resume", action="store_true",
                           help="Resume from last step — skip completed steps")
    p_extract.add_argument("--focus", type=str, default="",
                           help="Focus on specific content (e.g. 'architecture diagrams')")
    p_extract.add_argument("--time-range", type=str, default="",
                           help="Restrict to time range: START-END in seconds or MM:SS (e.g. '5:00-15:00')")
    p_extract.add_argument("--timestamps", type=str, default="",
                           help="Extract at exact timestamps, bypass AI selection (e.g. '5:30,10:00')")

    # ask
    p_ask = sub.add_parser("ask", help="Ask a question about a processed video")
    p_ask.add_argument("session_id", help="Video ID (from 'sessions' command)")
    p_ask.add_argument("question", help="Your question")
    p_ask.add_argument("--context", action="append", metavar="SOURCE",
                       help="File path, directory, URL, or raw text. Repeatable.")
    p_ask.add_argument("--stdin", action="store_true",
                       help="Read additional context from stdin")

    # slides
    p_slides = sub.add_parser("slides", help="Extract presentation slides from a video")
    p_slides.add_argument("url", help="YouTube URL")
    p_slides.add_argument("--max-slides", type=int, default=SLIDE_SELECTION_MAX,
                          help=f"Max slides to extract (default {SLIDE_SELECTION_MAX})")
    p_slides.add_argument("--force", action="store_true",
                          help="Re-extract even if slides already exist")
    p_slides.add_argument("--focus", type=str, default="",
                          help="Focus on specific content (e.g. 'only code examples')")
    p_slides.add_argument("--time-range", type=str, default="",
                          help="Restrict to time range: START-END in seconds or MM:SS (e.g. '5:00-15:00')")
    p_slides.add_argument("--timestamps", type=str, default="",
                          help="Extract at exact timestamps, bypass AI selection (e.g. '5:30,10:00')")

    # sessions
    sub.add_parser("sessions", help="List all processed videos")

    args = parser.parse_args()

    if args.command == "extract":
        cmd_extract(args)
    elif args.command == "ask":
        cmd_ask(args)
    elif args.command == "slides":
        cmd_slides(args)
    elif args.command == "sessions":
        cmd_sessions(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
