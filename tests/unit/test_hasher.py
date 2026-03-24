"""Tests for perceptual hashing and screen deduplication."""

from PIL import Image

from appspider.hasher import are_similar, screen_hash


def test_same_image_produces_same_hash(sample_image):
    h1 = screen_hash(sample_image)
    h2 = screen_hash(sample_image)
    assert h1 == h2


def test_identical_images_are_similar(sample_image):
    h = screen_hash(sample_image)
    assert are_similar(h, h)


def test_similar_images_detected(sample_image, sample_image_similar):
    h1 = screen_hash(sample_image)
    h2 = screen_hash(sample_image_similar)
    assert are_similar(h1, h2)


def test_different_images_not_similar():
    """Two images with different patterns should not be similar."""
    # Use patterned images — solid colors hash identically via average hash
    import numpy as np
    arr1 = np.zeros((200, 100, 3), dtype=np.uint8)
    arr1[:100, :, :] = 255  # Top half white
    arr2 = np.zeros((200, 100, 3), dtype=np.uint8)
    arr2[:, :50, :] = 255  # Left half white
    img1 = Image.fromarray(arr1)
    img2 = Image.fromarray(arr2)
    h1 = screen_hash(img1)
    h2 = screen_hash(img2)
    assert not are_similar(h1, h2)


def test_threshold_boundary():
    """Two clearly different patterned images should not be similar."""
    import numpy as np
    # Checkerboard vs inverse checkerboard — maximally different pattern
    arr1 = np.zeros((256, 256, 3), dtype=np.uint8)
    arr2 = np.full((256, 256, 3), 255, dtype=np.uint8)
    for i in range(16):
        for j in range(16):
            if (i + j) % 2 == 0:
                arr1[i*16:(i+1)*16, j*16:(j+1)*16, :] = 255
                arr2[i*16:(i+1)*16, j*16:(j+1)*16, :] = 0
    img1 = Image.fromarray(arr1)
    img2 = Image.fromarray(arr2)
    h1 = screen_hash(img1)
    h2 = screen_hash(img2)
    assert not are_similar(h1, h2, threshold=12)


def test_hash_returns_string(sample_image):
    h = screen_hash(sample_image)
    assert isinstance(h, str)
    assert len(h) > 0


def test_solid_black_image():
    img = Image.new("RGB", (50, 50), (0, 0, 0))
    h = screen_hash(img)
    assert isinstance(h, str)


def test_solid_white_image():
    img = Image.new("RGB", (50, 50), (255, 255, 255))
    h = screen_hash(img)
    assert isinstance(h, str)
