import os

# Must be set before importing app.py, which reads MODEL at module level.
os.environ.setdefault("MODEL", "bedrock_converse/anthropic.claude-3-5-haiku-20241022-v1:0")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "fake")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "fake")

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app import app, TokensUsed


FAKE_AGENT_RESPONSE = {
    "response": "I see 2 people.",
    "prediction_id": None,
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
    with patch("app.run_agent", return_value=FAKE_AGENT_RESPONSE):
        response = client.post(
            "/chat",
            json={"messages": [
                {"role": "user", "content": "What is in this image?", "image_base64": "AAAA"},
            ]},
        )
    assert response.status_code == 200


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
