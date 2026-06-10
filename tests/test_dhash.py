"""
Tests for the perceptual difference-hash (dHash) used to dedup frames and
detect a settled screen. These build images with Pillow directly, so they need
no ffmpeg or video.
"""

from PIL import Image, ImageDraw

from screenscribe.frame_extractor import DEDUP_HAMMING, dhash, hamming


def _gradient(path, shift=0):
    """A horizontal gradient (gives dHash plenty of signal), optionally shifted."""
    img = Image.new("L", (160, 120))
    px = img.load()
    for x in range(160):
        for y in range(120):
            px[x, y] = (x * 3 + shift) % 256
    img.save(path)


def test_identical_images_have_zero_distance(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _gradient(a)
    _gradient(b)
    assert hamming(dhash(a), dhash(b)) == 0


def test_near_identical_images_are_within_dedup_threshold(tmp_path):
    # Same gradient with one extra small annotation — like a chart with a tiny
    # added mark. Should still be flagged as a near-duplicate.
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _gradient(a)
    img = Image.open(a).copy()
    ImageDraw.Draw(img).ellipse([10, 10, 16, 16], fill=0)
    img.save(b)
    assert hamming(dhash(a), dhash(b)) <= DEDUP_HAMMING


def test_different_images_exceed_dedup_threshold(tmp_path):
    # A gradient vs a vertically-banded image — structurally different content.
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _gradient(a)
    img = Image.new("L", (160, 120))
    px = img.load()
    for x in range(160):
        for y in range(120):
            px[x, y] = 255 if (y // 8) % 2 == 0 else 0
    img.save(b)
    assert hamming(dhash(a), dhash(b)) > DEDUP_HAMMING


def test_hash_is_64_bits():
    assert hamming(0, (1 << 64) - 1) == 64
