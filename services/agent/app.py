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
from langchain_core.tools import tool
from PIL import Image as PILImage
from pydantic import BaseModel

YOLO_SERVICE_URL = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
MCP_SERVER_URL   = os.environ.get("MCP_SERVER_URL", "http://localhost:9000")
MODEL            = os.environ.get("MODEL")
AWS_REGION       = os.environ.get("AWS_REGION")
AWS_S3_BUCKET    = os.environ.get("AWS_S3_BUCKET")

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
    "7. After every tool result, respond naturally to the user.\n"
    "\n"
    "IMPORTANT: Never provide image_s3_key or detection_s3_key — these are injected automatically."
)

s3_client = boto3.client("s3", region_name=AWS_REGION)

_current_image_s3_key: ContextVar[Optional[str]] = ContextVar("current_image_s3_key", default=None)

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

_MCP_TOOLS_IMPL: dict = {}


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
            _MCP_TOOLS_IMPL[t.name] = t
            TOOLS[t.name] = t
        llm_with_tools = llm.bind_tools(list(TOOLS.values()))
        logging.info(f"MCP tools loaded: {list(_MCP_TOOLS_IMPL.keys())}")
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


# ── Agent loop ────────────────────────────────────────────────────────────────

async def run_agent(history: list, max_iterations: int = 10) -> dict:
    messages              = [SystemMessage(content=SYSTEM_PROMPT)] + history
    iterations            = 0
    tools_called          = []
    prediction_id         = None
    annotated_image       = None
    processed_s3_key      = None
    processed_url         = None
    _processing_tool_ran  = False
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

        # Capture once per turn — YOLO always detects on the original image so
        # that heavy edits (e.g. 90% noise) on earlier objects don't shift the
        # bounding-box indices for later tool calls in the same turn.
        turn_detection_s3_key = _current_image_s3_key.get()

        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            tools_called.append(tool_name)
            tool_id   = tool_call["id"]
            logging.info(f"Tool call: {tool_name}({tool_call.get('args', {})})")

            # ── MCP image tools ───────────────────────────────────────────────
            if tool_name in _MCP_TOOLS_IMPL:
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

                args = dict(tool_call.get("args", {}))
                args["image_s3_key"]       = s3_key               # current (possibly edited) image
                args["detection_s3_key"]   = turn_detection_s3_key  # original image for YOLO

                mcp_result  = await real_tool.ainvoke(
                    {"name": tool_name, "args": args, "id": tool_id, "type": "tool_call"}
                )
                result_text = _tool_content_text(mcp_result.content)

                try:
                    result_data = json.loads(result_text)
                except (json.JSONDecodeError, TypeError):
                    result_data = {}

                if "error" in result_data:
                    messages.append(ToolMessage(
                        content=result_data["error"],
                        tool_call_id=tool_id,
                    ))
                    continue

                if "processed_image_s3_key" in result_data:
                    processed_s3_key = result_data["processed_image_s3_key"]
                    processed_url    = result_data.get("processed_image_url")
                    annotated_image  = result_data.get("processed_image_base64")
                    _current_image_s3_key.set(processed_s3_key)
                    _processing_tool_ran = True
                    logging.info(f"Updated current image to: {processed_s3_key}")

                messages.append(ToolMessage(
                    content="Image processed successfully.",
                    tool_call_id=tool_id,
                ))

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
    processed_image_s3_key: Optional[str] = None   # sent back by frontend to restore last processed image


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
            # Restore the last processed S3 key from conversation history so
            # the next turn continues from the edited image, not the original.
            if msg.processed_image_s3_key:
                image_s3_key = msg.processed_image_s3_key
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
