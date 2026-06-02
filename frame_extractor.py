import json
import re
import subprocess
from pathlib import Path

from PIL import Image

# If scene detection yields fewer frames than this, fill gaps with evenly-spaced
# time-based samples so the LLM still sees the full video.
MIN_FRAMES_FALLBACK = 15
FALLBACK_INTERVAL = 10.0  # seconds between fallback samples

# ── Snap-to-stable + perceptual dedup (targeted extraction) ────────────────────
# A transcript cue ("look here") usually precedes the completed visual. After
# picking a timestamp we scan FORWARD up to SNAP_WINDOW seconds and capture the
# first frame where the screen has settled, so we land on the finished
# annotation instead of mid-draw.
SNAP_WINDOW = 6.0          # seconds to scan forward from the picked timestamp
SNAP_STEP = 1.5            # seconds between samples while searching
# Two frames count as "settled" (snap) or "duplicate" (dedup) when their
# perceptual difference-hash (dHash, 16x16 = 256-bit) Hamming distance is <= the
# threshold. 16x16 (not 8x8) is needed to register sparse annotations on line-art
# charts: at 8x8 a blank diagram and the same diagram fully annotated hash only
# ~5 apart (indistinguishable from a true duplicate); at 16x16 they're ~20+ apart
# while a genuinely static screen stays ~11.
SNAP_STABLE_HAMMING = 12
DEDUP_HAMMING = 14


def dhash(path: Path, hash_size: int = 16) -> int:
    """
    Perceptual difference-hash of an image (256-bit by default). Robust to tiny
    rendering differences; near-identical frames produce hashes a small Hamming
    distance apart. Used to drop duplicate frames and detect a settled screen.
    """
    img = Image.open(path).convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    px = img.tobytes()  # row-major, one byte per pixel in mode "L"
    bits = 0
    for row in range(hash_size):
        base = row * (hash_size + 1)
        for col in range(hash_size):
            bits = (bits << 1) | int(px[base + col] > px[base + col + 1])
    return bits


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two perceptual hashes."""
    return bin(a ^ b).count("1")


def _snap_to_stable(video_path, ts, max_width, tmp_dir,
                    window=SNAP_WINDOW, step=SNAP_STEP, stable_hamming=SNAP_STABLE_HAMMING):
    """
    Scan forward from `ts` and return the first timestamp where the on-screen
    visual has settled (a sample is near-identical to the next one) — i.e. the
    drawing/annotation is complete. Falls back to `ts` if there aren't enough
    samples, or to the last sample if the screen never settles within `window`.
    """
    samples = []
    t = ts
    end = ts + window
    while t <= end:
        tmp = tmp_dir / f"_snap_{t:.2f}.png"
        if extract_frame(video_path, t, tmp, max_width):
            samples.append((t, dhash(tmp)))
            tmp.unlink(missing_ok=True)
        t += step

    if len(samples) < 2:
        return ts
    for (t1, h1), (_t2, h2) in zip(samples, samples[1:]):
        if hamming(h1, h2) <= stable_hamming:
            return t1  # frame at t1 already looks like t1+step → settled
    return samples[-1][0]


def detect_scene_changes(video_path: Path, threshold: float) -> list[float]:
    """
    Run ffmpeg's scene change filter and return a list of timestamps (seconds)
    where a significant visual change is detected.

    ffmpeg writes showinfo output to stderr — we parse pts_time from there.
    """
    cmd = [
        "ffmpeg",
        "-i", str(video_path),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-vsync", "vfr",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    timestamps = []
    for line in result.stderr.splitlines():
        if "pts_time" in line and "showinfo" in line:
            match = re.search(r"pts_time:([\d.]+)", line)
            if match:
                timestamps.append(float(match.group(1)))

    return timestamps


def apply_min_interval(timestamps: list[float], min_interval: float) -> list[float]:
    """
    Drop timestamps that are closer than min_interval seconds to the previous
    kept timestamp. Prevents burst-capturing during slow zoom/pan animations.
    """
    if not timestamps:
        return []

    filtered = [timestamps[0]]
    for t in timestamps[1:]:
        if t - filtered[-1] >= min_interval:
            filtered.append(t)
    return filtered


def extract_frame(video_path: Path, timestamp: float, output_path: Path, max_width: int) -> bool:
    """
    Extract a single frame at `timestamp` seconds from `video_path`.
    Resizes to max_width if the frame is wider. Returns True if successful.
    """
    cmd = [
        "ffmpeg",
        "-ss", str(timestamp),
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
        str(output_path),
        "-y",
    ]
    subprocess.run(cmd, capture_output=True)

    if not output_path.exists():
        return False

    # Resize if needed to reduce API token cost
    img = Image.open(output_path)
    if img.width > max_width:
        ratio = max_width / img.width
        new_size = (max_width, int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)
        img.save(output_path)

    return True


def get_video_duration(video_path: Path) -> float:
    """Return video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def build_fallback_timestamps(duration: float, existing: list[float], interval: float) -> list[float]:
    """
    Generate evenly-spaced timestamps that don't overlap with existing ones (±interval/2).
    Used when scene detection finds too few frames.
    """
    extra = []
    t = interval
    while t < duration:
        if not any(abs(t - e) < interval / 2 for e in existing):
            extra.append(t)
        t += interval
    return extra


def extract_frames_at_timestamps(
    video_path: Path,
    frames_dir: Path,
    selections: list[dict],
    max_width: int,
    save_metadata: bool = True,
    snap: bool = True,
    dedup: bool = True,
) -> list[dict]:
    """
    Extract frames at specific pre-selected timestamps.
    `selections` is a list of {"timestamp": float, "reason": str}.

    snap:  scan forward from each timestamp to land on the frame where the
           visual has settled (drawing complete), not mid-draw.
    dedup: drop a frame that is perceptually near-identical to one already kept
           (e.g. two transcript cues over the same static chart).

    Returns list of dicts: [{'timestamp': float, 'path': str, 'reason': str}, ...]
    Writes frames/frames.json unless save_metadata is False.
    """
    frames_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    kept_hashes = []  # (timestamp, dhash) for each accepted frame
    total = len(selections)
    for n, sel in enumerate(selections, 1):
        ts = sel["timestamp"]
        reason = sel.get("reason", "")
        if snap:
            ts = _snap_to_stable(video_path, ts, max_width, frames_dir)

        # Index by kept count so accepted frames stay contiguous (frame_0000, ...).
        output_path = frames_dir / f"frame_{len(frames):04d}_{ts:.2f}s.png"
        ok = extract_frame(video_path, ts, output_path, max_width)
        if not ok:
            print(f"  [{n}/{total}] {ts:.1f}s → FAILED (skipped)")
            continue

        if dedup:
            h = dhash(output_path)
            dup = next((kt for kt, kh in kept_hashes if hamming(h, kh) <= DEDUP_HAMMING), None)
            if dup is not None:
                output_path.unlink(missing_ok=True)
                print(f"  [{n}/{total}] {ts:.1f}s → skipped (near-duplicate of {dup:.1f}s)")
                continue
            kept_hashes.append((ts, h))

        frames.append({"timestamp": ts, "path": str(output_path), "reason": reason})
        print(f"  [{n}/{total}] {ts:.1f}s → {output_path.name}")

    if save_metadata:
        (frames_dir / "frames.json").write_text(json.dumps(frames, indent=2))
        print(f"  Frame metadata saved to {frames_dir / 'frames.json'}")
    return frames


def extract_frames(
    video_path: Path,
    frames_dir: Path,
    threshold: float,
    min_interval: float,
    max_width: int,
) -> list[dict]:
    """
    Full pipeline: detect scene changes → filter by min interval → extract frames.
    Falls back to time-based sampling if too few scene changes detected.
    Returns list of dicts: [{'timestamp': float, 'path': str}, ...]
    Also writes frames/frames.json for inspection.
    """
    frames_dir.mkdir(parents=True, exist_ok=True)

    print("  Detecting scene changes...")
    raw = detect_scene_changes(video_path, threshold)
    timestamps = apply_min_interval(raw, min_interval)
    print(f"  {len(raw)} raw scene changes → {len(timestamps)} frames after {min_interval}s interval filter")

    # Fallback: if scene detection found too few frames, pad with time-based samples
    if len(timestamps) < MIN_FRAMES_FALLBACK:
        duration = get_video_duration(video_path)
        if duration > 0:
            extras = build_fallback_timestamps(duration, timestamps, FALLBACK_INTERVAL)
            timestamps = sorted(timestamps + extras)
            print(f"  Too few scene changes — added {len(extras)} time-based samples every {FALLBACK_INTERVAL}s → {len(timestamps)} total frames")

    frames = []
    for i, ts in enumerate(timestamps):
        output_path = frames_dir / f"frame_{i:04d}_{ts:.2f}s.png"
        ok = extract_frame(video_path, ts, output_path, max_width)
        if ok:
            frames.append({"timestamp": ts, "path": str(output_path)})
            print(f"  [{i + 1}/{len(timestamps)}] {ts:.1f}s → {output_path.name}")
        else:
            print(f"  [{i + 1}/{len(timestamps)}] {ts:.1f}s → FAILED (skipped)")

    (frames_dir / "frames.json").write_text(json.dumps(frames, indent=2))
    print(f"  Frame metadata saved to {frames_dir / 'frames.json'}")
    return frames
