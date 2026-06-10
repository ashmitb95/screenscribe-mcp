"""
Multi-input resolver — the shared front door.

Turns any of four input shapes into a normalized, de-duplicated, ordered list of
YouTube video IDs:
  1. a single video URL        -> kind="video"
  2. a channel URL (@handle / channel/UC… / c/… / user/…)  -> kind="channel"
  3. a playlist URL (…list=…)  -> kind="playlist"
  4. a list of video URLs      -> kind="list"

Channel and playlist resolution are the same yt-dlp `extract_flat` operation: both
return a lightweight `entries` list (id/title/duration) with no download and no
per-video visit. The resolver does NO extraction and NO download itself — only
yt-dlp metadata. Per-video extraction (the ~$0.03 Gemini call) is the caller's job.

Used by per-video extraction (one ID) and cross-video synthesis (many).
"""

import re

_VIDEO_PATTERNS = [
    r"youtu\.be/([^?&/]+)",
    r"youtube\.com/watch\?v=([^&]+)",
    r"youtube\.com/embed/([^?&/]+)",
    r"youtube\.com/shorts/([^?&/]+)",
]

_CHANNEL_ROOT = re.compile(r"youtube\.com/(@[^/?&]+|channel/[^/?&]+|c/[^/?&]+|user/[^/?&]+)/?$")


def _parse_video_id(url: str) -> str | None:
    """Return the video ID for a single-video URL, else None."""
    if not isinstance(url, str):
        return None
    for pattern in _VIDEO_PATTERNS:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def parse_video_id(url: str) -> str:
    """Return the video ID for a single-video URL; raise ValueError if it isn't one.
    The canonical single-video parser — other modules delegate here."""
    vid = _parse_video_id(url)
    if vid is None:
        raise ValueError(f"Cannot extract video ID from: {url}")
    return vid


def _looks_like_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _normalize_channel_url(url: str) -> str:
    """Point a bare channel/handle root at its uploads feed (/videos), so flat
    enumeration yields videos rather than the channel home's tab/shelf tree.
    Playlists and already-/videos URLs are left untouched."""
    if "list=" in url:
        return url
    if _CHANNEL_ROOT.search(url):
        return url.rstrip("/") + "/videos"
    return url


def _flat_extract(url: str) -> dict:
    """Flat-enumerate a channel/playlist via yt-dlp (no download, no per-video
    visit). Returns the info dict (with `entries` + `title`). Isolated so tests
    monkeypatch it instead of hitting the network."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


_UNAVAILABLE = {"private", "needs_auth", "subscriber_only", "premium_only"}


def resolve_videos(source, *, max_videos: int | None = None, min_duration: int = 0) -> dict:
    """
    Resolve `source` to a normalized list of video IDs.

    source: a single-video URL, a channel URL, a playlist URL, or a list of
            video URLs.
    max_videos: cap applied AFTER filtering; truncation is visible via
                `total_found > len(video_ids)` (never a silent cap).
    min_duration: drop entries shorter than this many seconds (e.g. Shorts);
                  entries with no known duration are KEPT (don't guess).

    Returns a ResolveResult dict:
      {kind, source, video_ids, videos:[{id,title,duration,view_count}], title,
       skipped:{too_short, unavailable}, total_found}
    `videos` carries per-video stubs (title/view_count populated only for
    channel/playlist flat enumeration; None for single-video and list inputs).
    """
    def _stub(vid, e=None):
        e = e or {}
        return {"id": vid, "title": e.get("title"),
                "duration": e.get("duration"), "view_count": e.get("view_count")}

    # 1. List input → map each element through the single-video parse.
    if isinstance(source, list):
        seen, ids = set(), []
        for item in source:
            vid = _parse_video_id(item)
            if not vid:
                raise ValueError(f"List element is not a single-video URL: {item!r}")
            if vid not in seen:
                seen.add(vid)
                ids.append(vid)
        return {
            "kind": "list", "source": source, "video_ids": ids,
            "videos": [_stub(v) for v in ids], "title": None,
            "skipped": {"too_short": 0, "unavailable": 0}, "total_found": len(ids),
        }

    if not isinstance(source, str):
        raise ValueError(f"source must be a URL string or a list of URLs, got {type(source).__name__}")

    # 2. String matching the single-video regex → one ID.
    vid = _parse_video_id(source)
    if vid:
        return {
            "kind": "video", "source": source, "video_ids": [vid],
            "videos": [_stub(vid)], "title": None,
            "skipped": {"too_short": 0, "unavailable": 0}, "total_found": 1,
        }

    # Reject clearly-unparseable strings before any network call.
    if not _looks_like_youtube(source):
        raise ValueError(f"Not a recognized YouTube video, channel, or playlist URL: {source!r}")

    # 3. Channel / playlist → flat enumerate.
    kind = "playlist" if "list=" in source else "channel"
    info = _flat_extract(_normalize_channel_url(source))
    entries = info.get("entries") or []
    title = info.get("title")

    seen, qualifying = set(), []
    too_short = unavailable = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("_type") == "playlist":   # nested playlist — don't recurse in v1
            continue
        vid = e.get("id")
        if not vid:
            continue
        if e.get("availability") in _UNAVAILABLE or e.get("live_status") == "is_upcoming":
            unavailable += 1
            continue
        dur = e.get("duration")
        if dur is not None and dur < min_duration:
            too_short += 1
            continue
        if vid in seen:
            continue
        seen.add(vid)
        qualifying.append(_stub(vid, e))

    stubs = qualifying[:max_videos] if max_videos is not None else qualifying
    return {
        "kind": kind, "source": source,
        "video_ids": [s["id"] for s in stubs], "videos": stubs, "title": title,
        "skipped": {"too_short": too_short, "unavailable": unavailable},
        "total_found": len(qualifying),
    }
