"""
Whole-video analysis with Gemini.

Gemini already watches the entire video when selecting frames; this asks it for
a structured understanding of the whole video instead — a summary, a section/
topic breakdown, the key visual moments (with what is on screen), and notable
on-screen text/data. One cheap Gemini call, no download or ffmpeg, and it gives
whole-video visual coverage without the per-frame Claude Vision pass.

Saved as <session>/gemini_analysis.json and queryable via get_video_analysis.
"""

import json

from screenscribe.gemini_selector import _call_gemini, gemini_available  # noqa: F401 (re-exported for callers)
from screenscribe.transcript_selector import _parse_time_range, _parse_timestamp


def _analysis_schema():
    from google.genai import types
    S, T = types.Schema, types.Type
    return S(
        type=T.OBJECT,
        properties={
            "summary": S(type=T.STRING, description="A few sentences summarising the whole video"),
            "sections": S(
                type=T.ARRAY,
                items=S(type=T.OBJECT, properties={
                    "start": S(type=T.STRING, description="Section start as MM:SS"),
                    "title": S(type=T.STRING),
                    "summary": S(type=T.STRING, description="What this section covers"),
                }, required=["start", "title", "summary"]),
            ),
            "key_moments": S(
                type=T.ARRAY,
                items=S(type=T.OBJECT, properties={
                    "timestamp": S(type=T.STRING, description="MM:SS"),
                    "description": S(type=T.STRING, description="What is on screen / happening and why it matters"),
                }, required=["timestamp", "description"]),
            ),
            "on_screen_text": S(
                type=T.ARRAY,
                items=S(type=T.STRING),
                description="Notable text, figures, numbers, labels, or data shown on screen",
            ),
        },
        required=["summary", "sections", "key_moments"],
    )


def _build_analysis_prompt(focus: str) -> str:
    body = (
        "Watch this entire video and produce a structured analysis of it, using what "
        "is actually shown on screen as well as the narration.\n\n"
        "Provide:\n"
        "- summary: a few sentences capturing what the video is about and its key takeaways.\n"
        "- sections: the video broken into its main parts, each with a start time (MM:SS), "
        "a short title, and a one-line summary.\n"
        "- key_moments: the most important moments to see, each with a timestamp (MM:SS) and "
        "a description of what is on screen and why it matters.\n"
        "- on_screen_text: notable text, figures, numbers, labels, or data visible on screen.\n"
    )
    if focus:
        body += f'\nFOCUS: Pay special attention to "{focus}" throughout.\n'
    return body


def _seconds(label):
    try:
        return _parse_timestamp(str(label))
    except (ValueError, TypeError):
        return None


def parse_gemini_analysis(raw_text: str) -> dict:
    """
    Parse + normalise Gemini's analysis JSON, adding a numeric `seconds` next to
    each MM:SS label. Separated from the network call so it can be unit-tested
    without the SDK.
    """
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        return {}
    out = {
        "summary": str(data.get("summary", "")),
        "sections": [],
        "key_moments": [],
        "on_screen_text": [
            str(t) for t in (data.get("on_screen_text") or [])
            if isinstance(t, (str, int, float))
        ],
    }
    for s in data.get("sections") or []:
        if not isinstance(s, dict):
            continue
        out["sections"].append({
            "start": str(s.get("start", "")),
            "seconds": _seconds(s.get("start")),
            "title": str(s.get("title", "")),
            "summary": str(s.get("summary", "")),
        })
    for m in data.get("key_moments") or []:
        if not isinstance(m, dict):
            continue
        out["key_moments"].append({
            "timestamp": str(m.get("timestamp", "")),
            "seconds": _seconds(m.get("timestamp")),
            "description": str(m.get("description", "")),
        })
    return out


def analyze_video_with_gemini(youtube_url, model, focus="", time_range="", media_resolution_low=True) -> dict:
    """Watch the whole video with Gemini and return a structured analysis dict."""
    parsed_range = _parse_time_range(time_range) if time_range else None
    raw = _call_gemini(
        youtube_url, model, _build_analysis_prompt(focus), parsed_range,
        media_resolution_low, response_schema=_analysis_schema(),
    )
    return parse_gemini_analysis(raw)
