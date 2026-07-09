import ast
import base64
import io
import json
import os
import random
import uuid
from typing import Annotated

import boto3
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from pydantic import Field

load_dotenv()

mcp = FastMCP("img-proc", host="0.0.0.0")

AWS_S3_BUCKET    = os.environ.get("AWS_S3_BUCKET", "")
AWS_REGION       = os.environ.get("AWS_REGION", "us-east-1")
YOLO_SERVICE_URL = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")

s3_client = boto3.client("s3", region_name=AWS_REGION)


# ── S3 helpers ────────────────────────────────────────────────────────────────

def _fetch_from_s3(s3_key: str) -> Image.Image:
    obj = s3_client.get_object(Bucket=AWS_S3_BUCKET, Key=s3_key)
    return ImageOps.exif_transpose(
        Image.open(io.BytesIO(obj["Body"].read()))
    ).convert("RGB")


def _upload_to_s3(img: Image.Image) -> tuple[str, str]:
    key = f"processed/{uuid.uuid4()}/result.jpg"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    s3_client.put_object(
        Bucket=AWS_S3_BUCKET,
        Key=key,
        Body=buf.getvalue(),
        ContentType="image/jpeg",
    )
    try:
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": AWS_S3_BUCKET, "Key": key},
            ExpiresIn=3600,
        )
    except Exception:
        url = None
    return key, url


def _result(img: Image.Image) -> str:
    """Upload to S3 and return JSON string with key, url, and base64 thumbnail."""
    key, url = _upload_to_s3(img)
    thumb = img.copy()
    thumb.thumbnail((768, 768), Image.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=85)
    return json.dumps({
        "processed_image_s3_key": key,
        "processed_image_url":    url,
        "processed_image_base64": base64.b64encode(buf.getvalue()).decode(),
    })


# ── YOLO helpers ──────────────────────────────────────────────────────────────

def _get_regions(s3_key: str, label: str, from_right: bool = False) -> list[dict]:
    """Call YOLO, filter by label, sort left-to-right (reversed when from_right=True)."""
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(f"{YOLO_SERVICE_URL}/predict", json={"image_s3_key": s3_key})
        resp.raise_for_status()
        uid = resp.json()["prediction_uid"]
        det = client.get(f"{YOLO_SERVICE_URL}/prediction/{uid}")
        det.raise_for_status()

    regions = []
    for obj in det.json().get("detection_objects", []):
        if obj["label"].lower() != label.lower():
            continue
        try:
            box = ast.literal_eval(obj["box"])
            x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
            if x2 > x1 and y2 > y1:
                regions.append({"bbox": [x1, y1, x2, y2], "score": obj.get("score", 0.0)})
        except (ValueError, IndexError, TypeError):
            continue

    regions.sort(key=lambda r: r["bbox"][0], reverse=from_right)
    return regions


def _select(regions: list[dict], indices: list[int] | None, all_objects: bool) -> list[int]:
    """Resolve which region indices to process."""
    n = len(regions)
    if all_objects:
        return list(range(n))
    if indices:
        return [i % n for i in indices if -n <= i < n]
    return [0]


def _detection_key(image_s3_key: str, detection_s3_key: str | None) -> str:
    """Return the key to use for YOLO detection — original image when provided."""
    return detection_s3_key if detection_s3_key else image_s3_key


# ── Image tools ───────────────────────────────────────────────────────────────

@mcp.tool()
def blur(
    image_s3_key: str,
    radius: float = 2.0,
    label: str | None = None,
    indices: list[int] | None = None,
    all_objects: bool = False,
    from_right: bool = False,
    detection_s3_key: str | None = None,
) -> str:
    """Apply Gaussian blur to the whole image or to specific detected objects."""
    try:
        img = _fetch_from_s3(image_s3_key)
    except Exception as e:
        return json.dumps({"error": f"Failed to load image: {e}"})

    if not label:
        return _result(img.filter(ImageFilter.GaussianBlur(radius)))

    regions = _get_regions(_detection_key(image_s3_key, detection_s3_key), label, from_right)
    if not regions:
        return json.dumps({"error": f"No '{label}' found in image"})

    out = img.copy()
    for idx in _select(regions, indices, all_objects):
        x1, y1, x2, y2 = regions[idx]["bbox"]
        patch = img.crop((x1, y1, x2, y2)).filter(ImageFilter.GaussianBlur(radius))
        out.paste(patch.resize((x2 - x1, y2 - y1), Image.LANCZOS), (x1, y1))
    return _result(out)


@mcp.tool()
def rotate(
    image_s3_key: str,
    angle: float = 90.0,
    label: str | None = None,
    indices: list[int] | None = None,
    all_objects: bool = False,
    from_right: bool = False,
    detection_s3_key: str | None = None,
) -> str:
    """Rotate the whole image or specific detected objects counter-clockwise."""
    try:
        img = _fetch_from_s3(image_s3_key)
    except Exception as e:
        return json.dumps({"error": f"Failed to load image: {e}"})

    if not label:
        return _result(img.rotate(angle, expand=True))

    regions = _get_regions(_detection_key(image_s3_key, detection_s3_key), label, from_right)
    if not regions:
        return json.dumps({"error": f"No '{label}' found in image"})

    out = img.copy()
    for idx in _select(regions, indices, all_objects):
        x1, y1, x2, y2 = regions[idx]["bbox"]
        patch = img.crop((x1, y1, x2, y2)).rotate(angle, expand=False)
        out.paste(patch.resize((x2 - x1, y2 - y1), Image.LANCZOS), (x1, y1))
    return _result(out)


@mcp.tool()
def flip(
    image_s3_key: str,
    direction: str = "horizontal",
    label: str | None = None,
    indices: list[int] | None = None,
    all_objects: bool = False,
    from_right: bool = False,
    detection_s3_key: str | None = None,
) -> str:
    """Flip the whole image or specific detected objects horizontally or vertically."""
    if direction not in ("horizontal", "vertical"):
        return json.dumps({"error": "direction must be 'horizontal' or 'vertical'"})

    try:
        img = _fetch_from_s3(image_s3_key)
    except Exception as e:
        return json.dumps({"error": f"Failed to load image: {e}"})

    op = Image.FLIP_LEFT_RIGHT if direction == "horizontal" else Image.FLIP_TOP_BOTTOM

    if not label:
        return _result(img.transpose(op))

    regions = _get_regions(_detection_key(image_s3_key, detection_s3_key), label, from_right)
    if not regions:
        return json.dumps({"error": f"No '{label}' found in image"})

    out = img.copy()
    for idx in _select(regions, indices, all_objects):
        x1, y1, x2, y2 = regions[idx]["bbox"]
        patch = img.crop((x1, y1, x2, y2)).transpose(op)
        out.paste(patch.resize((x2 - x1, y2 - y1), Image.LANCZOS), (x1, y1))
    return _result(out)


@mcp.tool()
def resize(
    image_s3_key: str,
    width: int = 256,
    height: int = 256,
    label: str | None = None,
    indices: list[int] | None = None,
    all_objects: bool = False,
    from_right: bool = False,
    detection_s3_key: str | None = None,
) -> str:
    """Resize the whole image or show a resized object within its bounding box."""
    if width <= 0 or height <= 0:
        return json.dumps({"error": "width and height must be positive integers"})

    try:
        img = _fetch_from_s3(image_s3_key)
    except Exception as e:
        return json.dumps({"error": f"Failed to load image: {e}"})

    if not label:
        return _result(img.resize((width, height), Image.LANCZOS))

    regions = _get_regions(_detection_key(image_s3_key, detection_s3_key), label, from_right)
    if not regions:
        return json.dumps({"error": f"No '{label}' found in image"})

    out = img.copy()
    px  = out.load()
    for idx in _select(regions, indices, all_objects):
        x1, y1, x2, y2 = regions[idx]["bbox"]
        bw, bh  = x2 - x1, y2 - y1
        resized = img.crop((x1, y1, x2, y2)).resize((width, height), Image.LANCZOS)

        border = []
        for bx in range(x1, x2):
            if y1 > 0:          border.append(px[bx, y1 - 1])
            if y2 < img.height: border.append(px[bx, y2])
        for by in range(y1, y2):
            if x1 > 0:         border.append(px[x1 - 1, by])
            if x2 < img.width: border.append(px[x2, by])
        avg = (
            tuple(int(sum(c[i] for c in border) / len(border)) for i in range(3))
            if border else (128, 128, 128)
        )
        ImageDraw.Draw(out).rectangle([x1, y1, x2 - 1, y2 - 1], fill=avg)

        cx = max(0, min(x1 + (bw - width)  // 2, out.width  - width))
        cy = max(0, min(y1 + (bh - height) // 2, out.height - height))
        out.paste(resized, (cx, cy))
    return _result(out)


@mcp.tool()
def crop(
    image_s3_key: str,
    x1: Annotated[int, Field(description="Left boundary as percentage of image width (0-100). Example: 25 means 25% from the left.")] = 0,
    y1: Annotated[int, Field(description="Top boundary as percentage of image height (0-100). Example: 0 means top edge.")] = 0,
    x2: Annotated[int, Field(description="Right boundary as percentage of image width (0-100). Example: 75 means 75% from the left.")] = 100,
    y2: Annotated[int, Field(description="Bottom boundary as percentage of image height (0-100). Example: 100 means bottom edge.")] = 100,
    label: str | None = None,
    indices: list[int] | None = None,
    from_right: bool = False,
    detection_s3_key: str | None = None,
) -> str:
    """
    Without label: crop full image using percentages (0-100). x1/y1/x2/y2 are percentages NOT pixels.
    With label: extract the detected object bounding box.
    """
    try:
        img = _fetch_from_s3(image_s3_key)
    except Exception as e:
        return json.dumps({"error": f"Failed to load image: {e}"})

    w, h = img.size

    if not label:
        px1 = int(x1 * w / 100)
        py1 = int(y1 * h / 100)
        px2 = int(x2 * w / 100)
        py2 = int(y2 * h / 100)
        if px1 >= px2 or py1 >= py2:
            return json.dumps({"error": f"Invalid crop percentages ({x1},{y1},{x2},{y2})"})
        return _result(img.crop((px1, py1, px2, py2)))

    regions = _get_regions(_detection_key(image_s3_key, detection_s3_key), label, from_right)
    if not regions:
        return json.dumps({"error": f"No '{label}' found in image"})

    selected = _select(regions, indices, all_objects=False)
    rx1, ry1, rx2, ry2 = regions[selected[0]]["bbox"]
    return _result(img.crop((rx1, ry1, rx2, ry2)))


@mcp.tool()
def add_noise(
    image_s3_key: str,
    amount: float = 0.05,
    label: str | None = None,
    indices: list[int] | None = None,
    all_objects: bool = False,
    from_right: bool = False,
    detection_s3_key: str | None = None,
) -> str:
    """Add salt-and-pepper noise to the whole image or to specific detected objects."""
    if not 0.0 <= amount <= 1.0:
        return json.dumps({"error": "amount must be between 0.0 and 1.0"})

    try:
        img = _fetch_from_s3(image_s3_key).convert("RGB")
    except Exception as e:
        return json.dumps({"error": f"Failed to load image: {e}"})

    def _apply(patch: Image.Image) -> Image.Image:
        p = patch.load()
        pw, ph = patch.size
        for _ in range(int(pw * ph * amount)):
            x = random.randint(0, pw - 1)
            y = random.randint(0, ph - 1)
            p[x, y] = (255, 255, 255) if random.random() > 0.5 else (0, 0, 0)
        return patch

    if not label:
        return _result(_apply(img))

    regions = _get_regions(_detection_key(image_s3_key, detection_s3_key), label, from_right)
    if not regions:
        return json.dumps({"error": f"No '{label}' found in image"})

    out = img.copy()
    for idx in _select(regions, indices, all_objects):
        x1, y1, x2, y2 = regions[idx]["bbox"]
        patch = _apply(img.crop((x1, y1, x2, y2)))
        out.paste(patch.resize((x2 - x1, y2 - y1), Image.LANCZOS), (x1, y1))
    return _result(out)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9000)
