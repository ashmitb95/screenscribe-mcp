"""
Context loading — accepts any combination of:
  - File paths         (single files or whole directories)
  - HTTP/HTTPS URLs    (fetches and includes page text)
  - Raw text strings   (passed directly)
  - stdin              (piped input)

Usage:
  sources = ["./src/strategy.py", "https://docs.example.com/api", "my note here"]
  ctx = load_context(sources, from_stdin=True)
"""

import sys
import urllib.request
from pathlib import Path

# File extensions to include when scanning a directory
_CODE_EXTENSIONS = {
    ".py", ".ts", ".js", ".tsx", ".jsx",
    ".go", ".rs", ".java", ".cs", ".cpp", ".c", ".h",
    ".md", ".txt", ".yaml", ".yml", ".toml", ".json",
    ".csv", ".sql",
}

# Directories to skip when scanning
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", "venv", ".venv", "dist", "build", ".next"}


def load_context(sources: list[str], from_stdin: bool = False) -> str:
    """
    Load and concatenate context from multiple sources.
    Returns a single string, or empty string if no sources provided.
    """
    parts = []

    if from_stdin and not sys.stdin.isatty():
        stdin_text = sys.stdin.read().strip()
        if stdin_text:
            parts.append(f"### stdin\n{stdin_text}")

    for source in sources:
        text = _load_one(source)
        if text:
            parts.append(text)

    return "\n\n".join(parts)


def _load_one(source: str) -> str:
    if source.startswith("http://") or source.startswith("https://"):
        return _load_url(source)

    path = Path(source)
    if path.exists():
        return _load_path(path)

    # Treat as raw text (user pasted something inline)
    return f"### (inline context)\n{source}"


def _load_url(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "screenscribe/0.2"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        # Strip HTML tags crudely if it looks like HTML
        if "<html" in raw[:500].lower():
            import re
            raw = re.sub(r"<[^>]+>", " ", raw)
            raw = re.sub(r"\s{3,}", "\n\n", raw)
        return f"### {url}\n{raw.strip()}"
    except Exception as e:
        return f"### {url}\n[Failed to fetch: {e}]"


def _load_path(path: Path) -> str:
    if path.is_file():
        return _read_file(path, label=path.name)

    if path.is_dir():
        parts = []
        for f in sorted(path.rglob("*")):
            if f.is_file() and f.suffix in _CODE_EXTENSIONS:
                if not any(skip in f.parts for skip in _SKIP_DIRS):
                    rel = f.relative_to(path)
                    parts.append(_read_file(f, label=str(rel)))
        if not parts:
            return f"### {path}/\n[No readable files found]"
        return "\n\n".join(parts)

    return ""


def _read_file(path: Path, label: str) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        return f"### {label}\n```\n{content}\n```"
    except Exception as e:
        return f"### {label}\n[Could not read: {e}]"
