import base64
import io
import json
import logging
import os
from contextvars import ContextVar
import time
from typing import Optional

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
from langchain_core.tools import tool
from pydantic import BaseModel

YOLO_SERVICE_URL = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
MODEL = os.environ.get("MODEL")

# Text-only models
ALLOWED_MODELS = {
    "openai:gpt-5.4-mini",
    "anthropic:claude-haiku-4-5",
    "google_genai:gemini-2.5-flash"
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

_current_image_b64: ContextVar[Optional[str]] = ContextVar("current_image_b64", default=None)

@tool
def detect_objects() -> str:
    """Detect and identify objects in the image provided by the user using YOLO object detection."""
    image_b64 = _current_image_b64.get()
    if not image_b64:
        return json.dumps({"error": "No image was provided by the user."})

    image_bytes = base64.b64decode(image_b64)
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{YOLO_SERVICE_URL}/predict",
            files={"file": ("image.jpg", io.BytesIO(image_bytes), "image/jpeg")},
        )
        response.raise_for_status()
    return json.dumps(response.json())


# Registry: map tool name -> tool function
TOOLS = {
    detect_objects.name: detect_objects
}

llm = init_chat_model(MODEL, temperature=0)

if llm.profile is not None:
    if not llm.profile.get("tool_calling"):
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
                    with httpx.Client(timeout=30.0) as client:
                        img_resp = client.get(f"{YOLO_SERVICE_URL}/prediction/{prediction_id}/image")
                        if img_resp.status_code == 200:
                            annotated_image = base64.b64encode(img_resp.content).decode("utf-8")
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
    latest_image = None

    for msg in request.messages:
        if msg.role == "user":
            if msg.image_base64:
                latest_image = msg.image_base64
                content = msg.content + "\n[An image was uploaded. Use existing tools to analyze it according to user instructions.]"
            else:
                content = msg.content
            lc_messages.append(HumanMessage(content=content))
        else:
            lc_messages.append(AIMessage(content=msg.content))

    token = _current_image_b64.set(latest_image)
    try:
        return ChatResponse(**run_agent(lc_messages))
    finally:
        _current_image_b64.reset(token)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
