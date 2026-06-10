"""
Resolve the ffmpeg / ffprobe binaries.

Prefer a system install (fast, respects the user's own ffmpeg); fall back to the
bundled static-ffmpeg binaries so the tool works with no system ffmpeg present —
`uvx screenscribe ...` needs no `brew install ffmpeg` first. Resolution is cached
for the process; the static binaries are fetched at most once.
"""

import shutil
from pathlib import Path

_cache: dict[str, str] = {}


def _fetch_static() -> tuple[str, str] | None:
    """(ffmpeg, ffprobe) paths from the bundled static binaries, or None."""
    try:
        from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise
        return get_or_fetch_platform_executables_else_raise()
    except Exception:
        return None


def _resolve(name: str) -> str:
    if name in _cache:
        return _cache[name]

    path = shutil.which(name)
    if path is None:
        static = _fetch_static()
        if static is not None:
            _cache["ffmpeg"], _cache["ffprobe"] = static
            path = _cache.get(name)

    # Last resort: the bare name — subprocess raises a clear FileNotFoundError.
    _cache[name] = path or name
    return _cache[name]


def ffmpeg_bin() -> str:
    return _resolve("ffmpeg")


def ffprobe_bin() -> str:
    return _resolve("ffprobe")


def ffmpeg_dir() -> str | None:
    """Directory holding ffmpeg, for yt-dlp's `ffmpeg_location`. None if unresolved."""
    path = ffmpeg_bin()
    return str(Path(path).parent) if path != "ffmpeg" else None
