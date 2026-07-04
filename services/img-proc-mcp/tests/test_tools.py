import sys
import pytest
from PIL import Image

sys.path.insert(0, ".")
from app import _decode, _encode, blur, rotate, flip, resize, crop, add_noise


def make_image(width=100, height=100, color=(255, 0, 0)) -> str:
    """Create a solid color image and return as base64 PNG."""
    return _encode(Image.new("RGB", (width, height), color=color))


# ── blur ──────────────────────────────────────────────────────────────────────

def test_blur_returns_string():
    assert isinstance(blur(make_image(), radius=2.0), str)


def test_blur_changes_image():
    # Solid color images don't change when blurred — use a checkerboard so pixels differ.
    img = Image.new("RGB", (100, 100))
    pixels = img.load()
    for y in range(100):
        for x in range(100):
            pixels[x, y] = (255, 255, 255) if (x + y) % 2 == 0 else (0, 0, 0)
    b64 = _encode(img)
    assert blur(b64, radius=5.0) != b64


def test_blur_preserves_size():
    b64 = make_image(80, 60)
    assert _decode(blur(b64, radius=2.0)).size == (80, 60)


# ── rotate ────────────────────────────────────────────────────────────────────

def test_rotate_90_swaps_dimensions():
    b64 = make_image(width=100, height=200)
    result = _decode(rotate(b64, angle=90))
    assert result.size == (200, 100)


def test_rotate_180_keeps_dimensions():
    b64 = make_image(width=100, height=200)
    result = _decode(rotate(b64, angle=180))
    assert result.size == (100, 200)


def test_rotate_360_same_pixels():
    b64 = make_image(100, 100, color=(0, 128, 255))
    result = rotate(b64, angle=360)
    orig = _decode(b64)
    rotated = _decode(result)
    assert orig.size == rotated.size


# ── flip ──────────────────────────────────────────────────────────────────────

def test_flip_horizontal_twice_restores():
    b64 = make_image()
    restored = flip(flip(b64, "horizontal"), "horizontal")
    assert _decode(restored).tobytes() == _decode(b64).tobytes()


def test_flip_vertical_twice_restores():
    b64 = make_image()
    restored = flip(flip(b64, "vertical"), "vertical")
    assert _decode(restored).tobytes() == _decode(b64).tobytes()


def test_flip_invalid_direction_raises():
    with pytest.raises(ValueError, match="direction must be"):
        flip(make_image(), direction="diagonal")


def test_flip_preserves_size():
    b64 = make_image(120, 80)
    assert _decode(flip(b64, "horizontal")).size == (120, 80)


# ── resize ────────────────────────────────────────────────────────────────────

def test_resize_correct_output_size():
    result = _decode(resize(make_image(100, 100), width=50, height=30))
    assert result.size == (50, 30)


def test_resize_same_dimensions():
    b64 = make_image(100, 100)
    assert _decode(resize(b64, width=100, height=100)).size == (100, 100)


def test_resize_zero_width_raises():
    with pytest.raises(ValueError):
        resize(make_image(), width=0, height=100)


def test_resize_zero_height_raises():
    with pytest.raises(ValueError):
        resize(make_image(), width=100, height=0)


# ── crop ──────────────────────────────────────────────────────────────────────

def test_crop_correct_output_size():
    result = _decode(crop(make_image(100, 100), x1=10, y1=20, x2=60, y2=70))
    assert result.size == (50, 50)


def test_crop_out_of_bounds_raises():
    with pytest.raises(ValueError):
        crop(make_image(100, 100), x1=0, y1=0, x2=200, y2=200)


def test_crop_inverted_box_raises():
    with pytest.raises(ValueError):
        crop(make_image(100, 100), x1=80, y1=80, x2=10, y2=10)


def test_crop_negative_raises():
    with pytest.raises(ValueError):
        crop(make_image(100, 100), x1=-5, y1=0, x2=50, y2=50)


# ── add_noise ─────────────────────────────────────────────────────────────────

def test_add_noise_returns_string():
    assert isinstance(add_noise(make_image(), amount=0.05), str)


def test_add_noise_preserves_size():
    b64 = make_image(80, 60)
    assert _decode(add_noise(b64, amount=0.1)).size == (80, 60)


def test_add_noise_above_range_raises():
    with pytest.raises(ValueError):
        add_noise(make_image(), amount=1.5)


def test_add_noise_below_range_raises():
    with pytest.raises(ValueError):
        add_noise(make_image(), amount=-0.1)


def test_add_noise_zero_amount():
    b64 = make_image()
    result = add_noise(b64, amount=0.0)
    assert _decode(result).size == _decode(b64).size
