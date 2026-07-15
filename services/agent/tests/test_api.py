import base64
import io
import os

# Must be set before importing app.py, which reads MODEL at module level.
os.environ.setdefault("MODEL", "bedrock_converse/amazon.nova-lite-v1:0")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_S3_BUCKET", "fake-bucket")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "fake")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "fake")

import pytest
from fastapi.testclient import TestClient
from PIL import Image as PILImage
from unittest.mock import patch

from app import app, TokensUsed


def _valid_jpeg_b64() -> str:
    """Return a base64-encoded 10x10 red JPEG — a real image the agent can open."""
    img = PILImage.new("RGB", (10, 10), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


FAKE_AGENT_RESPONSE = {
    "response": "I see 2 people.",
    "chat_session_id": "fake-session-id",
    "prediction_id": None,
    "processed_image_base64": None,
    "processed_image_s3_key": None,
    "processed_image_url": None,
    "annotated_image": None,
    "agent_loop_time_s": 0.05,
    "iterations": 1,
    "tools_called": [],
    "context_limit_exceeded": False,
    "tokens_used": TokensUsed(input=10, output=5, total=15),
}


@pytest.fixture
def client():
    return TestClient(app)


# --- GET /health -------------------------------------------------------------

def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- POST /chat --------------------------------------------------------------

def test_chat_returns_response(client):
    with patch("app.run_agent", return_value=FAKE_AGENT_RESPONSE):
        response = client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "Hello!"}]},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "I see 2 people."
    assert body["iterations"] == 1
    assert body["tools_called"] == []
    assert body["context_limit_exceeded"] is False


def test_chat_tokens_used_in_response(client):
    with patch("app.run_agent", return_value=FAKE_AGENT_RESPONSE):
        response = client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "Hello!"}]},
        )
    assert response.status_code == 200
    tokens = response.json()["tokens_used"]
    assert tokens["input"] == 10
    assert tokens["output"] == 5
    assert tokens["total"] == 15


def test_chat_with_image(client):
    with patch("app.run_agent", return_value=FAKE_AGENT_RESPONSE), \
         patch("app.s3_client") as mock_s3:
        mock_s3.put_object.return_value = {}
        response = client.post(
            "/chat",
            json={"messages": [
                {"role": "user", "content": "What is in this image?",
                 "image_base64": _valid_jpeg_b64()},
            ]},
        )
    assert response.status_code == 200


def test_chat_with_image_includes_dimensions(client):
    """Verify the LLM message contains the image dimensions when a valid image is uploaded."""
    captured = {}

    async def fake_run_agent(messages, **kw):
        captured["content"] = messages[-1].content
        return FAKE_AGENT_RESPONSE

    with patch("app.run_agent", side_effect=fake_run_agent), \
         patch("app.s3_client") as mock_s3:
        mock_s3.put_object.return_value = {}
        client.post(
            "/chat",
            json={"messages": [
                {"role": "user", "content": "describe",
                 "image_base64": _valid_jpeg_b64()},
            ]},
        )

    assert "10x10 px" in captured.get("content", ""), (
        f"Expected dimensions in message, got: {captured.get('content')}"
    )


def test_chat_with_invalid_base64_returns_400(client):
    """Sending garbage base64 must return 400, not a 500 crash."""
    response = client.post(
        "/chat",
        json={"messages": [
            {"role": "user", "content": "blur", "image_base64": "!!!not-base64!!!"},
        ]},
    )
    assert response.status_code == 400


def test_chat_with_conversation_history(client):
    with patch("app.run_agent", return_value=FAKE_AGENT_RESPONSE):
        response = client.post(
            "/chat",
            json={"messages": [
                {"role": "user", "content": "Hello!"},
                {"role": "assistant", "content": "Hi there!"},
                {"role": "user", "content": "Detect objects."},
            ]},
        )
    assert response.status_code == 200


def test_chat_missing_messages_field(client):
    response = client.post("/chat", json={})
    assert response.status_code == 422


def test_chat_invalid_body(client):
    response = client.post("/chat", json={"bad_field": "oops"})
    assert response.status_code == 422
