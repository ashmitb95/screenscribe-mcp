"""
Session management — persists extracted video knowledge to ~/.video-analyzer/.

Each session lives at:
  ~/.video-analyzer/{video_id}/
    session.json   ← metadata + transcript + frame descriptions
    frames/        ← extracted PNG frames
    video.*        ← downloaded video file
"""

import json
from datetime import datetime, timezone
from pathlib import Path

# Global sessions directory — accessible from any project / the MCP server
SESSIONS_DIR = Path.home() / ".video-analyzer"


def session_dir(video_id: str) -> Path:
    return SESSIONS_DIR / video_id


def session_file(video_id: str) -> Path:
    return session_dir(video_id) / "session.json"


def frames_dir(video_id: str) -> Path:
    return session_dir(video_id) / "frames"


def slides_dir(video_id: str) -> Path:
    return session_dir(video_id) / "slides"


def analysis_file(video_id: str) -> Path:
    return session_dir(video_id) / "gemini_analysis.json"


def save_analysis(video_id: str, analysis: dict) -> Path:
    """Persist Gemini's whole-video analysis. Returns the path."""
    path = analysis_file(video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(analysis, indent=2))
    return path


def load_analysis(video_id: str) -> dict | None:
    """Load Gemini's whole-video analysis, or None if it hasn't been produced."""
    path = analysis_file(video_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_session(
    video_id: str,
    url: str,
    title: str,
    duration: float,
    transcript: list[dict],
    frame_descriptions: list[str],
    frames: list[dict],
) -> Path:
    """Persist session data. Returns the path to session.json."""
    path = session_file(video_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "video_id": video_id,
        "url": url,
        "title": title,
        "duration": duration,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "frame_count": len(frames),
        "transcript": transcript,
        "frame_descriptions": frame_descriptions,
        "frames": frames,
    }
    path.write_text(json.dumps(data, indent=2))
    return path


def load_session(video_id: str) -> dict:
    """Load a session by video_id. Raises FileNotFoundError if not found."""
    path = session_file(video_id)
    if not path.exists():
        raise FileNotFoundError(
            f"No session found for '{video_id}'.\n"
            f"Run:  video-analyzer extract <url>"
        )
    return json.loads(path.read_text())


def session_exists(video_id: str) -> bool:
    return session_file(video_id).exists()


def list_sessions() -> list[dict]:
    """Return summary metadata for all sessions, newest first."""
    if not SESSIONS_DIR.exists():
        return []

    summaries = []
    for entry in SESSIONS_DIR.iterdir():
        sf = entry / "session.json"
        if sf.exists():
            try:
                data = json.loads(sf.read_text())
                summaries.append({
                    "session_id": data["video_id"],
                    "title":      data.get("title", "—"),
                    "url":        data.get("url", ""),
                    "duration":   data.get("duration", 0),
                    "frames":     data.get("frame_count", 0),
                    "extracted_at": data.get("extracted_at", ""),
                })
            except Exception:
                pass

    return sorted(summaries, key=lambda x: x["extracted_at"], reverse=True)
