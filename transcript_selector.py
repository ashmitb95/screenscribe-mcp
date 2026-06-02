"""
Transcript-driven frame selection.

Analyzes a video transcript with Claude (text-only, cheap) to identify
timestamps where the instructor is showing something visually important.
Returns targeted timestamps instead of blind time-based sampling.
"""

import json
import re
import time

import anthropic

MAX_RETRIES = 5
INITIAL_BACKOFF = 2.0


def _parse_timestamp(s: str) -> float:
    """Parse a timestamp string like '5:30', '330', or '1:05:30' into seconds."""
    s = s.strip()
    parts = s.split(":")
    if len(parts) == 1:
        return float(parts[0])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"Invalid timestamp format: {s}")


def _parse_time_range(time_range: str) -> tuple[float, float] | None:
    """Parse 'START-END' string into (start_seconds, end_seconds). Returns None if empty."""
    if not time_range or not time_range.strip():
        return None
    m = re.match(r"^(.+?)\s*-\s*(\d.*)$", time_range.strip())
    if not m:
        raise ValueError(f"Invalid time range format: {time_range}. Expected 'START-END' (e.g. '5:00-15:00' or '300-900').")
    start = _parse_timestamp(m.group(1))
    end = _parse_timestamp(m.group(2))
    if end <= start:
        raise ValueError(f"Time range end ({end}s) must be after start ({start}s).")
    return (start, end)


def _parse_timestamps_list(timestamps_str: str) -> list[dict]:
    """Parse comma-separated timestamps into a selections list."""
    if not timestamps_str or not timestamps_str.strip():
        return []
    parts = [p.strip() for p in timestamps_str.split(",") if p.strip()]
    return [
        {"timestamp": _parse_timestamp(p), "reason": "user-specified timestamp"}
        for p in parts
    ]

_RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


def _format_transcript(transcript: list[dict]) -> str:
    """Format transcript segments as [MM:SS] text for the prompt."""
    lines = []
    for seg in transcript:
        t = seg["start"]
        mm, ss = int(t // 60), int(t % 60)
        lines.append(f"[{mm}:{ss:02d}] {seg['text']}")
    return "\n".join(lines)


def _parse_json_response(text: str) -> list[dict]:
    """Extract JSON array from Claude's response, handling markdown fences."""
    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = cleaned.strip()

    # Find the JSON array in the response
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON array found in response: {text[:200]}")

    return json.loads(match.group(0))


def _format_chapters(chapters: list[dict]) -> str:
    """Format YouTube chapters for the prompt."""
    if not chapters:
        return ""
    lines = []
    for ch in chapters:
        t = ch.get("start_time", 0)
        mm, ss = int(t // 60), int(t % 60)
        end = ch.get("end_time", 0)
        emm, ess = int(end // 60), int(end % 60)
        lines.append(f"  [{mm}:{ss:02d} - {emm}:{ess:02d}] {ch.get('title', '')}")
    return "\n".join(lines)


def _call_claude_with_retry(client, model, system_prompt, user_prompt):
    """Call Claude with exponential backoff retry on transient errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response
        except _RETRYABLE as e:
            if attempt == MAX_RETRIES:
                raise
            delay = INITIAL_BACKOFF * (2 ** (attempt - 1))
            print(f"    \u23f3 {type(e).__name__} — retrying in {delay:.0f}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(delay)


def _validate_and_filter(raw_selections, video_duration, max_items, min_interval, time_range=None):
    """
    Turn Claude's raw picks into the final selection.

    raw_selections arrives in IMPORTANCE order (Claude is told to return the
    most critical moments first). We greedily accept picks in that order,
    skipping any that fall within min_interval of an already-accepted pick —
    which both enforces spacing and collapses exact/near-duplicate timestamps —
    and stop once we have max_items. The kept picks are then sorted by time for
    chronological output. This keeps the MOST IMPORTANT moments when capping,
    rather than just the earliest-in-time ones.
    """
    range_start = time_range[0] if time_range else 0
    range_end = time_range[1] if time_range else video_duration

    accepted = []
    for item in raw_selections:
        raw_ts = item.get("timestamp")
        if raw_ts is None:
            continue  # skip picks with no timestamp
        try:
            ts = float(raw_ts)
        except (TypeError, ValueError):
            continue  # skip malformed picks rather than crashing the run
        reason = str(item.get("reason", ""))
        if not (range_start <= ts <= range_end):
            continue
        if any(abs(ts - a["timestamp"]) < min_interval for a in accepted):
            continue  # too close to a higher-priority pick (or an exact duplicate)
        accepted.append({"timestamp": ts, "reason": reason})
        if len(accepted) >= max_items:
            break

    accepted.sort(key=lambda x: x["timestamp"])
    return accepted


def select_frames_from_transcript(
    transcript: list[dict],
    model: str,
    max_frames: int = 25,
    min_interval: float = 5.0,
    chapters: list[dict] | None = None,
    focus: str = "",
    time_range: str = "",
    timestamps: str = "",
) -> list[dict]:
    """
    Analyze transcript with Claude to identify timestamps where a screenshot
    would be valuable for understanding the visual content.

    Optional:
      focus: natural language instruction to narrow selection
      time_range: 'START-END' (seconds or MM:SS) to restrict to a portion
      timestamps: comma-separated exact timestamps, bypasses Claude selection

    Returns: [{"timestamp": float, "reason": str}, ...]
    """
    if not transcript:
        return []

    video_duration = transcript[-1]["start"] + transcript[-1].get("duration", 0)
    parsed_range = _parse_time_range(time_range) if time_range else None

    # Direct timestamps bypass Claude selection entirely
    if timestamps:
        user_selections = _parse_timestamps_list(timestamps)
        return _validate_and_filter(
            user_selections, video_duration, max_frames, min_interval,
            time_range=parsed_range,
        )

    # Filter transcript to time range if specified
    active_transcript = transcript
    if parsed_range:
        range_start, range_end = parsed_range
        active_transcript = [
            seg for seg in transcript
            if seg["start"] >= range_start and seg["start"] <= range_end
        ]
        if not active_transcript:
            return []

    formatted = _format_transcript(active_transcript)

    client = anthropic.Anthropic()

    system_prompt = (
        "You are analyzing the transcript of an instructional screen-recording video "
        "to identify moments where a screenshot would help understand the visual content. "
        "The video shows charts, diagrams, and annotations — no webcam."
    )

    chapters_section = ""
    if chapters:
        chapters_section = (
            f"\n\nThe video has the following chapters:\n"
            f"{_format_chapters(chapters)}\n"
            f"IMPORTANT: Always capture a frame near the END of each chapter — "
            f"this is when the instructor's final annotation, completed zone, or "
            f"summary for that section is fully visible on screen.\n"
        )

    focus_section = ""
    if focus:
        focus_section = (
            f"\nUSER FOCUS INSTRUCTION: The user has asked to focus specifically on: "
            f'"{focus}". Prioritize moments related to this topic above all else. '
            f"If fewer than {max_frames} moments match this focus, that's fine — "
            f"only return moments that are genuinely relevant.\n"
        )

    time_range_section = ""
    if parsed_range:
        rs_mm, rs_ss = int(parsed_range[0] // 60), int(parsed_range[0] % 60)
        re_mm, re_ss = int(parsed_range[1] // 60), int(parsed_range[1] % 60)
        time_range_section = (
            f"\nRESTRICTED TIME RANGE: Only select moments between "
            f"{rs_mm}:{rs_ss:02d} and {re_mm}:{re_ss:02d}. "
            f"Ignore all content outside this window.\n"
        )

    user_prompt = (
        f"Here is the transcript of a {int(video_duration)}-second instructional video. "
        f"Identify up to {max_frames} timestamps where the instructor is showing something "
        f"visually important that would require a screenshot to understand.\n\n"
        f"Focus on moments where:\n"
        f"- A new chart, diagram, or example appears on screen\n"
        f"- The instructor points to, circles, or highlights specific visual elements\n"
        f"- Key patterns, zones, levels, or formations are being described\n"
        f"- Step-by-step visual walkthroughs transition to a new step\n"
        f"- Before/after comparisons are shown\n"
        f"- The instructor uses words like 'here', 'this', 'look', 'see', 'notice' "
        f"while describing something on screen\n"
        f"- **A topic or section is wrapping up** — the completed chart/diagram/zone is "
        f"fully drawn and the instructor is summarizing or transitioning to the next topic. "
        f"These end-of-section frames capture the final, complete visual.\n"
        f"{chapters_section}"
        f"{focus_section}"
        f"{time_range_section}\n"
        f"Return ONLY a JSON array, ordered by importance (most critical first):\n"
        f'[{{"timestamp": <seconds>, "reason": "<brief description>"}}]\n\n'
        f"TRANSCRIPT:\n{formatted}"
    )

    response = _call_claude_with_retry(client, model, system_prompt, user_prompt)
    raw_selections = _parse_json_response(response.content[0].text)
    return _validate_and_filter(
        raw_selections, video_duration, max_frames, min_interval,
        time_range=parsed_range,
    )


def select_slides_from_transcript(
    transcript: list[dict],
    model: str,
    max_slides: int = 15,
    min_interval: float = 10.0,
    chapters: list[dict] | None = None,
    focus: str = "",
    time_range: str = "",
    timestamps: str = "",
) -> list[dict]:
    """
    Analyze transcript with Claude to identify timestamps where the screen
    shows a complete visual that would work as a standalone presentation slide.

    Optional:
      focus: natural language instruction to narrow selection
      time_range: 'START-END' (seconds or MM:SS) to restrict to a portion
      timestamps: comma-separated exact timestamps, bypasses Claude selection

    Returns: [{"timestamp": float, "reason": str}, ...]
    """
    if not transcript:
        return []

    video_duration = transcript[-1]["start"] + transcript[-1].get("duration", 0)
    parsed_range = _parse_time_range(time_range) if time_range else None

    # Direct timestamps bypass Claude selection entirely
    if timestamps:
        user_selections = _parse_timestamps_list(timestamps)
        return _validate_and_filter(
            user_selections, video_duration, max_slides, min_interval,
            time_range=parsed_range,
        )

    # Filter transcript to time range if specified
    active_transcript = transcript
    if parsed_range:
        range_start, range_end = parsed_range
        active_transcript = [
            seg for seg in transcript
            if seg["start"] >= range_start and seg["start"] <= range_end
        ]
        if not active_transcript:
            return []

    formatted = _format_transcript(active_transcript)

    client = anthropic.Anthropic()

    system_prompt = (
        "You are analyzing the transcript of an instructional video to identify "
        "moments where the screen shows a COMPLETE visual that would work as a "
        "standalone slide in a presentation deck. The video shows charts, diagrams, "
        "code, and annotations."
    )

    chapters_section = ""
    if chapters:
        chapters_section = (
            f"\n\nThe video has the following chapters:\n"
            f"{_format_chapters(chapters)}\n"
            f"IMPORTANT: Capture a frame near the END of each chapter — this is "
            f"when the completed visual for that section is fully visible.\n"
        )

    focus_section = ""
    if focus:
        focus_section = (
            f"\nUSER FOCUS INSTRUCTION: The user has asked to focus specifically on: "
            f'"{focus}". Prioritize moments related to this topic above all else. '
            f"If fewer than {max_slides} moments match this focus, that's fine — "
            f"only return moments that are genuinely relevant.\n"
        )

    time_range_section = ""
    if parsed_range:
        rs_mm, rs_ss = int(parsed_range[0] // 60), int(parsed_range[0] % 60)
        re_mm, re_ss = int(parsed_range[1] // 60), int(parsed_range[1] % 60)
        time_range_section = (
            f"\nRESTRICTED TIME RANGE: Only select moments between "
            f"{rs_mm}:{rs_ss:02d} and {re_mm}:{re_ss:02d}. "
            f"Ignore all content outside this window.\n"
        )

    user_prompt = (
        f"Here is the transcript of a {int(video_duration)}-second instructional video. "
        f"Identify up to {max_slides} timestamps where the screen shows a COMPLETE "
        f"visual that would make a good standalone presentation slide.\n\n"
        f"Select moments where:\n"
        f"- A diagram, chart, or illustration is FULLY DRAWN and COMPLETE (not "
        f"mid-animation or mid-drawing). Prefer the moment just AFTER the instructor "
        f"finishes building a visual, not while they are still adding to it.\n"
        f"- Text, labels, or titles are clearly visible and would be readable as a slide.\n"
        f"- A key concept, definition, formula, or summary is displayed on screen.\n"
        f"- A code snippet or configuration is fully shown (not partially scrolled).\n"
        f"- A comparison table, list of steps, or structured information is complete.\n"
        f"- A section title or topic header is shown (good for slide deck dividers).\n\n"
        f"AVOID selecting moments where:\n"
        f"- The instructor is mid-drawing or mid-typing (visual is incomplete).\n"
        f"- The screen is transitioning between views.\n"
        f"- The content is a near-duplicate of an already-selected slide.\n"
        f"- The visual is too zoomed-in to be self-explanatory without narration.\n\n"
        f"For each selection, the 'reason' should describe what the slide would "
        f"communicate as a standalone visual (e.g. 'Complete architecture diagram "
        f"showing 3-tier system' not 'instructor is drawing a diagram').\n\n"
        f"Prioritize DIVERSITY of content — a good slide deck covers all major topics.\n"
        f"{chapters_section}"
        f"{focus_section}"
        f"{time_range_section}\n"
        f"Return ONLY a JSON array, ordered by importance (most critical first):\n"
        f'[{{"timestamp": <seconds>, "reason": "<brief description>"}}]\n\n'
        f"TRANSCRIPT:\n{formatted}"
    )

    response = _call_claude_with_retry(client, model, system_prompt, user_prompt)
    raw_selections = _parse_json_response(response.content[0].text)
    return _validate_and_filter(
        raw_selections, video_duration, max_slides, min_interval,
        time_range=parsed_range,
    )
