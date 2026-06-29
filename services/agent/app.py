import base64
import json
import logging
import os
from contextvars import ContextVar
import time
from typing import Optional
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
from pydantic import BaseModel

YOLO_SERVICE_URL = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
MODEL         = os.environ.get("MODEL")
AWS_REGION    = os.environ.get("AWS_REGION")
AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET")

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
    "Use the available tools to extract information from images. "
)

s3_client = boto3.client("s3", region_name=AWS_REGION)

_current_image_s3_key: ContextVar[Optional[str]] = ContextVar("current_image_s3_key", default=None)

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


# Registry: map tool name -> tool function
TOOLS = {
    detect_objects.name: detect_objects
}

# Bedrock on-demand default: ~50 RPM for Claude models (check AWS Service Quotas for your account).
# 50 requests/min = 50/60 ≈ 0.833 requests/second
rate_limiter = InMemoryRateLimiter(
    requests_per_second=50 / 60,
    check_every_n_seconds=0.1,
    max_bucket_size=5,
)

# Bedrock models use "provider/model-id" format (e.g. bedrock_converse/anthropic.claude-...).
# init_chat_model only recognises "provider:model" with a colon, but Bedrock model IDs
# already contain colons, so we split on "/" ourselves and pass model_provider explicitly.
if "/" in MODEL:
    _provider, _model_id = MODEL.split("/", 1)
    llm = init_chat_model(_model_id, model_provider=_provider, temperature=0, rate_limiter=rate_limiter)
else:
    llm = init_chat_model(MODEL, temperature=0, rate_limiter=rate_limiter)

if llm.profile is not None:
    # profile.get("tool_calling") returns None when the key is absent (e.g. Bedrock models
    # whose profile doesn't include this field). Only reject on an explicit False.
    if llm.profile.get("tool_calling") is False:
        raise SystemExit(
            f"\n[ERROR] Model '{MODEL}' does not support tool calling.\n"
            "Set MODEL to a model that has tool_calling: true in its profile.\n"
        )

llm_with_tools = llm.bind_tools(list(TOOLS.values()))


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



def run_agent(history: list, max_iterations: int = 10) -> dict:
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
            response = llm_with_tools.invoke(messages)
            if response.usage_metadata:
                total_input_tokens  += response.usage_metadata.get("input_tokens", 0)
                total_output_tokens += response.usage_metadata.get("output_tokens", 0)
                total_tokens        += response.usage_metadata.get("total_tokens", 0)
            content = response.content
            if isinstance(content, list):
                content = "".join(block["text"] for block in content if block.get("type") == "text")
            break

        response: AIMessage = llm_with_tools.invoke(messages)
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
            tools_called.append(tool_call["name"])
            tool_fn = TOOLS[tool_call["name"]]
            tool_result = tool_fn.invoke(tool_call)
            messages.append(tool_result)

            try:
                result_data = json.loads(tool_result.content)
                if "prediction_uid" in result_data:
                    prediction_id = result_data["prediction_uid"]
                if "predicted_image_s3_key" in result_data:
                    obj = s3_client.get_object(Bucket=AWS_S3_BUCKET, Key=result_data["predicted_image_s3_key"])
                    annotated_image = base64.b64encode(obj["Body"].read()).decode("utf-8")
            except Exception:
                pass

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


app = FastAPI(title="Vision Agent")

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
    role: str                           # "user" or "assistant"
    content: str
    image_base64: Optional[str] = None  # only on user messages that carry an image


class ChatRequest(BaseModel):
    messages: list[ChatMessage]         # full conversation thread, oldest first


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
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
        return ChatResponse(**run_agent(lc_messages))
    finally:
        _current_image_s3_key.reset(token)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
