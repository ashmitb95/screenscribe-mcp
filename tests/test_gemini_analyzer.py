"""
Tests for gemini_analyzer. The network/SDK call (_call_gemini) is monkeypatched,
so these run with no GEMINI_API_KEY and no API calls.
"""

import json

from screenscribe import gemini_analyzer
from screenscribe.gemini_analyzer import analyze_video_with_gemini, parse_gemini_analysis


def test_parse_normalizes_timestamps_to_seconds():
    raw = json.dumps({
        "summary": "A talk about X.",
        "sections": [{"start": "0:00", "title": "Intro", "summary": "hello"},
                     {"start": "5:30", "title": "Body", "summary": "core"}],
        "key_moments": [{"timestamp": "1:05", "description": "a chart appears"}],
        "on_screen_text": ["39.46%", "BUY"],
    })
    out = parse_gemini_analysis(raw)
    assert out["summary"] == "A talk about X."
    assert out["sections"][1]["seconds"] == 330.0
    assert out["sections"][1]["title"] == "Body"
    assert out["key_moments"][0]["seconds"] == 65.0
    assert out["on_screen_text"] == ["39.46%", "BUY"]


def test_parse_non_dict_returns_empty():
    assert parse_gemini_analysis(json.dumps(["not", "a", "dict"])) == {}


def test_parse_skips_malformed_entries():
    raw = json.dumps({
        "summary": "s",
        "sections": ["bad", {"start": "0:10", "title": "ok", "summary": "fine"}],
        "key_moments": [{"timestamp": "bad-ts", "description": "kept, seconds=None"}],
    })
    out = parse_gemini_analysis(raw)
    assert len(out["sections"]) == 1 and out["sections"][0]["title"] == "ok"
    assert out["key_moments"][0]["seconds"] is None  # unparseable timestamp -> None, not a crash


def test_analyze_video_uses_call_and_parses(monkeypatch):
    canned = json.dumps({
        "summary": "ok", "sections": [{"start": "0:00", "title": "t", "summary": "s"}],
        "key_moments": [{"timestamp": "2:00", "description": "d"}],
    })
    monkeypatch.setattr(gemini_analyzer, "_call_gemini", lambda *a, **k: canned)
    out = analyze_video_with_gemini("https://youtu.be/x", "gemini-3.5-flash")
    assert out["summary"] == "ok"
    assert out["key_moments"][0]["seconds"] == 120.0
