"""
Tests for img-proc-mcp tools after refactor.

All tools now accept image_s3_key instead of image_b64.
S3 and YOLO are mocked at the helper level.
"""

import io
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

sys.path.insert(0, ".")
from app import (
    _fetch_from_s3, _get_regions, _result, _select, _upload_to_s3,
    blur, rotate, flip, resize, crop, add_noise,
)


# ── Test helpers ──────────────────────────────────────────────────────────────

def _make_pil(width=100, height=100, color=(255, 0, 0)) -> Image.Image:
    return Image.new("RGB", (width, height), color=color)


def _fake_regions(count: int = 2, w: int = 40, h: int = 40) -> list[dict]:
    return [
        {"bbox": [i * 60, 10, i * 60 + w, 10 + h], "score": round(0.9 - i * 0.05, 2)}
        for i in range(count)
    ]


def _mock_fetch(img: Image.Image):
    """Patch _fetch_from_s3 to return a PIL image directly."""
    return patch("app._fetch_from_s3", return_value=img)


def _mock_upload(key="processed/fake/result.jpg", url="https://s3.example.com/fake"):
    """Patch _upload_to_s3 to return a fake key/url without hitting S3."""
    return patch("app._upload_to_s3", return_value=(key, url))


def _parse(result_json: str) -> dict:
    return json.loads(result_json)


# ── _select ───────────────────────────────────────────────────────────────────

def test_select_default_returns_first():
    regions = _fake_regions(3)
    assert _select(regions, None, False) == [0]


def test_select_all_objects():
    regions = _fake_regions(3)
    assert _select(regions, None, True) == [0, 1, 2]


def test_select_indices():
    regions = _fake_regions(3)
    assert _select(regions, [0, 2], False) == [0, 2]


def test_select_out_of_range_skipped():
    regions = _fake_regions(2)
    assert _select(regions, [5], False) == []


# ── blur ──────────────────────────────────────────────────────────────────────

def test_blur_whole_image():
    img = _make_pil()
    with _mock_fetch(img), _mock_upload() as mu:
        result = _parse(blur("images/test.jpg", radius=3.0))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"
    assert result["processed_image_url"] == "https://s3.example.com/fake"
    assert "processed_image_base64" in result
    mu.assert_called_once()


def test_blur_object_specific():
    img = _make_pil(200, 200)
    regions = _fake_regions(2)
    with _mock_fetch(img), _mock_upload(), patch("app._get_regions", return_value=regions):
        result = _parse(blur("images/test.jpg", radius=2.0, label="car"))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_blur_label_not_found():
    img = _make_pil()
    with _mock_fetch(img), patch("app._get_regions", return_value=[]):
        result = _parse(blur("images/test.jpg", radius=2.0, label="elephant"))
    assert "error" in result
    assert "elephant" in result["error"]


def test_blur_all_objects():
    img = _make_pil(200, 200)
    regions = _fake_regions(3)
    with _mock_fetch(img), _mock_upload(), patch("app._get_regions", return_value=regions):
        result = _parse(blur("images/test.jpg", radius=2.0, label="car", all_objects=True))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_blur_s3_failure():
    with patch("app._fetch_from_s3", side_effect=Exception("S3 error")):
        result = _parse(blur("images/test.jpg", radius=2.0))
    assert "error" in result
    assert "S3 error" in result["error"]


# ── rotate ────────────────────────────────────────────────────────────────────

def test_rotate_whole_image():
    img = _make_pil(100, 200)
    with _mock_fetch(img), _mock_upload():
        result = _parse(rotate("images/test.jpg", angle=90.0))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_rotate_object_specific():
    img = _make_pil(200, 200)
    regions = _fake_regions(1)
    with _mock_fetch(img), _mock_upload(), patch("app._get_regions", return_value=regions):
        result = _parse(rotate("images/test.jpg", angle=90.0, label="person"))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_rotate_label_not_found():
    img = _make_pil()
    with _mock_fetch(img), patch("app._get_regions", return_value=[]):
        result = _parse(rotate("images/test.jpg", angle=90.0, label="dog"))
    assert "error" in result


# ── flip ──────────────────────────────────────────────────────────────────────

def test_flip_whole_image_horizontal():
    img = _make_pil()
    with _mock_fetch(img), _mock_upload():
        result = _parse(flip("images/test.jpg", direction="horizontal"))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_flip_whole_image_vertical():
    img = _make_pil()
    with _mock_fetch(img), _mock_upload():
        result = _parse(flip("images/test.jpg", direction="vertical"))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_flip_invalid_direction():
    img = _make_pil()
    with _mock_fetch(img):
        result = _parse(flip("images/test.jpg", direction="diagonal"))
    assert "error" in result
    assert "horizontal" in result["error"]


def test_flip_object_specific():
    img = _make_pil(200, 200)
    regions = _fake_regions(2)
    with _mock_fetch(img), _mock_upload(), patch("app._get_regions", return_value=regions):
        result = _parse(flip("images/test.jpg", direction="horizontal", label="car", all_objects=True))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_flip_label_not_found():
    img = _make_pil()
    with _mock_fetch(img), patch("app._get_regions", return_value=[]):
        result = _parse(flip("images/test.jpg", direction="horizontal", label="cat"))
    assert "error" in result


# ── resize ────────────────────────────────────────────────────────────────────

def test_resize_whole_image():
    img = _make_pil(100, 100)
    with _mock_fetch(img), _mock_upload():
        result = _parse(resize("images/test.jpg", width=50, height=50))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_resize_zero_width_error():
    img = _make_pil()
    with _mock_fetch(img):
        result = _parse(resize("images/test.jpg", width=0, height=100))
    assert "error" in result


def test_resize_zero_height_error():
    img = _make_pil()
    with _mock_fetch(img):
        result = _parse(resize("images/test.jpg", width=100, height=0))
    assert "error" in result


def test_resize_object_specific():
    img = _make_pil(200, 200)
    regions = _fake_regions(1)
    with _mock_fetch(img), _mock_upload(), patch("app._get_regions", return_value=regions):
        result = _parse(resize("images/test.jpg", width=30, height=30, label="car"))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_resize_label_not_found():
    img = _make_pil()
    with _mock_fetch(img), patch("app._get_regions", return_value=[]):
        result = _parse(resize("images/test.jpg", width=50, height=50, label="bus"))
    assert "error" in result


# ── crop ─────────────────────────────────────────────────────────────────────

def test_crop_whole_image_percentage():
    img = _make_pil(200, 200)
    with _mock_fetch(img), _mock_upload():
        result = _parse(crop("images/test.jpg", x1=0, y1=0, x2=50, y2=100))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_crop_invalid_percentages():
    img = _make_pil(200, 200)
    with _mock_fetch(img):
        result = _parse(crop("images/test.jpg", x1=80, y1=0, x2=20, y2=100))
    assert "error" in result


def test_crop_object_extracts_bbox():
    img = _make_pil(200, 200)
    regions = [{"bbox": [20, 30, 80, 90], "score": 0.95}]
    with _mock_fetch(img), _mock_upload(), patch("app._get_regions", return_value=regions):
        result = _parse(crop("images/test.jpg", label="car"))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_crop_label_not_found():
    img = _make_pil()
    with _mock_fetch(img), patch("app._get_regions", return_value=[]):
        result = _parse(crop("images/test.jpg", label="plane"))
    assert "error" in result


# ── add_noise ─────────────────────────────────────────────────────────────────

def test_add_noise_whole_image():
    img = _make_pil()
    with _mock_fetch(img), _mock_upload():
        result = _parse(add_noise("images/test.jpg", amount=0.05))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_add_noise_amount_too_high():
    img = _make_pil()
    with _mock_fetch(img):
        result = _parse(add_noise("images/test.jpg", amount=1.5))
    assert "error" in result


def test_add_noise_amount_negative():
    img = _make_pil()
    with _mock_fetch(img):
        result = _parse(add_noise("images/test.jpg", amount=-0.1))
    assert "error" in result


def test_add_noise_zero_amount():
    img = _make_pil()
    with _mock_fetch(img), _mock_upload():
        result = _parse(add_noise("images/test.jpg", amount=0.0))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_add_noise_object_specific():
    img = _make_pil(200, 200)
    regions = _fake_regions(2)
    with _mock_fetch(img), _mock_upload(), patch("app._get_regions", return_value=regions):
        result = _parse(add_noise("images/test.jpg", amount=0.1, label="person", all_objects=True))
    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"


def test_add_noise_label_not_found():
    img = _make_pil()
    with _mock_fetch(img), patch("app._get_regions", return_value=[]):
        result = _parse(add_noise("images/test.jpg", amount=0.1, label="horse"))
    assert "error" in result


# ── from_right selection ───────────────────────────────────────────────────────

def test_from_right_passes_flag_to_get_regions():
    img = _make_pil(200, 200)
    regions = _fake_regions(3)
    with _mock_fetch(img), _mock_upload(), \
         patch("app._get_regions", return_value=regions) as mock_gr:
        _parse(blur("images/test.jpg", radius=2.0, label="car", indices=[0], from_right=True))
    mock_gr.assert_called_once_with("images/test.jpg", "car", True)


# ── Result format ─────────────────────────────────────────────────────────────

def test_result_contains_all_keys():
    img = _make_pil()
    with _mock_fetch(img), _mock_upload():
        result = _parse(blur("images/test.jpg"))
    assert "processed_image_s3_key"  in result
    assert "processed_image_url"     in result
    assert "processed_image_base64"  in result


def test_error_result_has_no_image_keys():
    img = _make_pil()
    with _mock_fetch(img), patch("app._get_regions", return_value=[]):
        result = _parse(blur("images/test.jpg", label="ghost"))
    assert "error" in result
    assert "processed_image_s3_key" not in result
