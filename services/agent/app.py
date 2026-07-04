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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_core.tools import tool
from PIL import Image as PILImage
from pydantic import BaseModel

YOLO_SERVICE_URL  = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
MCP_SERVER_URL    = os.environ.get("MCP_SERVER_URL", "http://localhost:8000")
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
    "You are an AI vision assistant. You help users understand and analyze images. "
    "Use the available tools to extract information from images and apply image processing. "
    "For operations on the entire image, use get_image_from_s3 first, then pass the result to the image tool. "
    "For operations on a specific object (e.g. 'blur the second dog'), use get_object_region first to "
    "extract the object, then apply the image processing tool to the returned base64 image. "
    "When order matters (e.g. 'from the right'), pass order='right_to_left' to get_object_region."
)

s3_client = boto3.client("s3", region_name=AWS_REGION)

_current_image_s3_key: ContextVar[Optional[str]] = ContextVar("current_image_s3_key", default=None)

# Tools the agent can call. MCP tools are added at startup via lifespan.
TOOLS: dict = {}


# ── Local tools ───────────────────────────────────────────────────────────────

@tool
def detect_objects() -> str:
    """Detect and identify objects in the image provided by the user using YOLO object detection."""
    s3_key = _current_image_s3_key.get()
    if not s3_key:
        return json.dumps({"error": "No image was provided by the user."})

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{YOLO_SERVICE_URL}/predict",
            json={"image_s3_key": s3_key},
        )
        response.raise_for_status()
    return json.dumps(response.json())


@tool
def get_image_from_s3() -> str:
    """
    Download the current user image from S3 as a base64 PNG string.
    Use this before calling image processing tools (blur, rotate, flip, resize, crop, add_noise)
    when you need to process the entire image.
    Returns a base64 PNG string on success, or a JSON error string on failure.
    """
    s3_key = _current_image_s3_key.get()
    if not s3_key:
        return json.dumps({"error": "No image was provided by the user."})
    try:
        obj = s3_client.get_object(Bucket=AWS_S3_BUCKET, Key=s3_key)
        img = PILImage.open(io.BytesIO(obj["Body"].read())).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_object_region(label: str, index: int = 0, order: str = "left_to_right") -> str:
    """
    Extract a specific detected object from the image as a base64 PNG string.
    Use this before applying image processing to a specific object
    (e.g. 'blur the second dog', 'add noise to the car').

    Args:
        label: Object type to find, e.g. 'dog', 'car', 'person'.
        index: Which instance to pick after sorting (0=first, 1=second, etc.).
        order: Sort direction — 'left_to_right' or 'right_to_left'.

    Returns a base64 PNG string of the cropped object region,
    or a JSON error string if not found.
    """
    s3_key = _current_image_s3_key.get()
    if not s3_key:
        return json.dumps({"error": "No image was provided by the user."})

    # Run YOLO detection
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{YOLO_SERVICE_URL}/predict",
                json={"image_s3_key": s3_key},
            )
            resp.raise_for_status()
        prediction = resp.json()
    except Exception as e:
        return json.dumps({"error": f"YOLO detection failed: {e}"})

    # Filter by label
    matches = [o for o in prediction.get("detection_objects", []) if o["label"] == label]
    if not matches:
        available = list({o["label"] for o in prediction.get("detection_objects", [])})
        return json.dumps({"error": f"No '{label}' detected. Found: {available}"})

    # Sort by x1 position
    matches.sort(
        key=lambda o: ast.literal_eval(o["box"])[0],
        reverse=(order == "right_to_left"),
    )

    if index >= len(matches):
        return json.dumps({"error": f"Only {len(matches)} '{label}' found, cannot pick index {index}."})

    # Crop the bounding box region
    box = ast.literal_eval(matches[index]["box"])
    x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    try:
        obj = s3_client.get_object(Bucket=AWS_S3_BUCKET, Key=s3_key)
        img = PILImage.open(io.BytesIO(obj["Body"].read()))
        cropped = img.crop((x1, y1, x2, y2))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        return json.dumps({"error": str(e)})


TOOLS = {
    detect_objects.name:    detect_objects,
    get_image_from_s3.name: get_image_from_s3,
    get_object_region.name: get_object_region,
}

# Bedrock on-demand default: ~50 RPM for Claude models.
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

# Names of MCP image-processing tools — their raw base64 return value
# is captured as the annotated_image shown to the user.
_MCP_IMAGE_TOOLS = {"blur", "rotate", "flip", "resize", "crop", "add_noise"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tool_content_text(content) -> str:
    """Extract plain text from a ToolMessage content (str or list of blocks)."""
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
    """Return True if s looks like a base64-encoded PNG or JPEG."""
    if not isinstance(s, str) or len(s) < 100:
        return False
    return s.startswith(("iVBO", "/9j/"))   # PNG or JPEG magic bytes in base64


# ── FastAPI lifespan — connect to MCP server ──────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global llm_with_tools
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        client = MultiServerMCPClient({
            "img-proc": {
                "url": f"{MCP_SERVER_URL}/sse",
                "transport": "sse",
            }
        })
        mcp_tools = await client.get_tools()
        for t in mcp_tools:
            TOOLS[t.name] = t
        llm_with_tools = llm.bind_tools(list(TOOLS.values()))
        logging.info(f"MCP tools loaded: {[t.name for t in mcp_tools]}")
        yield
    except Exception as e:
        logging.warning(f"MCP server unavailable ({e}). Image processing tools disabled.")
        yield


# ── Agent loop ────────────────────────────────────────────────────────────────

class TokensUsed(BaseModel):
    input: int
    output: int
    total: int


class ChatResponse(BaseModel):
    response: str
    prediction_id: Optional[str] = None
    annotated_image: Optional[str] = None
    agent_loop_time_s: float
    iterations: int
    tools_called: list[str]
    context_limit_exceeded: bool
    tokens_used: TokensUsed


async def run_agent(history: list, max_iterations: int = 10) -> dict:
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + history
    iterations = 0
    tools_called = []
    prediction_id = None
    annotated_image = None
    context_limit_exceeded = False
    total_input_tokens = 0
    total_output_tokens = 0
    total_tokens = 0
    start = time.time()

    while True:
        if iterations >= max_iterations:
            context_limit_exceeded = True
            messages.append(HumanMessage(
                content="You've reached the maximum number of steps. Based on everything gathered so far, give your best answer now."
            ))
            response = await llm_with_tools.ainvoke(messages)
            if response.usage_metadata:
                total_input_tokens  += response.usage_metadata.get("input_tokens", 0)
                total_output_tokens += response.usage_metadata.get("output_tokens", 0)
                total_tokens        += response.usage_metadata.get("total_tokens", 0)
            content = response.content
            if isinstance(content, list):
                content = "".join(block["text"] for block in content if block.get("type") == "text")
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
                content = "".join(block["text"] for block in content if block.get("type") == "text")
            return {
                "response": content,
                "prediction_id": prediction_id,
                "annotated_image": annotated_image,
                "agent_loop_time_s": round(time.time() - start, 3),
                "iterations": iterations,
                "tools_called": tools_called,
                "context_limit_exceeded": context_limit_exceeded,
                "tokens_used": TokensUsed(
                    input=total_input_tokens,
                    output=total_output_tokens,
                    total=total_tokens,
                ),
            }

        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            tools_called.append(tool_name)
            tool_fn = TOOLS[tool_name]
            tool_result = await tool_fn.ainvoke(tool_call)
            messages.append(tool_result)

            content_text = _tool_content_text(tool_result.content)

            # Extract prediction info from YOLO results
            try:
                result_data = json.loads(content_text)
                if "prediction_uid" in result_data:
                    prediction_id = result_data["prediction_uid"]
                if "predicted_image_s3_key" in result_data:
                    obj = s3_client.get_object(Bucket=AWS_S3_BUCKET, Key=result_data["predicted_image_s3_key"])
                    annotated_image = base64.b64encode(obj["Body"].read()).decode("utf-8")
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

            # Capture processed image returned by MCP image tools
            if tool_name in _MCP_IMAGE_TOOLS and _is_base64_image(content_text):
                annotated_image = content_text

    return {
        "response": content,
        "prediction_id": prediction_id,
        "annotated_image": annotated_image,
        "agent_loop_time_s": round(time.time() - start, 3),
        "iterations": iterations,
        "tools_called": tools_called,
        "context_limit_exceeded": context_limit_exceeded,
        "tokens_used": TokensUsed(
            input=total_input_tokens,
            output=total_output_tokens,
            total=total_tokens,
        ),
    }


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
    lc_messages = []
    image_s3_key = None

    for msg in request.messages:
        if msg.role == "user":
            if msg.image_base64:
                image_bytes = base64.b64decode(msg.image_base64)
                image_s3_key = f"images/{uuid.uuid4()}/original.jpg"
                s3_client.put_object(
                    Bucket=AWS_S3_BUCKET,
                    Key=image_s3_key,
                    Body=image_bytes,
                    ContentType="image/jpeg",
                )
                content = msg.content + "\n[An image was uploaded. Use existing tools to analyze it according to user instructions.]"
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
