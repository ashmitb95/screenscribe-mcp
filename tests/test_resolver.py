"""
Tests for the multi-input resolver. yt-dlp flat enumeration is monkeypatched
(via resolver._flat_extract), so these run with no network and no downloads —
mirroring the SDK-free style in test_gemini_selector.py.
"""

import pytest

import screenscribe.resolver as resolver
from screenscribe.resolver import resolve_videos


# ── single video (no network) ───────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://youtu.be/eKjYwhecRvY", "eKjYwhecRvY"),
    ("https://www.youtube.com/watch?v=eKjYwhecRvY", "eKjYwhecRvY"),
    ("https://www.youtube.com/watch?v=eKjYwhecRvY&t=30s", "eKjYwhecRvY"),
    ("https://youtube.com/shorts/eKjYwhecRvY", "eKjYwhecRvY"),
    ("https://www.youtube.com/embed/eKjYwhecRvY", "eKjYwhecRvY"),
])
def test_single_video_url(url, expected):
    out = resolve_videos(url)
    assert out["kind"] == "video"
    assert out["video_ids"] == [expected]
    assert out["total_found"] == 1


# ── list of URLs (no network) ─────────────────────────────────────────────────

def test_list_ordered_and_deduped():
    out = resolve_videos([
        "https://youtu.be/aaaaaaaaaaa",
        "https://www.youtube.com/watch?v=bbbbbbbbbbb",
        "https://youtu.be/aaaaaaaaaaa",  # duplicate
    ])
    assert out["kind"] == "list"
    assert out["video_ids"] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert out["total_found"] == 2


def test_list_element_not_a_video_raises():
    with pytest.raises(ValueError):
        resolve_videos(["https://youtu.be/aaaaaaaaaaa", "not a video url"])


# ── unparseable string (no network) ──────────────────────────────────────────

def test_unparseable_string_raises_without_network(monkeypatch):
    # If this tried to enumerate, the absence of a _flat_extract stub would surface;
    # assert it raises ValueError purely from the parse stage.
    def boom(*a, **k):
        raise AssertionError("must not hit the network for an unparseable string")
    monkeypatch.setattr(resolver, "_flat_extract", boom)
    with pytest.raises(ValueError):
        resolve_videos("just some text, not a url")


# ── channel / playlist flat enumeration (network mocked) ─────────────────────

def _canned(entries, title="Some Channel"):
    return {"title": title, "entries": entries}


def test_channel_enumeration_basic(monkeypatch):
    monkeypatch.setattr(resolver, "_flat_extract", lambda url: _canned([
        {"id": "v1", "title": "A", "duration": 600},
        {"id": "v2", "title": "B", "duration": 700},
    ]))
    out = resolve_videos("https://www.youtube.com/@manashirRanna")
    assert out["kind"] == "channel"
    assert out["video_ids"] == ["v1", "v2"]
    assert out["total_found"] == 2
    assert out["title"] == "Some Channel"


def test_playlist_kind_detected(monkeypatch):
    monkeypatch.setattr(resolver, "_flat_extract", lambda url: _canned([
        {"id": "v1", "duration": 600},
    ]))
    out = resolve_videos("https://www.youtube.com/playlist?list=PL123")
    assert out["kind"] == "playlist"


def test_min_duration_filters_shorts_and_accounts(monkeypatch):
    monkeypatch.setattr(resolver, "_flat_extract", lambda url: _canned([
        {"id": "short", "duration": 30},
        {"id": "long", "duration": 600},
        {"id": "noDur"},  # missing duration → KEPT (don't guess)
    ]))
    out = resolve_videos("https://www.youtube.com/@chan", min_duration=60)
    assert out["video_ids"] == ["long", "noDur"]
    assert out["skipped"]["too_short"] == 1
    assert out["total_found"] == 2


def test_dedupe_and_order_preserved(monkeypatch):
    monkeypatch.setattr(resolver, "_flat_extract", lambda url: _canned([
        {"id": "v1", "duration": 600},
        {"id": "v2", "duration": 600},
        {"id": "v1", "duration": 600},  # duplicate
    ]))
    out = resolve_videos("https://www.youtube.com/@chan")
    assert out["video_ids"] == ["v1", "v2"]


def test_max_videos_caps_without_silent_drop(monkeypatch):
    monkeypatch.setattr(resolver, "_flat_extract", lambda url: _canned([
        {"id": "v1", "duration": 600},
        {"id": "v2", "duration": 600},
        {"id": "v3", "duration": 600},
    ]))
    out = resolve_videos("https://www.youtube.com/@chan", max_videos=2)
    assert out["video_ids"] == ["v1", "v2"]
    # caller can detect truncation: total_found > len(video_ids)
    assert out["total_found"] == 3
    assert len(out["video_ids"]) == 2


def test_nested_playlist_entries_skipped(monkeypatch):
    monkeypatch.setattr(resolver, "_flat_extract", lambda url: _canned([
        {"id": "pl", "_type": "playlist", "title": "a sub-playlist"},
        {"id": "v1", "duration": 600},
    ]))
    out = resolve_videos("https://www.youtube.com/@chan")
    assert out["video_ids"] == ["v1"]


def test_unavailable_entries_accounted(monkeypatch):
    monkeypatch.setattr(resolver, "_flat_extract", lambda url: _canned([
        {"id": "ok", "duration": 600},
        {"id": "priv", "duration": 600, "availability": "private"},
    ]))
    out = resolve_videos("https://www.youtube.com/@chan")
    assert out["video_ids"] == ["ok"]
    assert out["skipped"]["unavailable"] == 1


def test_channel_root_normalized_to_videos_feed(monkeypatch):
    seen = {}
    def capture(url):
        seen["url"] = url
        return _canned([{"id": "v1", "duration": 600}])
    monkeypatch.setattr(resolver, "_flat_extract", capture)
    resolve_videos("https://www.youtube.com/@manashirRanna")
    assert seen["url"].endswith("/videos")


def test_channel_surfaces_per_video_stubs(monkeypatch):
    monkeypatch.setattr(resolver, "_flat_extract", lambda url: _canned([
        {"id": "v1", "title": "Veg Curry", "duration": 600, "view_count": 1000},
        {"id": "v2", "title": "Fish Fry", "duration": 700, "view_count": 5000},
    ]))
    out = resolve_videos("https://www.youtube.com/@chan")
    assert out["videos"] == [
        {"id": "v1", "title": "Veg Curry", "duration": 600, "view_count": 1000},
        {"id": "v2", "title": "Fish Fry", "duration": 700, "view_count": 5000},
    ]


def test_single_and_list_stubs_have_none_metadata():
    assert resolve_videos("https://youtu.be/aaaaaaaaaaa")["videos"] == [
        {"id": "aaaaaaaaaaa", "title": None, "duration": None, "view_count": None}
    ]
    out = resolve_videos(["https://youtu.be/aaaaaaaaaaa"])
    assert out["videos"][0]["id"] == "aaaaaaaaaaa" and out["videos"][0]["title"] is None
