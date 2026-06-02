"""
Gemini-driven frame selection.

The transcript-based selector (transcript_selector.py) guesses key moments from
the words alone — blind to the pixels. This selector instead hands the YouTube
URL to Gemini, which actually watches the video (frames + audio) and returns the
timestamps where something visually important is on screen. ffmpeg then extracts
those exact frames.

Returns the same shape as transcript_selector — [{"timestamp": float, "reason":
str}, ...] — and reuses _validate_and_filter, so it's a drop-in replacement for
the selection step. Callers fall back to the transcript selector when no
GEMINI_API_KEY is set (see gemini_available()).
"""

import json
import os
import time

from transcript_selector import (
    _parse_time_range,
    _parse_timestamp,
    _validate_and_filter,
    select_frames_from_transcript,
    select_slides_from_transcript,
)

MAX_RETRIES = 3
INITIAL_BACKOFF = 2.0


def gemini_available() -> bool:
    """True if a Gemini key is configured and the SDK is importable."""
    if not os.environ.get("GEMINI_API_KEY"):
        return False
    try:
        import google.genai  # noqa: F401
        return True
    except ImportError:
        return False


def _frame_schema():
    from google.genai import types
    return types.Schema(
        type=types.Type.ARRAY,
        items=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "timestamp": types.Schema(
                    type=types.Type.STRING,
                    description="Timestamp in the video as MM:SS or H:MM:SS",
                ),
                "reason": types.Schema(
                    type=types.Type.STRING,
                    description="What is on screen at this moment and why it matters",
                ),
            },
            required=["timestamp", "reason"],
        ),
    )


def _build_prompt(purpose: str, max_items: int, focus: str) -> str:
    if purpose == "slides":
        body = (
            f"Watch this video and identify up to {max_items} moments that would make "
            f"strong standalone images — a complete, self-contained visual someone could "
            f"understand on its own.\n\n"
            f"Select a moment ONLY when the visual is COMPLETE and clear — a finished "
            f"diagram, chart, scene, result, or readable on-screen text/code/table (not "
            f"mid-transition, mid-draw, or mid-scroll). Prefer the instant when the visual "
            f"is most complete and readable. Avoid near-duplicates of the same shot. "
            f"Prioritise diversity across the video's major moments and topics.\n"
        )
    else:
        body = (
            f"Watch this video and identify up to {max_items} moments where a screenshot "
            f"best captures something visually important.\n\n"
            f"Choose moments where a key action, object, scene, demonstration, result, or "
            f"on-screen text/diagram/chart is clearly visible — especially right after "
            f"something important appears or is pointed out, or when a visual is complete. "
            f"Give the timestamp where the relevant thing is most fully and clearly "
            f"visible, not merely when it is first mentioned.\n"
        )
    if focus:
        body += (
            f'\nFOCUS: Prioritise moments related to "{focus}" above all else. '
            f"Only return moments genuinely relevant to it.\n"
        )
    body += (
        "\nReturn a JSON array ordered by importance (most critical first). Each item: "
        '{"timestamp": "MM:SS", "reason": "<what is on screen and why it matters>"}.'
    )
    return body


def _call_gemini(youtube_url, model, prompt, parsed_range, media_resolution_low, response_schema=None):
    """Run one Gemini call over a YouTube URL and return the JSON text. Shared by
    frame selection and whole-video analysis (each passes its own response_schema)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    video_metadata = None
    if parsed_range:
        video_metadata = types.VideoMetadata(
            start_offset=f"{int(parsed_range[0])}s",
            end_offset=f"{int(parsed_range[1])}s",
        )

    part = types.Part(
        file_data=types.FileData(file_uri=youtube_url),
        video_metadata=video_metadata,
    )
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=response_schema or _frame_schema(),
        media_resolution=(
            types.MediaResolution.MEDIA_RESOLUTION_LOW
            if media_resolution_low
            else types.MediaResolution.MEDIA_RESOLUTION_MEDIUM
        ),
    )

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=types.Content(parts=[part, types.Part(text=prompt)]),
                config=config,
            )
            return resp.text
        except Exception as e:  # noqa: BLE001 — surface after retries; caller falls back
            last_err = e
            if attempt == MAX_RETRIES:
                raise
            time.sleep(INITIAL_BACKOFF * (2 ** (attempt - 1)))
    raise last_err


def parse_gemini_selections(raw_text: str) -> list[dict]:
    """
    Turn Gemini's JSON response into raw [{"timestamp": float, "reason": str}].
    Timestamps may be "MM:SS", "H:MM:SS", or plain seconds; all are normalised to
    float seconds. Malformed items are skipped. Importance order is preserved.
    Separated from the network call so it can be unit-tested without the SDK.
    """
    data = json.loads(raw_text)
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict) or "timestamp" not in item:
            continue
        try:
            ts = _parse_timestamp(str(item["timestamp"]))
        except (ValueError, TypeError):
            continue
        out.append({"timestamp": ts, "reason": str(item.get("reason", ""))})
    return out


def select_with_gemini(
    youtube_url: str,
    model: str,
    purpose: str = "frames",
    max_items: int = 25,
    min_interval: float = 5.0,
    video_duration: float = 0.0,
    focus: str = "",
    time_range: str = "",
    media_resolution_low: bool = True,
) -> list[dict]:
    """
    Ask Gemini to watch the YouTube video and return key timestamps.

    purpose: "frames" (anything visually important) or "slides" (complete,
             standalone visuals). Returns [{"timestamp": float, "reason": str}],
             validated/deduped/capped by _validate_and_filter.
    """
    parsed_range = _parse_time_range(time_range) if time_range else None
    prompt = _build_prompt(purpose, max_items, focus)
    raw_text = _call_gemini(youtube_url, model, prompt, parsed_range, media_resolution_low)
    raw = parse_gemini_selections(raw_text)

    # Without a known duration the range filter in _validate_and_filter would drop
    # everything; derive a generous upper bound from the picks themselves.
    duration = video_duration
    if duration <= 0 and raw:
        duration = max(s["timestamp"] for s in raw) + min_interval

    return _validate_and_filter(raw, duration, max_items, min_interval, time_range=parsed_range)


def select_frames(
    youtube_url,
    transcript,
    *,
    transcript_model,
    gemini_model,
    max_frames,
    min_interval,
    chapters,
    focus="",
    time_range="",
    timestamps="",
    video_duration=0.0,
    media_resolution_low=True,
) -> list[dict]:
    """
    Pick frames to capture. Uses Gemini (watches the video) when a key is set;
    falls back to transcript-based picking otherwise, on error, or when the user
    passed explicit timestamps (which bypass AI selection entirely).
    """
    if gemini_available() and not timestamps:
        try:
            sels = select_with_gemini(
                youtube_url, gemini_model, "frames", max_frames, min_interval,
                video_duration, focus, time_range, media_resolution_low,
            )
            if sels:
                print(f"  [selection] Gemini watched the video → {len(sels)} moments")
                return sels
            print("  [selection] Gemini returned nothing — falling back to transcript")
        except Exception as e:  # noqa: BLE001
            print(f"  [selection] Gemini failed ({type(e).__name__}: {str(e)[:100]}) — falling back to transcript")

    return select_frames_from_transcript(
        transcript=transcript, model=transcript_model, max_frames=max_frames,
        min_interval=min_interval, chapters=chapters, focus=focus,
        time_range=time_range, timestamps=timestamps,
    )


def select_slides(
    youtube_url,
    transcript,
    *,
    transcript_model,
    gemini_model,
    max_slides,
    min_interval,
    chapters,
    focus="",
    time_range="",
    timestamps="",
    video_duration=0.0,
    media_resolution_low=True,
) -> list[dict]:
    """Slide variant of select_frames (complete, standalone visuals)."""
    if gemini_available() and not timestamps:
        try:
            sels = select_with_gemini(
                youtube_url, gemini_model, "slides", max_slides, min_interval,
                video_duration, focus, time_range, media_resolution_low,
            )
            if sels:
                print(f"  [selection] Gemini watched the video → {len(sels)} slides")
                return sels
            print("  [selection] Gemini returned nothing — falling back to transcript")
        except Exception as e:  # noqa: BLE001
            print(f"  [selection] Gemini failed ({type(e).__name__}: {str(e)[:100]}) — falling back to transcript")

    return select_slides_from_transcript(
        transcript=transcript, model=transcript_model, max_slides=max_slides,
        min_interval=min_interval, chapters=chapters, focus=focus,
        time_range=time_range, timestamps=timestamps,
    )
