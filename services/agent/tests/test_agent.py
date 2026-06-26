import os

# Must be set before importing app.py, which reads MODEL at module level.
os.environ.setdefault("MODEL", "bedrock_converse/anthropic.claude-3-5-haiku-20241022-v1:0")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "fake")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "fake")

from unittest.mock import patch, MagicMock
from langchain_core.messages import AIMessage, HumanMessage

from app import run_agent


def _ai(content, *, tool_calls=None, input_tokens=10, output_tokens=5):
    """Build a fake AIMessage with usage_metadata."""
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


def _tool_call():
    """Return a minimal detect_objects tool-call descriptor."""
    return [{"name": "detect_objects", "args": {}, "id": "call_test_1", "type": "tool_call"}]


# --- simple text reply (no tool calls) --------------------------------------

def test_run_agent_returns_content():
    fake = _ai("Hello there!")
    with patch("app.llm_with_tools") as mock_llm:
        mock_llm.invoke = MagicMock(return_value=fake)
        result = run_agent([HumanMessage(content="Hi!")])

    assert result["response"] == "Hello there!"


def test_run_agent_one_iteration_no_tools():
    fake = _ai("Done.")
    with patch("app.llm_with_tools") as mock_llm:
        mock_llm.invoke = MagicMock(return_value=fake)
        result = run_agent([HumanMessage(content="Hi!")])

    assert result["iterations"] == 1
    assert result["tools_called"] == []
    assert result["context_limit_exceeded"] is False


def test_run_agent_token_counts_no_tools():
    fake = _ai("Done.", input_tokens=10, output_tokens=5)
    with patch("app.llm_with_tools") as mock_llm:
        mock_llm.invoke = MagicMock(return_value=fake)
        result = run_agent([HumanMessage(content="Hi!")])

    assert result["tokens_used"].input == 10
    assert result["tokens_used"].output == 5
    assert result["tokens_used"].total == 15


# --- tool call path ---------------------------------------------------------

def test_run_agent_executes_tool_call():
    responses = [
        _ai("", tool_calls=_tool_call(), input_tokens=20, output_tokens=3),
        _ai("No objects detected.", input_tokens=30, output_tokens=8),
    ]
    with patch("app.llm_with_tools") as mock_llm:
        mock_llm.invoke = MagicMock(side_effect=responses)
        result = run_agent([HumanMessage(content="What is in this image?")])

    assert result["tools_called"] == ["detect_objects"]
    assert result["iterations"] == 2
    assert result["response"] == "No objects detected."


def test_run_agent_accumulates_tokens_across_iterations():
    responses = [
        _ai("", tool_calls=_tool_call(), input_tokens=20, output_tokens=3),
        _ai("Done.", input_tokens=30, output_tokens=8),
    ]
    with patch("app.llm_with_tools") as mock_llm:
        mock_llm.invoke = MagicMock(side_effect=responses)
        result = run_agent([HumanMessage(content="Go!")])

    assert result["tokens_used"].input == 20 + 30
    assert result["tokens_used"].output == 3 + 8
    assert result["tokens_used"].total == 23 + 38


# --- max iterations guard ---------------------------------------------------

def test_run_agent_context_limit_exceeded():
    # Always return a tool call so the loop never exits naturally.
    # After max_iterations it will force a final answer.
    looping = _ai("", tool_calls=_tool_call(), input_tokens=5, output_tokens=1)
    final = _ai("Giving up.", input_tokens=5, output_tokens=2)

    # max_iterations=2 means after 2 loops it fires the emergency call.
    responses = [looping, looping, final]
    with patch("app.llm_with_tools") as mock_llm:
        mock_llm.invoke = MagicMock(side_effect=responses)
        result = run_agent([HumanMessage(content="Loop forever")], max_iterations=2)

    assert result["context_limit_exceeded"] is True
    assert result["response"] == "Giving up."
