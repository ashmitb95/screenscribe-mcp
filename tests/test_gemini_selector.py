"""
Tests for gemini_selector. The network/SDK call (_call_gemini) is monkeypatched,
so these run with no GEMINI_API_KEY and no API calls.
"""

import json

from screenscribe import gemini_selector
from screenscribe.gemini_selector import parse_gemini_selections, select_frames, select_with_gemini


def test_parse_handles_mmss_seconds_and_hms():
    raw = json.dumps([
        {"timestamp": "1:30", "reason": "a"},
        {"timestamp": "90", "reason": "b"},
        {"timestamp": "1:00:05", "reason": "c"},
    ])
    out = parse_gemini_selections(raw)
    assert [s["timestamp"] for s in out] == [90.0, 90.0, 3605.0]


def test_parse_skips_malformed_and_preserves_order():
    raw = json.dumps([
        {"timestamp": "0:10", "reason": "first"},
        {"reason": "no timestamp"},
        {"timestamp": "garbage", "reason": "bad"},
        {"timestamp": "0:20", "reason": "second"},
    ])
    out = parse_gemini_selections(raw)
    assert [(s["timestamp"], s["reason"]) for s in out] == [(10.0, "first"), (20.0, "second")]


def test_parse_non_array_returns_empty():
    assert parse_gemini_selections(json.dumps({"oops": 1})) == []


def test_select_with_gemini_normalizes_and_dedups(monkeypatch):
    # Importance-ordered, with a duplicate timestamp and a near one inside min_interval.
    canned = json.dumps([
        {"timestamp": "0:50", "reason": "most important"},
        {"timestamp": "50", "reason": "exact duplicate ts"},
        {"timestamp": "0:52", "reason": "2s later, within interval"},
        {"timestamp": "2:00", "reason": "second"},
    ])
    monkeypatch.setattr(gemini_selector, "_call_gemini", lambda *a, **k: canned)
    out = select_with_gemini("https://youtu.be/x", "gemini-2.5-flash",
                             purpose="frames", max_items=25, min_interval=5.0,
                             video_duration=300)
    ts = [s["timestamp"] for s in out]
    assert ts == [50.0, 120.0]  # dup + near collapsed, sorted by time


def test_select_with_gemini_caps_by_importance(monkeypatch):
    canned = json.dumps([
        {"timestamp": "5:00", "reason": "most important (late)"},
        {"timestamp": "4:00", "reason": "second (late)"},
        {"timestamp": "0:10", "reason": "least important (early)"},
    ])
    monkeypatch.setattr(gemini_selector, "_call_gemini", lambda *a, **k: canned)
    out = select_with_gemini("https://youtu.be/x", "gemini-2.5-flash",
                             purpose="frames", max_items=2, min_interval=5.0,
                             video_duration=400)
    assert {s["timestamp"] for s in out} == {240.0, 300.0}  # kept the two important late ones


def test_dispatcher_uses_gemini_when_available(monkeypatch):
    monkeypatch.setattr(gemini_selector, "gemini_available", lambda: True)
    monkeypatch.setattr(gemini_selector, "select_with_gemini",
                        lambda *a, **k: [{"timestamp": 12.0, "reason": "from gemini"}])
    out = select_frames("https://youtu.be/x", transcript=[],
                        transcript_model="m", gemini_model="g",
                        max_frames=10, min_interval=5.0, chapters=[])
    assert out == [{"timestamp": 12.0, "reason": "from gemini"}]


def test_dispatcher_falls_back_to_transcript_on_error(monkeypatch):
    monkeypatch.setattr(gemini_selector, "gemini_available", lambda: True)
    def boom(*a, **k):
        raise RuntimeError("gemini down")
    monkeypatch.setattr(gemini_selector, "select_with_gemini", boom)
    monkeypatch.setattr(gemini_selector, "select_frames_from_transcript",
                        lambda **k: [{"timestamp": 7.0, "reason": "from transcript"}])
    out = select_frames("https://youtu.be/x", transcript=[],
                        transcript_model="m", gemini_model="g",
                        max_frames=10, min_interval=5.0, chapters=[])
    assert out == [{"timestamp": 7.0, "reason": "from transcript"}]


def test_explicit_timestamps_bypass_gemini(monkeypatch):
    # Even when Gemini is available, explicit user timestamps skip it entirely.
    monkeypatch.setattr(gemini_selector, "gemini_available", lambda: True)
    def fail(*a, **k):
        raise AssertionError("Gemini should not be called when timestamps are given")
    monkeypatch.setattr(gemini_selector, "select_with_gemini", fail)
    out = select_frames("https://youtu.be/x", transcript=[{"start": 0, "duration": 100, "text": "x"}],
                        transcript_model="m", gemini_model="g",
                        max_frames=10, min_interval=5.0, chapters=[],
                        timestamps="10,20")
    assert [s["timestamp"] for s in out] == [10.0, 20.0]
