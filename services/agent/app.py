import ast
import base64
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Optional
import time
import uuid

import boto3
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.getLogger("langchain").setLevel(logging.DEBUG)
logging.getLogger("langchain_core").setLevel(logging.DEBUG)

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_core.tools import tool, StructuredTool
from PIL import Image as PILImage, ImageDraw, ImageOps
from pydantic import BaseModel, Field

YOLO_SERVICE_URL  = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
MCP_SERVER_URL    = os.environ.get("MCP_SERVER_URL", "http://localhost:9000")
MODEL             = os.environ.get("MODEL")
AWS_REGION        = os.environ.get("AWS_REGION")
AWS_S3_BUCKET     = os.environ.get("AWS_S3_BUCKET")

for _var in ("AWS_REGION", "AWS_S3_BUCKET"):
    if not os.environ.get(_var):
        raise SystemExit(f"\n[ERROR] Required environment variable '{_var}' is not set.\n"
                         "Add it to your .env file.\n")

ALLOWED_MODELS = {
    "openai:gpt-5.4-mini",
    "anthropic:claude-haiku-4-5",
    "google_genai:gemini-2.5-flash",
    "bedrock_converse/anthropic.claude-3-5-haiku-20241022-v1:0",
    "bedrock_converse/amazon.nova-lite-v1:0",
}

if MODEL not in ALLOWED_MODELS:
    allowed_list = "\n  ".join(sorted(ALLOWED_MODELS))
    raise SystemExit(
        f"\n[ERROR] MODEL='{MODEL}' is not allowed.\n"
        f"Set MODEL in your .env to one of the supported text-only models:\n  {allowed_list}\n"
    )

SYSTEM_PROMPT = (
    "You are an AI vision assistant. Follow these rules exactly:\n"
    "\n"
    "1. To identify objects: call detect_objects.\n"
    "\n"
    "2. To process the ENTIRE image (no specific object): call the tool WITHOUT label.\n"
    "   Examples:\n"
    "     rotate(angle=90)               ← rotate whole image\n"
    "     blur(radius=3)                 ← blur whole image\n"
    "     flip(direction='horizontal')   ← flip whole image\n"
    "     crop(x1=0, y1=0, x2=50, y2=100)  ← crop left half  (coordinates are PERCENTAGES 0-100)\n"
    "     crop(x1=50, y1=0, x2=100, y2=100) ← crop right half\n"
    "     crop(x1=0, y1=0, x2=100, y2=50)  ← crop top half\n"
    "\n"
    "3. To process SPECIFIC OBJECT(S): call the tool WITH label='<class>'.\n"
    "\n"
    "   Selection parameters:\n"
    "   - label:       required — object class, e.g. 'car', 'person', 'dog'\n"
    "   - indices:     0-based positions after sorting left-to-right.\n"
    "                  [0]=first  [1]=second  [0,2]=1st and 3rd  [0,1]=first two\n"
    "   - from_right:  True → sort right-to-left before applying indices\n"
    "   - all_objects: True → process ALL matching objects\n"
    "\n"
    "   Default (no indices, no all_objects): processes the LEFTMOST object.\n"
    "\n"
    "   Examples:\n"
    "     blur(radius=3, label='car')                                ← leftmost car\n"
    "     blur(radius=3, label='car', indices=[1])                   ← 2nd car from left\n"
    "     blur(radius=3, label='car', indices=[1], from_right=True)  ← 2nd car from right\n"
    "     blur(radius=3, label='person', indices=[0, 2])             ← 1st and 3rd person\n"
    "     blur(radius=3, label='person', indices=[0, 1])             ← first two persons\n"
    "     blur(radius=3, label='car', all_objects=True)              ← all cars\n"
    "\n"
    "4. NEVER call detect_objects before a processing tool — processing runs detection internally.\n"
    "\n"
    "5. NEVER call the same image tool more than once per turn to fix different objects.\n"
    "   Wrong: blur(label='car', indices=[0]) then blur(label='car', indices=[2])\n"
    "   Right: blur(label='car', indices=[0, 2]) — one call, both objects.\n"
    "\n"
    "6. NEVER call detect_objects after a processing tool — the processing result IS the output.\n"
    "   Calling detect_objects after processing replaces the processed image with a detection image.\n"
    "\n"
    "7. After every tool result, respond naturally to the user."
)

s3_client = boto3.client("s3", region_name=AWS_REGION)

_current_image_s3_key: ContextVar[Optional[str]] = ContextVar("current_image_s3_key", default=None)
_yolo_cache: ContextVar[Optional[dict]] = ContextVar("yolo_cache", default=None)

TOOLS: dict = {}


# ── Local tools ───────────────────────────────────────────────────────────────

@tool
def detect_objects() -> str:
    """Detect and identify objects in the image provided by the user using YOLO object detection."""
    s3_key = _current_image_s3_key.get()
    if not s3_key:
        return json.dumps({"error": "No image was provided by the user."})

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            f"{YOLO_SERVICE_URL}/predict",
            json={"image_s3_key": s3_key},
        )
        resp.raise_for_status()
        uid = resp.json()["prediction_uid"]
        det = client.get(f"{YOLO_SERVICE_URL}/prediction/{uid}")
        det.raise_for_status()
    return json.dumps(det.json())


TOOLS = {detect_objects.name: detect_objects}


# ── Server-side image helpers (never passed through the LLM) ──────────────────

def _fetch_full_image(s3_key: str) -> str:
    """Fetch image from S3, compress to ≤512 px, return base64 JPEG."""
    obj = s3_client.get_object(Bucket=AWS_S3_BUCKET, Key=s3_key)
    img = ImageOps.exif_transpose(PILImage.open(io.BytesIO(obj["Body"].read()))).convert("RGB")
    img.thumbnail((512, 512), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=60)
    return base64.b64encode(buf.getvalue()).decode()


def _get_all_regions(s3_key: str, label: str, from_right: bool = False) -> list[dict]:
    """
    Return all valid bounding boxes for *label*, sorted left-to-right by x1
    (reversed when from_right=True).
    Each entry: {"bbox": [x1,y1,x2,y2], "score": float}
    Malformed boxes are silently skipped.
    """
    cache = _yolo_cache.get()
    if cache and cache.get("s3_key") == s3_key:
        prediction = cache["prediction"]
        logging.info(f"_get_all_regions: reusing cached YOLO prediction, label='{label}'")
    else:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(f"{YOLO_SERVICE_URL}/predict", json={"image_s3_key": s3_key})
            resp.raise_for_status()
            uid = resp.json()["prediction_uid"]
            det = client.get(f"{YOLO_SERVICE_URL}/prediction/{uid}")
            det.raise_for_status()
        prediction = det.json()
        _yolo_cache.set({"s3_key": s3_key, "prediction": prediction})
        logging.info(f"_get_all_regions: YOLO call complete, uid={uid}, label='{label}'")

    regions = []
    for obj in prediction.get("detection_objects", []):
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


def _upload_image(b64: str) -> tuple[str, Optional[str]]:
    """
    Upload a base64-encoded image to S3.
    Returns (s3_key, presigned_url).  presigned_url is None on error.
    """
    ext = "png" if b64[:4] == "iVBO" else "jpg"
    ct  = "image/png" if ext == "png" else "image/jpeg"
    key = f"processed/{uuid.uuid4()}/result.{ext}"
    s3_client.put_object(
        Bucket=AWS_S3_BUCKET,
        Key=key,
        Body=base64.b64decode(b64),
        ContentType=ct,
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


# ── MCP image tool stubs — image_b64 injected server-side ────────────────────

class _ImgBase(BaseModel):
    label: Optional[str]        = Field(None,  description="Object class to target. Omit to process the entire image.")
    indices: Optional[list[int]] = Field(None, description="0-based indices of objects to process (left-to-right). E.g. [0]=first, [0,2]=1st & 3rd. Ignored when all_objects=True.")
    all_objects: bool            = Field(False, description="If True, process ALL detected objects with the given label.")
    from_right: bool             = Field(False, description="If True, sort objects right-to-left before applying indices.")

class _BlurInput(_ImgBase):
    radius: float = Field(2.0, description="Blur radius in pixels")

class _RotateInput(_ImgBase):
    angle: float = Field(90.0, description="Rotation angle in degrees counter-clockwise")

class _FlipInput(_ImgBase):
    direction: str = Field("horizontal", description="'horizontal' or 'vertical'")

class _ResizeInput(_ImgBase):
    width:  int = Field(256, description="Target width in pixels")
    height: int = Field(256, description="Target height in pixels")

class _CropInput(_ImgBase):
    x1: int = Field(0,   description="Left boundary as % of image width (0-100). e.g. left half → x1=0, x2=50")
    y1: int = Field(0,   description="Top boundary as % of image height (0-100). e.g. top half → y1=0, y2=50")
    x2: int = Field(100, description="Right boundary as % of image width (0-100). e.g. right half → x1=50, x2=100")
    y2: int = Field(100, description="Bottom boundary as % of image height (0-100). e.g. bottom half → y1=50, y2=100")

class _AddNoiseInput(_ImgBase):
    amount: float = Field(0.05, description="Fraction of pixels to corrupt (0.0–1.0)")


def _stub(**_) -> str:
    return ""


_MCP_STUB_TOOLS: dict = {
    "blur":      StructuredTool.from_function(_stub, name="blur",      description="Apply Gaussian blur. Omit label for whole image; set label to target objects.",        args_schema=_BlurInput),
    "rotate":    StructuredTool.from_function(_stub, name="rotate",    description="Rotate image. Omit label for whole image; set label to target objects.",               args_schema=_RotateInput),
    "flip":      StructuredTool.from_function(_stub, name="flip",      description="Flip image. Omit label for whole image; set label to target objects.",                 args_schema=_FlipInput),
    "resize":    StructuredTool.from_function(_stub, name="resize",    description="Resize image. Omit label for whole image; set label to target objects.",               args_schema=_ResizeInput),
    "crop":      StructuredTool.from_function(_stub, name="crop",      description="Crop full image to given coords (no label), or extract a specific object (with label).", args_schema=_CropInput),
    "add_noise": StructuredTool.from_function(_stub, name="add_noise", description="Add salt-and-pepper noise. Omit label for whole image; set label to target objects.", args_schema=_AddNoiseInput),
}

_MCP_IMAGE_TOOLS: set = set(_MCP_STUB_TOOLS.keys())
_MCP_TOOLS_IMPL:  dict = {}

TOOLS.update(_MCP_STUB_TOOLS)

# Bedrock on-demand default: ~50 RPM
rate_limiter = InMemoryRateLimiter(
    requests_per_second=50 / 60,
    check_every_n_seconds=0.1,
    max_bucket_size=5,
)

if "/" in MODEL:
    _provider, _model_id = MODEL.split("/", 1)
    llm = init_chat_model(_model_id, model_provider=_provider, temperature=0, rate_limiter=rate_limiter)
else:
    llm = init_chat_model(MODEL, temperature=0, rate_limiter=rate_limiter)

if llm.profile is not None:
    if llm.profile.get("tool_calling") is False:
        raise SystemExit(
            f"\n[ERROR] Model '{MODEL}' does not support tool calling.\n"
            "Set MODEL to a model that has tool_calling: true in its profile.\n"
        )

llm_with_tools = llm.bind_tools(list(TOOLS.values()))


# ── FastAPI lifespan — connect to MCP server ──────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global llm_with_tools
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        client = MultiServerMCPClient({
            "img-proc": {
                "url": f"{MCP_SERVER_URL}/mcp",
                "transport": "streamable_http",
            }
        })
        mcp_tools = await client.get_tools()
        for t in mcp_tools:
            if t.name in _MCP_IMAGE_TOOLS:
                _MCP_TOOLS_IMPL[t.name] = t
        logging.info(f"MCP tools loaded (streamable_http): {list(_MCP_TOOLS_IMPL.keys())}")
        yield
    except Exception as e:
        logging.warning(f"MCP server unavailable ({e}). Image processing tools disabled.")
        yield


# ── Agent models ──────────────────────────────────────────────────────────────

class TokensUsed(BaseModel):
    input: int
    output: int
    total: int


class ChatResponse(BaseModel):
    response: str
    prediction_id: Optional[str] = None
    processed_image_base64: Optional[str] = None
    processed_image_s3_key: Optional[str] = None
    processed_image_url: Optional[str] = None
    annotated_image: Optional[str] = None   # deprecated alias for processed_image_base64
    agent_loop_time_s: float
    iterations: int
    tools_called: list[str]
    context_limit_exceeded: bool
    tokens_used: TokensUsed


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tool_content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _is_base64_image(s: str) -> bool:
    if not isinstance(s, str) or len(s) < 100:
        return False
    return s.startswith(("iVBO", "/9j/"))


# ── Agent loop ────────────────────────────────────────────────────────────────

async def run_agent(history: list, max_iterations: int = 10) -> dict:
    messages              = [SystemMessage(content=SYSTEM_PROMPT)] + history
    iterations            = 0
    tools_called          = []
    prediction_id         = None
    annotated_image       = None
    processed_s3_key      = None
    processed_url         = None
    _processing_tool_ran  = False   # True once any MCP image tool sets a result
    context_limit_exceeded = False
    total_input_tokens    = 0
    total_output_tokens   = 0
    total_tokens          = 0
    start                 = time.time()

    def _clean(text: str) -> str:
        import re
        text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
        text = re.sub(r"<response>(.*?)</response>", r"\1", text, flags=re.DOTALL)
        return text.strip()

    def _ret(content: str) -> dict:
        return {
            "response":               content,
            "prediction_id":          prediction_id,
            "processed_image_base64": annotated_image,
            "processed_image_s3_key": processed_s3_key,
            "processed_image_url":    processed_url,
            "annotated_image":        annotated_image,
            "agent_loop_time_s":      round(time.time() - start, 3),
            "iterations":             iterations,
            "tools_called":           tools_called,
            "context_limit_exceeded": context_limit_exceeded,
            "tokens_used": TokensUsed(
                input=total_input_tokens,
                output=total_output_tokens,
                total=total_tokens,
            ),
        }

    while True:
        if iterations >= max_iterations:
            context_limit_exceeded = True
            messages.append(HumanMessage(
                content="You've reached the maximum number of steps. Give your best answer now."
            ))
            response = await llm_with_tools.ainvoke(messages)
            if response.usage_metadata:
                total_input_tokens  += response.usage_metadata.get("input_tokens", 0)
                total_output_tokens += response.usage_metadata.get("output_tokens", 0)
                total_tokens        += response.usage_metadata.get("total_tokens", 0)
            content = response.content
            if isinstance(content, list):
                content = "".join(b["text"] for b in content if b.get("type") == "text")
            content = _clean(content)
            break

        response: AIMessage = await llm_with_tools.ainvoke(messages)
        if response.usage_metadata:
            total_input_tokens  += response.usage_metadata.get("input_tokens", 0)
            total_output_tokens += response.usage_metadata.get("output_tokens", 0)
            total_tokens        += response.usage_metadata.get("total_tokens", 0)
        messages.append(response)
        iterations += 1

        if not response.tool_calls:
            content = response.content
            if isinstance(content, list):
                content = "".join(b["text"] for b in content if b.get("type") == "text")
            return _ret(_clean(content))

        # If any MCP tool in this turn operates on the whole image (no label),
        # detect_objects is pointless — the whole-image path never uses YOLO.
        has_whole_image_tool = any(
            tc["name"] in _MCP_IMAGE_TOOLS and not tc.get("args", {}).get("label")
            for tc in response.tool_calls
        )

        # Accumulates edits across multiple tool calls in the same turn.
        # Each object-specific tool pastes onto this instead of the original S3 image,
        # so effects from previous tools in the same turn are preserved.
        current_composite: Optional[PILImage.Image] = None

        # Capture original key once — YOLO detection always runs on the original
        # image so bounding boxes stay consistent even after the first tool edits it.
        turn_s3_key = _current_image_s3_key.get()

        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            tools_called.append(tool_name)
            tool_id   = tool_call["id"]
            logging.info(f"Tool call: {tool_name}({tool_call.get('args', {})})")

            if tool_name == "detect_objects" and has_whole_image_tool:
                logging.info("Skipping detect_objects — whole-image processing tool in same turn")
                messages.append(ToolMessage(
                    content="Detection skipped — not needed for whole-image processing.",
                    tool_call_id=tool_id,
                ))
                continue

            # ── MCP image tools ───────────────────────────────────────────────
            if tool_name in _MCP_IMAGE_TOOLS:
                s3_key    = _current_image_s3_key.get()
                real_tool = _MCP_TOOLS_IMPL.get(tool_name)

                if not s3_key:
                    messages.append(ToolMessage(
                        content="No image was provided. Please upload an image first.",
                        tool_call_id=tool_id,
                    ))
                    continue

                if not real_tool:
                    messages.append(ToolMessage(
                        content=f"Image processing tool '{tool_name}' is currently unavailable.",
                        tool_call_id=tool_id,
                    ))
                    continue

                args        = dict(tool_call.get("args", {}))
                label       = args.pop("label",       None)
                indices     = args.pop("indices",     None)
                all_objects = bool(args.pop("all_objects", False))
                from_right  = bool(args.pop("from_right",  False))

                # ── Whole-image path ──────────────────────────────────────────
                if not label:
                    img_b64 = _fetch_full_image(s3_key)
                    if tool_name == "crop":
                        _pil = PILImage.open(io.BytesIO(base64.b64decode(img_b64)))
                        _w, _h = _pil.size
                        args["x1"] = int(args.get("x1", 0)   * _w / 100)
                        args["y1"] = int(args.get("y1", 0)   * _h / 100)
                        args["x2"] = int(args.get("x2", 100) * _w / 100)
                        args["y2"] = int(args.get("y2", 100) * _h / 100)
                    args["image_b64"] = img_b64
                    mcp_result  = await real_tool.ainvoke(
                        {"name": tool_name, "args": args, "id": tool_id, "type": "tool_call"}
                    )
                    result_text = _tool_content_text(mcp_result.content)

                    if _is_base64_image(result_text):
                        annotated_image              = result_text
                        processed_s3_key, processed_url = _upload_image(annotated_image)
                        _current_image_s3_key.set(processed_s3_key)
                        turn_s3_key = processed_s3_key  # subsequent object tools detect on this transformed image
                        _processing_tool_ran         = True
                        logging.info(f"Updated current image to processed result: {processed_s3_key}")
                        messages.append(ToolMessage(content="Image processed successfully.", tool_call_id=tool_id))
                    else:
                        messages.append(ToolMessage(content=result_text, tool_call_id=tool_id))
                    continue

                # ── Object-specific path ──────────────────────────────────────
                try:
                    regions = _get_all_regions(turn_s3_key, label, from_right)
                except Exception as exc:
                    messages.append(ToolMessage(
                        content=f"YOLO detection failed: {exc}",
                        tool_call_id=tool_id,
                    ))
                    continue

                if not regions:
                    messages.append(ToolMessage(
                        content=f"I could not find any '{label}' in the image.",
                        tool_call_id=tool_id,
                    ))
                    continue

                n = len(regions)

                # Resolve selected indices
                if all_objects:
                    selected = list(range(n))
                elif indices:
                    bad = [i for i in indices if not (-n <= i < n)]
                    if bad:
                        messages.append(ToolMessage(
                            content=(
                                f"Index error: only {n} '{label}' found. "
                                f"Invalid indices: {bad}. Valid range: 0–{n-1}."
                            ),
                            tool_call_id=tool_id,
                        ))
                        continue
                    selected = [i % n for i in indices]
                else:
                    selected = [0]  # default: leftmost

                # ── Crop + label: extract region, no composite ────────────────
                if tool_name == "crop":
                    region        = regions[selected[0]]
                    x1, y1, x2, y2 = region["bbox"]
                    if current_composite is not None:
                        orig = current_composite.copy()
                    else:
                        s3_obj = s3_client.get_object(Bucket=AWS_S3_BUCKET, Key=s3_key)
                        orig   = ImageOps.exif_transpose(PILImage.open(io.BytesIO(s3_obj["Body"].read()))).convert("RGB")
                    crop_pil = orig.crop((x1, y1, x2, y2))
                    buf = io.BytesIO()
                    crop_pil.save(buf, format="JPEG", quality=75)
                    annotated_image              = base64.b64encode(buf.getvalue()).decode()
                    processed_s3_key, processed_url = _upload_image(annotated_image)
                    note = f" (first of {len(selected)} selected)" if len(selected) > 1 else ""
                    messages.append(ToolMessage(
                        content=f"'{label}' region extracted{note}.",
                        tool_call_id=tool_id,
                    ))
                    continue

                # ── All other tools: crop → MCP → composite ───────────────────
                if current_composite is not None:
                    orig = current_composite.copy()
                    logging.info("Using previous tool composite as base")
                else:
                    s3_obj = s3_client.get_object(Bucket=AWS_S3_BUCKET, Key=s3_key)
                    orig   = PILImage.open(io.BytesIO(s3_obj["Body"].read())).convert("RGB")

                processed_count = 0
                errors: list[str] = []

                for sel_idx in selected:
                    region        = regions[sel_idx]
                    x1, y1, x2, y2 = region["bbox"]
                    bw, bh = x2 - x1, y2 - y1
                    logging.info(
                        f"  [{label}][{sel_idx}] bbox=[{x1},{y1},{x2},{y2}] "
                        f"crop={bw}x{bh} score={region.get('score', '?'):.2f}"
                    )

                    # Crop from the running composite (accumulates edits)
                    cropped = orig.crop((x1, y1, x2, y2))
                    buf = io.BytesIO()
                    cropped.save(buf, format="JPEG", quality=75)
                    img_b64 = base64.b64encode(buf.getvalue()).decode()

                    tool_args = {**args, "image_b64": img_b64}
                    try:
                        mcp_result  = await real_tool.ainvoke(
                            {"name": tool_name, "args": tool_args, "id": tool_id, "type": "tool_call"}
                        )
                        result_text = _tool_content_text(mcp_result.content)
                        if _is_base64_image(result_text):
                            processed = PILImage.open(
                                io.BytesIO(base64.b64decode(result_text))
                            ).convert("RGB")
                            if tool_name == "resize":
                                # For resize: show actual size change by sampling background,
                                # clearing the bbox, then centering the resized result in it.
                                border_pixels = []
                                orig_rgb = orig.load()
                                for bx in range(x1, x2):
                                    if y1 > 0:
                                        border_pixels.append(orig_rgb[bx, y1 - 1])
                                    if y2 < orig.height:
                                        border_pixels.append(orig_rgb[bx, y2])
                                for by in range(y1, y2):
                                    if x1 > 0:
                                        border_pixels.append(orig_rgb[x1 - 1, by])
                                    if x2 < orig.width:
                                        border_pixels.append(orig_rgb[x2, by])
                                if border_pixels:
                                    avg_color = tuple(
                                        int(sum(c[i] for c in border_pixels) / len(border_pixels))
                                        for i in range(3)
                                    )
                                else:
                                    avg_color = (128, 128, 128)
                                ImageDraw.Draw(orig).rectangle([x1, y1, x2 - 1, y2 - 1], fill=avg_color)
                                pw, ph = processed.size
                                cx = x1 + (bw - pw) // 2
                                cy = y1 + (bh - ph) // 2
                                # Clamp to canvas bounds
                                cx = max(0, min(cx, orig.width  - pw))
                                cy = max(0, min(cy, orig.height - ph))
                                orig.paste(processed, (cx, cy))
                                logging.info(
                                    f"    → resize: cleared {bw}x{bh} bbox, pasted "
                                    f"{pw}x{ph} at ({cx},{cy}) ✓"
                                )
                            else:
                                # All other tools: force-fit processed result back into bbox
                                orig.paste(
                                    processed.resize((bw, bh), PILImage.LANCZOS),
                                    (x1, y1),
                                )
                                logging.info(f"    → pasted {bw}x{bh} at ({x1},{y1}) ✓")
                            processed_count += 1
                        else:
                            logging.warning(
                                f"    → MCP returned non-image (len={len(result_text)}, "
                                f"preview={result_text[:60]!r})"
                            )
                            errors.append(f"{label}[{sel_idx}]: unexpected MCP response")
                    except Exception as exc:
                        logging.warning(f"    → MCP exception for {label}[{sel_idx}]: {exc}")
                        errors.append(f"{label}[{sel_idx}]: {exc}")

                if processed_count == 0:
                    messages.append(ToolMessage(
                        content=f"Processing failed for all selected objects. {'; '.join(errors)}",
                        tool_call_id=tool_id,
                    ))
                    continue

                current_composite = orig.copy()  # full-res base for next tool in this turn
                orig.thumbnail((768, 768), PILImage.LANCZOS)
                buf = io.BytesIO()
                orig.save(buf, format="JPEG", quality=70)
                annotated_image              = base64.b64encode(buf.getvalue()).decode()
                processed_s3_key, processed_url = _upload_image(annotated_image)
                _current_image_s3_key.set(processed_s3_key)
                _processing_tool_ran         = True

                confirm = f"Processed {processed_count}/{len(selected)} {label}(s) successfully."
                if errors:
                    confirm += f" Note: {'; '.join(errors)}."
                messages.append(ToolMessage(content=confirm, tool_call_id=tool_id))

            # ── Local tools (detect_objects) ──────────────────────────────────
            else:
                tool_fn = TOOLS.get(tool_name)
                if not tool_fn:
                    messages.append(ToolMessage(
                        content=f"Unknown tool '{tool_name}'.",
                        tool_call_id=tool_id,
                    ))
                    continue

                tool_result  = await tool_fn.ainvoke(tool_call)
                messages.append(tool_result)

                content_text = _tool_content_text(tool_result.content)
                try:
                    result_data = json.loads(content_text)
                    if "prediction_uid" in result_data:
                        prediction_id = result_data["prediction_uid"]
                    s3_key_now = _current_image_s3_key.get()
                    if s3_key_now and "detection_objects" in result_data:
                        _yolo_cache.set({"s3_key": s3_key_now, "prediction": result_data})
                        logging.info(
                            f"YOLO cache populated: {len(result_data['detection_objects'])} objects"
                        )
                    pred_key = (result_data.get("predicted_image_s3_key")
                                or result_data.get("predicted_image"))
                    if pred_key and not _processing_tool_ran:
                        obj = s3_client.get_object(Bucket=AWS_S3_BUCKET, Key=pred_key)
                        annotated_image  = base64.b64encode(obj["Body"].read()).decode("utf-8")
                        processed_s3_key = pred_key
                        try:
                            processed_url = s3_client.generate_presigned_url(
                                "get_object",
                                Params={"Bucket": AWS_S3_BUCKET, "Key": pred_key},
                                ExpiresIn=3600,
                            )
                        except Exception:
                            processed_url = None
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

    return _ret(content)


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Vision Agent", lifespan=lifespan)

from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://dev.talal.fursa.click:3000",
        "http://prod.talal.fursa.click:3000",
    ],
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)


class ChatMessage(BaseModel):
    role: str
    content: str
    image_base64: Optional[str] = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    lc_messages  = []
    image_s3_key = None

    for msg in request.messages:
        if msg.role == "user":
            if msg.image_base64:
                try:
                    image_bytes = base64.b64decode(msg.image_base64)
                except Exception:
                    raise HTTPException(
                        status_code=400,
                        detail="The uploaded image could not be decoded. Please send a valid base64-encoded image.",
                    )
                try:
                    _img = PILImage.open(io.BytesIO(image_bytes))
                    size_hint = f" ({_img.width}x{_img.height} px)"
                except Exception:
                    size_hint = ""
                image_s3_key = f"images/{uuid.uuid4()}/original.jpg"
                s3_client.put_object(
                    Bucket=AWS_S3_BUCKET,
                    Key=image_s3_key,
                    Body=image_bytes,
                    ContentType="image/jpeg",
                )
                content = msg.content + f"\n[An image was uploaded{size_hint}. Use existing tools to analyze it according to user instructions.]"
            else:
                content = msg.content
            lc_messages.append(HumanMessage(content=content))
        else:
            lc_messages.append(AIMessage(content=msg.content))

    token = _current_image_s3_key.set(image_s3_key)
    try:
        return ChatResponse(**await run_agent(lc_messages))
    finally:
        _current_image_s3_key.reset(token)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
