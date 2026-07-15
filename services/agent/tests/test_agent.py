"""
Tests for the simplified agent after refactor.

The agent now only:
 - injects image_s3_key into MCP tool calls
 - parses the JSON result dict from MCP
 - updates _current_image_s3_key from the result
 - forwards errors from MCP to the LLM as ToolMessages

MCP now owns all image processing logic (YOLO, S3, compositing).
"""

import base64
import io
import json
import os

import pytest

os.environ.setdefault("MODEL", "bedrock_converse/amazon.nova-lite-v1:0")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_S3_BUCKET", "fake-bucket")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "fake")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "fake")

from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage
from PIL import Image as PILImage

from app import _current_image_s3_key, run_agent


# ── Test helpers ──────────────────────────────────────────────────────────────

def _png_b64(w: int = 20, h: int = 20, color: str = "red") -> str:
    img = PILImage.new("RGB", (w, h), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _jpeg_bytes(w: int = 100, h: int = 100, color: str = "blue") -> bytes:
    img = PILImage.new("RGB", (w, h), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _ai(content: str = "", *, tool_calls=None, input_tokens: int = 10, output_tokens: int = 5):
    kwargs = {
        "content": content,
        "usage_metadata": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    if tool_calls:
        kwargs["tool_calls"] = tool_calls
    return AIMessage(**kwargs)


def _tc(name: str, args: dict, call_id: str = "call_1") -> list:
    return [{"name": name, "args": args, "id": call_id, "type": "tool_call"}]


def _mcp_impl(
    s3_key:  str = "processed/fake/result.jpg",
    url:     str = "https://s3.example.com/fake",
    b64:     str | None = None,
    error:   str | None = None,
):
    """Mock MCP tool that returns a processed image result JSON string."""
    if error:
        content = json.dumps({"error": error})
    else:
        content = json.dumps({
            "processed_image_s3_key": s3_key,
            "processed_image_url":    url,
            "processed_image_base64": b64 or _png_b64(),
        })
    mock = MagicMock()
    mock.ainvoke = AsyncMock(return_value=MagicMock(content=content))
    return mock


# ── Core agent loop ───────────────────────────────────────────────────────────

async def test_run_agent_returns_content():
    with patch("app.llm_with_tools") as m:
        m.ainvoke = AsyncMock(return_value=_ai("Hello there!"))
        result = await run_agent([HumanMessage(content="Hi!")])
    assert result["response"] == "Hello there!"


async def test_run_agent_one_iteration_no_tools():
    with patch("app.llm_with_tools") as m:
        m.ainvoke = AsyncMock(return_value=_ai("Done."))
        result = await run_agent([HumanMessage(content="Hi!")])
    assert result["iterations"] == 1
    assert result["tools_called"] == []
    assert result["context_limit_exceeded"] is False


async def test_run_agent_token_counts():
    with patch("app.llm_with_tools") as m:
        m.ainvoke = AsyncMock(return_value=_ai("Done.", input_tokens=10, output_tokens=5))
        result = await run_agent([HumanMessage(content="Hi!")])
    assert result["tokens_used"].input == 10
    assert result["tokens_used"].output == 5
    assert result["tokens_used"].total == 15


async def test_run_agent_accumulates_tokens():
    responses = [
        _ai("", tool_calls=_tc("detect_objects", {}), input_tokens=20, output_tokens=3),
        _ai("Done.", input_tokens=30, output_tokens=8),
    ]
    with patch("app.llm_with_tools") as m:
        m.ainvoke = AsyncMock(side_effect=responses)
        result = await run_agent([HumanMessage(content="Go!")])
    assert result["tokens_used"].input == 50
    assert result["tokens_used"].output == 11
    assert result["tokens_used"].total == 61


async def test_run_agent_context_limit_exceeded():
    looping = _ai("", tool_calls=_tc("detect_objects", {}))
    final   = _ai("Giving up.")
    with patch("app.llm_with_tools") as m:
        m.ainvoke = AsyncMock(side_effect=[looping, looping, final])
        result = await run_agent([HumanMessage(content="Loop")], max_iterations=2)
    assert result["context_limit_exceeded"] is True
    assert result["response"] == "Giving up."


async def test_run_agent_executes_detect_objects():
    responses = [
        _ai("", tool_calls=_tc("detect_objects", {}), input_tokens=20, output_tokens=3),
        _ai("No objects detected.", input_tokens=30, output_tokens=8),
    ]
    with patch("app.llm_with_tools") as m:
        m.ainvoke = AsyncMock(side_effect=responses)
        result = await run_agent([HumanMessage(content="What is in this image?")])
    assert result["tools_called"] == ["detect_objects"]
    assert result["iterations"] == 2
    assert result["response"] == "No objects detected."


# ── MCP tool: image_s3_key injection ─────────────────────────────────────────

async def test_mcp_tool_injects_s3_key():
    """Agent must inject image_s3_key into MCP args — LLM never provides this."""
    impls = {"blur": _mcp_impl()}
    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, patch("app._MCP_TOOLS_IMPL", impls):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 3.0})),
                _ai("Done."),
            ])
            await run_agent([HumanMessage(content="Blur")])
    finally:
        _current_image_s3_key.reset(token)

    call_args = impls["blur"].ainvoke.call_args[0][0]
    assert call_args["args"]["image_s3_key"] == "images/test.jpg"


async def test_mcp_tool_passes_llm_args():
    """All args the LLM chose (radius, label, etc.) must be forwarded to MCP."""
    impls = {"blur": _mcp_impl()}
    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, patch("app._MCP_TOOLS_IMPL", impls):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 5.0, "label": "car", "indices": [1]})),
                _ai("Done."),
            ])
            await run_agent([HumanMessage(content="Blur 2nd car")])
    finally:
        _current_image_s3_key.reset(token)

    call_args = impls["blur"].ainvoke.call_args[0][0]["args"]
    assert call_args["radius"]  == 5.0
    assert call_args["label"]   == "car"
    assert call_args["indices"] == [1]
    assert call_args["image_s3_key"] == "images/test.jpg"


# ── MCP tool: result parsing ──────────────────────────────────────────────────

async def test_mcp_result_sets_processed_s3_key():
    impls = {"blur": _mcp_impl(s3_key="processed/abc/result.jpg")}
    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, patch("app._MCP_TOOLS_IMPL", impls):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 3.0})),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Blur")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_s3_key"] == "processed/abc/result.jpg"


async def test_mcp_result_sets_url_and_base64():
    b64 = _png_b64(40, 40, "green")
    impls = {"rotate": _mcp_impl(url="https://s3.example.com/result", b64=b64)}
    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, patch("app._MCP_TOOLS_IMPL", impls):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("rotate", {"angle": 90.0})),
                _ai("Rotated."),
            ])
            result = await run_agent([HumanMessage(content="Rotate")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_url"]    == "https://s3.example.com/result"
    assert result["processed_image_base64"] == b64
    assert result["annotated_image"]        == b64   # deprecated alias


async def test_mcp_result_updates_current_s3_key_for_chaining():
    """
    After first tool updates _current_image_s3_key, the second tool
    in the same turn must receive the NEW key, not the original.
    """
    captured_keys: list[str] = []

    def _track_ainvoke(tool_call):
        captured_keys.append(tool_call["args"]["image_s3_key"])
        result = json.dumps({
            "processed_image_s3_key": f"processed/step{len(captured_keys)}/result.jpg",
            "processed_image_url":    "https://s3.example.com/fake",
            "processed_image_base64": _png_b64(),
        })
        return MagicMock(content=result)

    blur_mock  = MagicMock()
    blur_mock.ainvoke = AsyncMock(side_effect=_track_ainvoke)
    flip_mock  = MagicMock()
    flip_mock.ainvoke = AsyncMock(side_effect=_track_ainvoke)

    impls = {"blur": blur_mock, "flip": flip_mock}
    token = _current_image_s3_key.set("images/original.jpg")
    try:
        with patch("app.llm_with_tools") as m, patch("app._MCP_TOOLS_IMPL", impls):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=(
                    _tc("blur", {"radius": 3.0},          "call_1") +
                    _tc("flip", {"direction": "horizontal"}, "call_2")
                )),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Blur then flip")])
    finally:
        _current_image_s3_key.reset(token)

    assert captured_keys[0] == "images/original.jpg"
    assert captured_keys[1] == "processed/step1/result.jpg"
    assert result["processed_image_s3_key"] == "processed/step2/result.jpg"


# ── MCP tool: error forwarding ────────────────────────────────────────────────

async def test_mcp_error_forwarded_to_llm():
    """Error in MCP result must be sent as ToolMessage, no crash."""
    impls = {"blur": _mcp_impl(error="No 'elephant' found in image")}
    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, patch("app._MCP_TOOLS_IMPL", impls):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 3.0, "label": "elephant"})),
                _ai("No elephants found."),
            ])
            result = await run_agent([HumanMessage(content="Blur elephant")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_base64"] is None
    assert result["processed_image_s3_key"] is None


# ── Error cases ────────────────────────────────────────────────────────────────

async def test_no_image_returns_clean_response():
    impls = {"blur": _mcp_impl()}
    # Do NOT set _current_image_s3_key
    with patch("app.llm_with_tools") as m, patch("app._MCP_TOOLS_IMPL", impls):
        m.ainvoke = AsyncMock(side_effect=[
            _ai("", tool_calls=_tc("blur", {"radius": 3.0})),
            _ai("Please upload an image first."),
        ])
        result = await run_agent([HumanMessage(content="Blur the image")])

    assert result["processed_image_base64"] is None
    assert result["processed_image_s3_key"] is None
    impls["blur"].ainvoke.assert_not_awaited()


async def test_mcp_tool_unavailable():
    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, patch("app._MCP_TOOLS_IMPL", {}):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 3.0})),
                _ai("Tool not available."),
            ])
            result = await run_agent([HumanMessage(content="Blur")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_base64"] is None


async def test_unknown_tool_handled():
    with patch("app.llm_with_tools") as m:
        m.ainvoke = AsyncMock(side_effect=[
            _ai("", tool_calls=_tc("nonexistent_tool", {})),
            _ai("Could not run tool."),
        ])
        result = await run_agent([HumanMessage(content="Do something")])
    assert result["response"] == "Could not run tool."


# ── All MCP tools reach MCP ───────────────────────────────────────────────────

@pytest.mark.parametrize("tool_name,args", [
    ("blur",      {"radius": 3.0}),
    ("rotate",    {"angle": 90.0}),
    ("flip",      {"direction": "horizontal"}),
    ("resize",    {"width": 50, "height": 50}),
    ("crop",      {"x1": 0, "y1": 0, "x2": 50, "y2": 100}),
    ("add_noise", {"amount": 0.1}),
])
async def test_all_mcp_tools_call_mcp(tool_name, args):
    impls = {tool_name: _mcp_impl()}
    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, patch("app._MCP_TOOLS_IMPL", impls):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc(tool_name, args)),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Process image")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_s3_key"] == "processed/fake/result.jpg"
    impls[tool_name].ainvoke.assert_awaited_once()


# ── annotated_image alias ─────────────────────────────────────────────────────

async def test_annotated_image_equals_processed_image_base64():
    b64 = _png_b64(40, 40, "blue")
    impls = {"blur": _mcp_impl(b64=b64)}
    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, patch("app._MCP_TOOLS_IMPL", impls):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 3.0})),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Blur")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["annotated_image"] is not None
    assert result["annotated_image"] == result["processed_image_base64"]
