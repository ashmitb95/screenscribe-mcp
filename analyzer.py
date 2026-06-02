"""
Analyzer — Pass 1 only.

Takes extracted frames + transcript and generates visual descriptions
using Claude Vision. Results are stored in the session and queried
later via `ask`.
"""

import base64
import time

import anthropic

from config import FRAME_DESCRIPTION_MAX_TOKENS_PER_FRAME

MAX_RETRIES = 5
INITIAL_BACKOFF = 2.0  # seconds

# Transient errors worth retrying
_RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


def get_transcript_context(transcript: list[dict], timestamp: float, window: float) -> str:
    """Return transcript text for segments within `window` seconds of `timestamp`."""
    segments = [
        seg for seg in transcript
        if seg["start"] >= timestamp - window and seg["start"] <= timestamp + window
    ]
    return " ".join(seg["text"] for seg in segments).strip()


def encode_image(path: str) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def _call_with_retry(client, **kwargs) -> anthropic.types.Message:
    """Call client.messages.create with exponential backoff on transient errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.messages.create(**kwargs)
        except _RETRYABLE as e:
            if attempt == MAX_RETRIES:
                raise
            delay = INITIAL_BACKOFF * (2 ** (attempt - 1))
            print(f"    ⏳ {type(e).__name__} — retrying in {delay:.0f}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(delay)


def describe_frames(
    frames: list[dict],
    transcript: list[dict],
    model: str,
    transcript_window: float,
    batch_size: int,
    progress_file=None,
    existing_descriptions: list[str] | None = None,
) -> list[str]:
    """
    For each batch of frames, ask Claude to describe what's on screen
    and what concept the instructor is explaining.
    Returns a list of description strings (one per batch).

    If `existing_descriptions` is provided, skips that many batches (resume).
    If `progress_file` is a Path, appends each new description as a JSON line.
    """
    import json

    client = anthropic.Anthropic()
    descriptions = list(existing_descriptions or [])
    skip_batches = len(descriptions)

    total_batches = (len(frames) + batch_size - 1) // batch_size

    for batch_idx, batch_start in enumerate(range(0, len(frames), batch_size)):
        if batch_idx < skip_batches:
            continue

        batch = frames[batch_start : batch_start + batch_size]
        batch_end = min(batch_start + batch_size, len(frames))
        print(f"  Describing frames {batch_start + 1}–{batch_end} of {len(frames)} (batch {batch_idx + 1}/{total_batches})...")

        content = []

        for frame in batch:
            ts = frame["timestamp"]
            ctx = get_transcript_context(transcript, ts, transcript_window)

            reason = frame.get("reason", "")
            reason_line = f"Selected because: \"{reason}\"\n" if reason else ""
            content.append({
                "type": "text",
                "text": (
                    f"\n--- Frame at {ts:.1f}s ---\n"
                    f"{reason_line}"
                    f"Transcript around this moment: \"{ctx}\"\n"
                    f"Screen at {ts:.1f}s:"
                ),
            })
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": encode_image(frame["path"]),
                },
            })

        content.append({
            "type": "text",
            "text": (
                "For each frame above, describe concisely:\n"
                "1. What is visible on screen — chart type, timeframe, price levels, "
                "highlighted zones, drawn lines, annotations, candle patterns\n"
                "2. What concept the instructor is demonstrating based on the transcript\n"
                "3. Any specific entry/exit conditions, confirmations, or invalidations shown\n\n"
                "Be precise — mention exact visual elements like 'red supply zone', "
                "'wick below support closing above', 'break and retest', etc."
            ),
        })

        response = _call_with_retry(
            client,
            model=model,
            # Scale with batch size so per-frame descriptions aren't truncated.
            max_tokens=FRAME_DESCRIPTION_MAX_TOKENS_PER_FRAME * len(batch),
            system=(
                "You are analyzing an instructional screen-recording video. "
                "Charts and annotations only — no webcam. "
                "Extract precise, actionable information from each frame."
            ),
            messages=[{"role": "user", "content": content}],
        )

        text = response.content[0].text
        descriptions.append(text)

        # Save progress after each batch
        if progress_file:
            with open(progress_file, "a") as f:
                f.write(json.dumps(text) + "\n")

    return descriptions
