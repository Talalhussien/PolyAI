"""
Tests for agent image-processing logic.

Coverage
--------
  Core loop:        text reply, tool execution, token accumulation, context-limit guard
  Whole-image:      blur, rotate, flip, resize, add_noise, crop (parametrized + individual)
  Single-object:    blur, rotate, flip, resize, add_noise (parametrized), crop (individual)
  Multiple-objects: blur (indices=[0,1])
  All-objects:      blur, rotate (all_objects=True)
  Selection:        from_right flag
  Errors:           no image, label not found, bad index, MCP unavailable, YOLO failure
"""

import base64
import io
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

from app import _current_image_s3_key, _yolo_cache, run_agent


# ── Test helpers ──────────────────────────────────────────────────────────────

def _png_b64(w: int = 20, h: int = 20, color: str = "red") -> str:
    """Create a valid base64-encoded PNG (starts with 'iVBO', triggers _is_base64_image)."""
    img = PILImage.new("RGB", (w, h), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _jpeg_bytes(w: int = 100, h: int = 100, color: str = "blue") -> bytes:
    """JPEG bytes for S3 get_object mock."""
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


def _mcp_impl(return_b64: str):
    """Mock MCP tool that returns the given base64 image string."""
    mock = MagicMock()
    mock.ainvoke = AsyncMock(return_value=MagicMock(content=return_b64))
    return mock


def _regions(count: int = 2, w: int = 40, h: int = 40) -> list:
    """Non-overlapping bounding boxes sorted left-to-right."""
    return [
        {"bbox": [i * 60, 10, i * 60 + w, 10 + h], "score": round(0.9 - i * 0.05, 2)}
        for i in range(count)
    ]


def _mock_s3(jpeg_b: bytes):
    """Mock s3_client; get_object returns a fresh BytesIO each call."""
    s3 = MagicMock()
    s3.get_object.side_effect = lambda **kw: {"Body": io.BytesIO(jpeg_b)}
    s3.put_object.return_value = {}
    s3.generate_presigned_url.return_value = "https://s3.example.com/fake"
    return s3


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


async def test_run_agent_executes_detect_objects():
    """detect_objects returns immediately (no image) without HTTP calls."""
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


# ── Whole-image processing ─────────────────────────────────────────────────────

WHOLE_IMAGE_CASES = [
    ("blur",      {"radius": 3.0}),
    ("rotate",    {"angle": 90.0}),
    ("flip",      {"direction": "horizontal"}),
    ("resize",    {"width": 50, "height": 50}),
    ("add_noise", {"amount": 0.1}),
]


@pytest.mark.parametrize("tool_name,extra_args", WHOLE_IMAGE_CASES)
async def test_whole_image_tool(tool_name, extra_args):
    """No label → fetch full image and send to MCP, no YOLO call."""
    fake_in  = _png_b64(20, 20, "gray")
    fake_out = _png_b64(20, 20, "green")
    s3    = _mock_s3(_jpeg_bytes())
    impls = {tool_name: _mcp_impl(fake_out)}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._fetch_full_image", return_value=fake_in), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc(tool_name, extra_args)),
                _ai(f"{tool_name} done."),
            ])
            result = await run_agent([HumanMessage(content="Apply " + tool_name)])
    finally:
        _current_image_s3_key.reset(token)

    assert result["tools_called"] == [tool_name]
    assert result["processed_image_base64"] == fake_out
    assert result["processed_image_s3_key"] is not None
    assert result["processed_image_url"] == "https://s3.example.com/fake"
    assert result["annotated_image"] == fake_out   # backward-compat alias
    s3.put_object.assert_called_once()


async def test_whole_image_crop():
    """crop without label crops to given pixel coords via MCP."""
    fake_in  = _png_b64(20, 20, "gray")
    fake_out = _png_b64(10, 10, "yellow")
    s3    = _mock_s3(_jpeg_bytes())
    impls = {"crop": _mcp_impl(fake_out)}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._fetch_full_image", return_value=fake_in), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("crop", {"x1": 0, "y1": 0, "x2": 50, "y2": 50})),
                _ai("Cropped."),
            ])
            result = await run_agent([HumanMessage(content="Crop the image")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["tools_called"] == ["crop"]
    assert result["processed_image_base64"] == fake_out
    assert result["processed_image_s3_key"] is not None
    impls["crop"].ainvoke.assert_awaited_once()


# ── Single-object processing ───────────────────────────────────────────────────

SINGLE_OBJ_CASES = [
    ("blur",      {"radius": 3.0}),
    ("rotate",    {"angle": 90.0}),
    ("flip",      {"direction": "horizontal"}),
    ("resize",    {"width": 50, "height": 50}),
    ("add_noise", {"amount": 0.1}),
]


@pytest.mark.parametrize("tool_name,extra_args", SINGLE_OBJ_CASES)
async def test_single_object(tool_name, extra_args):
    """Default (no indices, no all_objects) → processes leftmost object only."""
    fake_out = _png_b64(40, 40, "green")
    s3       = _mock_s3(_jpeg_bytes(200, 200))
    regions  = _regions(count=3)
    impls    = {tool_name: _mcp_impl(fake_out)}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=regions), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc(tool_name, {**extra_args, "label": "car"})),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Process car")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["tools_called"] == [tool_name]
    assert result["processed_image_s3_key"] is not None
    assert result["processed_image_base64"] is not None
    # MCP called exactly once — only the leftmost object
    impls[tool_name].ainvoke.assert_awaited_once()


async def test_single_object_second_from_left():
    """indices=[1] selects the second object, not the first."""
    fake_out = _png_b64(40, 40, "green")
    s3       = _mock_s3(_jpeg_bytes(200, 200))
    regions  = _regions(count=3)
    impls    = {"blur": _mcp_impl(fake_out)}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=regions), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 2.0, "label": "car", "indices": [1]})),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Blur 2nd car")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["tools_called"] == ["blur"]
    assert result["processed_image_s3_key"] is not None
    impls["blur"].ainvoke.assert_awaited_once()


async def test_single_object_crop():
    """crop + label extracts the object region client-side; MCP is NOT called."""
    s3      = _mock_s3(_jpeg_bytes(200, 200))
    regions = [{"bbox": [20, 30, 80, 90], "score": 0.95}]
    mcp_mock = _mcp_impl(_png_b64())
    impls    = {"crop": mcp_mock}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=regions), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("crop", {"label": "car"})),
                _ai("Extracted."),
            ])
            result = await run_agent([HumanMessage(content="Crop the car")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["tools_called"] == ["crop"]
    assert result["processed_image_s3_key"] is not None
    assert result["processed_image_base64"] is not None
    # MCP should NOT be called — crop is done client-side using the YOLO bbox
    mcp_mock.ainvoke.assert_not_awaited()


# ── Multiple-object processing ─────────────────────────────────────────────────

async def test_multiple_objects_blur():
    """indices=[0, 1] → MCP called twice, one per selected object."""
    fake_out = _png_b64(40, 40, "green")
    s3       = _mock_s3(_jpeg_bytes(200, 200))
    regions  = _regions(count=3)
    impls    = {"blur": _mcp_impl(fake_out)}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=regions), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {
                    "radius": 3.0, "label": "car", "indices": [0, 1]
                })),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Blur 1st and 2nd car")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["tools_called"] == ["blur"]
    assert result["processed_image_s3_key"] is not None
    assert impls["blur"].ainvoke.await_count == 2


async def test_multiple_objects_flip():
    """indices=[0, 2] → MCP called twice (1st and 3rd objects skipping the 2nd)."""
    fake_out = _png_b64(40, 40, "purple")
    s3       = _mock_s3(_jpeg_bytes(200, 200))
    regions  = _regions(count=3)
    impls    = {"flip": _mcp_impl(fake_out)}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=regions), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("flip", {
                    "direction": "vertical", "label": "person", "indices": [0, 2]
                })),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Flip 1st and 3rd person")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_s3_key"] is not None
    assert impls["flip"].ainvoke.await_count == 2


# ── All-object processing ──────────────────────────────────────────────────────

async def test_all_objects_blur():
    """all_objects=True → MCP called once per detected object."""
    fake_out = _png_b64(40, 40, "green")
    s3       = _mock_s3(_jpeg_bytes(200, 200))
    regions  = _regions(count=4)
    impls    = {"blur": _mcp_impl(fake_out)}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=regions), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {
                    "radius": 3.0, "label": "car", "all_objects": True
                })),
                _ai("All cars blurred."),
            ])
            result = await run_agent([HumanMessage(content="Blur all cars")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["tools_called"] == ["blur"]
    assert result["processed_image_s3_key"] is not None
    assert impls["blur"].ainvoke.await_count == 4


async def test_all_objects_rotate():
    """all_objects=True for rotate → composite with all regions rotated."""
    fake_out = _png_b64(40, 40, "blue")
    s3       = _mock_s3(_jpeg_bytes(200, 200))
    regions  = _regions(count=2)
    impls    = {"rotate": _mcp_impl(fake_out)}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=regions), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("rotate", {
                    "angle": 45.0, "label": "person", "all_objects": True
                })),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Rotate all persons")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["tools_called"] == ["rotate"]
    assert result["processed_image_s3_key"] is not None
    assert impls["rotate"].ainvoke.await_count == 2


async def test_all_objects_add_noise():
    fake_out = _png_b64(40, 40, "orange")
    s3       = _mock_s3(_jpeg_bytes(200, 200))
    regions  = _regions(count=3)
    impls    = {"add_noise": _mcp_impl(fake_out)}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=regions), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("add_noise", {
                    "amount": 0.2, "label": "car", "all_objects": True
                })),
                _ai("All cars noisy."),
            ])
            result = await run_agent([HumanMessage(content="Add noise to all cars")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_s3_key"] is not None
    assert impls["add_noise"].ainvoke.await_count == 3


# ── from_right selection ───────────────────────────────────────────────────────

async def test_from_right_selects_rightmost():
    """from_right=True + indices=[0] processes only one object (the rightmost)."""
    fake_out = _png_b64(40, 40, "cyan")
    s3       = _mock_s3(_jpeg_bytes(200, 200))
    # _get_all_regions is expected to be called with from_right=True and return
    # already-reversed list; test verifies only one MCP call (indices=[0])
    regions_reversed = [
        {"bbox": [120, 10, 170, 60], "score": 0.85},
        {"bbox": [60,  10, 110, 60], "score": 0.90},
        {"bbox": [0,   10,  50, 60], "score": 0.95},
    ]
    impls = {"flip": _mcp_impl(fake_out)}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=regions_reversed) as mock_get_regions, \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("flip", {
                    "direction": "horizontal", "label": "car",
                    "indices": [0], "from_right": True,
                })),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Flip rightmost car")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["tools_called"] == ["flip"]
    assert result["processed_image_s3_key"] is not None
    # Verify from_right=True was passed to _get_all_regions
    mock_get_regions.assert_called_once_with("images/test.jpg", "car", True)
    # Only one object selected
    impls["flip"].ainvoke.assert_awaited_once()


# ── Error cases ────────────────────────────────────────────────────────────────

async def test_no_image_returns_clean_response():
    """When no image key is in context, tool gracefully reports it."""
    impls = {"blur": _mcp_impl(_png_b64())}
    # Do NOT set _current_image_s3_key — default is None
    with patch("app.llm_with_tools") as m, \
         patch("app._MCP_TOOLS_IMPL", impls):
        m.ainvoke = AsyncMock(side_effect=[
            _ai("", tool_calls=_tc("blur", {"radius": 3.0})),
            _ai("Please upload an image first."),
        ])
        result = await run_agent([HumanMessage(content="Blur the image")])

    assert result["processed_image_base64"] is None
    assert result["processed_image_s3_key"] is None
    impls["blur"].ainvoke.assert_not_awaited()


async def test_label_not_found():
    """Zero regions returned → tool message sent, MCP not called."""
    impls = {"blur": _mcp_impl(_png_b64())}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=[]), \
             patch("app._MCP_TOOLS_IMPL", impls):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 3.0, "label": "elephant"})),
                _ai("No elephants found."),
            ])
            result = await run_agent([HumanMessage(content="Blur the elephant")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_base64"] is None
    impls["blur"].ainvoke.assert_not_awaited()


async def test_index_out_of_range():
    """Requesting indices=[5] when only 1 object exists → error message, no crash."""
    impls = {"blur": _mcp_impl(_png_b64())}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=_regions(count=1)), \
             patch("app._MCP_TOOLS_IMPL", impls):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 3.0, "label": "car", "indices": [5]})),
                _ai("Index out of range."),
            ])
            result = await run_agent([HumanMessage(content="Blur the 6th car")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_base64"] is None
    impls["blur"].ainvoke.assert_not_awaited()


async def test_mcp_tool_unavailable():
    """Empty _MCP_TOOLS_IMPL → unavailability message returned, no crash."""
    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._MCP_TOOLS_IMPL", {}):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 3.0})),
                _ai("Tool not available."),
            ])
            result = await run_agent([HumanMessage(content="Blur")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_base64"] is None


async def test_yolo_failure_handled_gracefully():
    """YOLO exception → clean error ToolMessage, agent continues without crash."""
    impls = {"blur": _mcp_impl(_png_b64())}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", side_effect=RuntimeError("YOLO timeout")), \
             patch("app._MCP_TOOLS_IMPL", impls):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 3.0, "label": "car"})),
                _ai("Detection failed."),
            ])
            result = await run_agent([HumanMessage(content="Blur the car")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_base64"] is None
    impls["blur"].ainvoke.assert_not_awaited()


# ── YOLO cache consistency ─────────────────────────────────────────────────────

async def test_yolo_cache_reused_within_request():
    """
    detect_objects populates _yolo_cache; a subsequent label-specific processing
    tool must reuse the cached prediction and NOT call YOLO a second time.
    """
    fake_out = _png_b64(40, 40, "green")
    s3       = _mock_s3(_jpeg_bytes(200, 200))
    impls    = {"blur": _mcp_impl(fake_out)}

    cached_prediction = {
        "detection_objects": [
            {"label": "car", "score": 0.91, "box": "[10, 10, 60, 60]"},
            {"label": "car", "score": 0.53, "box": "[70, 10, 120, 60]"},
            {"label": "car", "score": 0.67, "box": "[130, 10, 180, 60]"},
        ]
    }

    token_key   = _current_image_s3_key.set("images/test.jpg")
    token_cache = _yolo_cache.set({"s3_key": "images/test.jpg", "prediction": cached_prediction})
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3), \
             patch("app.httpx") as mock_httpx:
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 3.0, "label": "car", "all_objects": True})),
                _ai("All cars blurred."),
            ])
            result = await run_agent([HumanMessage(content="Blur all cars")])
    finally:
        _current_image_s3_key.reset(token_key)
        _yolo_cache.reset(token_cache)

    assert impls["blur"].ainvoke.await_count == 3
    assert result["processed_image_s3_key"] is not None
    mock_httpx.Client.assert_not_called()


async def test_mcp_partial_failure_continues():
    """
    If MCP fails for one object but succeeds for others, agent reports partial
    success rather than crashing.  We simulate this by making ainvoke raise on
    the first call and succeed on the second.
    """
    fake_out = _png_b64(40, 40, "green")
    s3       = _mock_s3(_jpeg_bytes(200, 200))
    regions  = _regions(count=2)

    failing_then_ok = MagicMock()
    failing_then_ok.ainvoke = AsyncMock(
        side_effect=[RuntimeError("MCP error"), MagicMock(content=fake_out)]
    )
    impls = {"blur": failing_then_ok}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=regions), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {
                    "radius": 3.0, "label": "car", "all_objects": True
                })),
                _ai("Partial success."),
            ])
            result = await run_agent([HumanMessage(content="Blur all cars")])
    finally:
        _current_image_s3_key.reset(token)

    # Second object succeeded → composite uploaded
    assert result["processed_image_s3_key"] is not None
    assert failing_then_ok.ainvoke.await_count == 2


# ── Working image state ────────────────────────────────────────────────────────

async def test_whole_image_tool_does_not_call_yolo():
    """Whole-image blur (no label) must never call the YOLO HTTP service."""
    fake_out = _png_b64(40, 40, "red")
    s3       = _mock_s3(_jpeg_bytes(200, 200))
    impls    = {"blur": _mcp_impl(fake_out)}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3), \
             patch("app.httpx") as mock_httpx:
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 3.0})),  # no label
                _ai("Blurred."),
            ])
            result = await run_agent([HumanMessage(content="Blur the whole image")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_s3_key"] is not None
    mock_httpx.Client.assert_not_called()


async def test_object_specific_then_whole_image_uses_processed_key():
    """
    After a label-specific tool updates _current_image_s3_key, a whole-image
    tool in the same turn must fetch from that new processed key, not the original.
    """
    blur_out = _png_b64(40, 40, "blue")
    flip_out = _png_b64(200, 200, "green")
    regions  = _regions(count=1)
    impls    = {"blur": _mcp_impl(blur_out), "flip": _mcp_impl(flip_out)}

    get_object_keys: list = []

    def tracking_get_object(**kw):
        get_object_keys.append(kw["Key"])
        return {"Body": io.BytesIO(_jpeg_bytes(200, 200))}

    s3 = MagicMock()
    s3.get_object.side_effect    = tracking_get_object
    s3.put_object.return_value   = {}
    s3.generate_presigned_url.return_value = "https://s3.example.com/fake"

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", return_value=regions), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=(
                    _tc("blur", {"radius": 3.0, "label": "car"}, "call_1") +
                    _tc("flip", {"direction": "horizontal"},      "call_2")
                )),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Blur car then flip whole")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_s3_key"] is not None
    # The flip (whole-image) path must have called _fetch_full_image with the
    # processed key — verify at least one get_object used a "processed/" key.
    assert any("processed/" in k for k in get_object_keys), (
        f"Expected flip to load from processed/ key. All get_object keys: {get_object_keys}"
    )


async def test_chains_two_label_tools_use_composite():
    """
    Two label-specific tools in the same turn (blur car, add_noise dog).
    The second tool must crop from current_composite (no extra S3 load),
    not re-download the original image.
    """
    blur_out  = _png_b64(40, 40, "blue")
    noise_out = _png_b64(40, 40, "green")

    car_regions = [{"bbox": [0,  10, 60,  60], "score": 0.9}]
    dog_regions = [{"bbox": [100, 10, 160, 60], "score": 0.85}]

    def fake_regions(s3_key, label, from_right=False):
        return car_regions if label == "car" else dog_regions

    impls = {
        "blur":      _mcp_impl(blur_out),
        "add_noise": _mcp_impl(noise_out),
    }

    load_count = {"n": 0}

    def tracking_get_object(**kw):
        load_count["n"] += 1
        return {"Body": io.BytesIO(_jpeg_bytes(200, 200))}

    s3 = MagicMock()
    s3.get_object.side_effect    = tracking_get_object
    s3.put_object.return_value   = {}
    s3.generate_presigned_url.return_value = "https://s3.example.com/fake"

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._get_all_regions", side_effect=fake_regions), \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=(
                    _tc("blur",      {"radius": 3.0, "label": "car"}, "call_1") +
                    _tc("add_noise", {"amount": 0.1, "label": "dog"}, "call_2")
                )),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Blur car and add noise to dog")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["processed_image_s3_key"] is not None
    assert impls["blur"].ainvoke.await_count      == 1
    assert impls["add_noise"].ainvoke.await_count == 1
    # S3 must be loaded exactly once — second tool reused current_composite
    assert load_count["n"] == 1, (
        f"Expected S3 loaded once (composite reused for second tool), "
        f"got {load_count['n']} loads"
    )


async def test_annotated_image_equals_processed_image_base64():
    """annotated_image is a deprecated alias; it must always equal processed_image_base64."""
    fake_out = _png_b64(40, 40, "blue")
    s3       = _mock_s3(_jpeg_bytes(200, 200))
    impls    = {"blur": _mcp_impl(fake_out)}

    token = _current_image_s3_key.set("images/test.jpg")
    try:
        with patch("app.llm_with_tools") as m, \
             patch("app._MCP_TOOLS_IMPL", impls), \
             patch("app.s3_client", s3):
            m.ainvoke = AsyncMock(side_effect=[
                _ai("", tool_calls=_tc("blur", {"radius": 3.0})),
                _ai("Done."),
            ])
            result = await run_agent([HumanMessage(content="Blur")])
    finally:
        _current_image_s3_key.reset(token)

    assert result["annotated_image"] is not None
    assert result["annotated_image"] == result["processed_image_base64"]
