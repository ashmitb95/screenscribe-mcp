"""
Tests for transcript_selector._validate_and_filter — the function that turns
Claude's raw timestamp picks into the final frame selection.

These lock in two behaviors that were previously broken:
  1. Duplicate / near-duplicate timestamps must be collapsed.
  2. When there are more candidates than max_items, the most IMPORTANT picks
     (Claude returns them most-critical-first) must be kept — not just the
     earliest-in-time ones.
"""

from screenscribe.transcript_selector import _validate_and_filter


def _ts(selections):
    return [s["timestamp"] for s in selections]


def test_collapses_exact_duplicate_timestamps():
    # Claude returned the same chapter-boundary second four times (real failure
    # mode observed on video GqO9C819SgI).
    raw = [
        {"timestamp": 1020.0, "reason": "a"},
        {"timestamp": 1020.0, "reason": "b"},
        {"timestamp": 1020.0, "reason": "c"},
        {"timestamp": 1020.0, "reason": "d"},
        {"timestamp": 600.0, "reason": "e"},
    ]
    out = _validate_and_filter(raw, video_duration=1485, max_items=25, min_interval=5.0)
    ts = _ts(out)
    assert len(ts) == len(set(ts)), f"duplicate timestamps survived: {ts}"
    assert ts.count(1020.0) == 1
    assert set(ts) == {600.0, 1020.0}


def test_collapses_near_duplicate_within_min_interval():
    raw = [
        {"timestamp": 100.0, "reason": "a"},
        {"timestamp": 102.0, "reason": "b"},  # 2s later, inside 5s min_interval
        {"timestamp": 200.0, "reason": "c"},
    ]
    out = _validate_and_filter(raw, video_duration=300, max_items=25, min_interval=5.0)
    ts = _ts(out)
    assert ts == [100.0, 200.0], ts


def test_caps_by_importance_not_by_time():
    # Most-critical-first ordering: the two most important moments are LATE in
    # the video. With max_items=2 they must both survive.
    raw = [
        {"timestamp": 1400.0, "reason": "most important"},
        {"timestamp": 1300.0, "reason": "second most important"},
        {"timestamp": 50.0, "reason": "least important, but earliest"},
        {"timestamp": 60.0, "reason": "least important"},
    ]
    out = _validate_and_filter(raw, video_duration=1485, max_items=2, min_interval=5.0)
    ts = set(_ts(out))
    assert ts == {1300.0, 1400.0}, f"expected the two most-important late picks, got {ts}"


def test_higher_priority_wins_when_two_are_too_close():
    # First in the list is higher priority; the close one (within min_interval)
    # should be dropped, keeping the higher-priority pick.
    raw = [
        {"timestamp": 500.0, "reason": "higher priority"},
        {"timestamp": 503.0, "reason": "lower priority, too close"},
    ]
    out = _validate_and_filter(raw, video_duration=1000, max_items=25, min_interval=5.0)
    assert _ts(out) == [500.0], _ts(out)


def test_respects_time_range():
    raw = [
        {"timestamp": 100.0, "reason": "in range"},
        {"timestamp": 900.0, "reason": "out of range"},
    ]
    out = _validate_and_filter(
        raw, video_duration=1000, max_items=25, min_interval=5.0, time_range=(0, 300)
    )
    assert _ts(out) == [100.0], _ts(out)


def test_output_is_sorted_by_timestamp():
    raw = [
        {"timestamp": 300.0, "reason": "a"},
        {"timestamp": 100.0, "reason": "b"},
        {"timestamp": 200.0, "reason": "c"},
    ]
    out = _validate_and_filter(raw, video_duration=1000, max_items=25, min_interval=5.0)
    assert _ts(out) == [100.0, 200.0, 300.0], _ts(out)


def test_ignores_malformed_items():
    raw = [
        {"timestamp": "not a number", "reason": "bad"},
        {"reason": "missing timestamp"},
        {"timestamp": 100.0, "reason": "good"},
    ]
    out = _validate_and_filter(raw, video_duration=1000, max_items=25, min_interval=5.0)
    assert _ts(out) == [100.0], _ts(out)
