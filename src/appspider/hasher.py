"""Perceptual hashing for screen deduplication."""

from __future__ import annotations

import imagehash
from PIL import Image


def screen_hash(image: Image.Image, hash_size: int = 16) -> str:
    """Compute a perceptual hash of a screenshot.

    Uses average hash — fast and tolerant of minor rendering differences
    like clock changes, battery level, etc.
    """
    return str(imagehash.average_hash(image, hash_size=hash_size))


def are_similar(hash1: str, hash2: str, threshold: int = 12) -> bool:
    """Check if two screen hashes are similar enough to be the same screen.

    Lower threshold = stricter matching. 12 works well for most apps.
    """
    h1 = imagehash.hex_to_hash(hash1)
    h2 = imagehash.hex_to_hash(hash2)
    return (h1 - h2) < threshold
