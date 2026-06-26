import os
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

# Must be set before importing app.py, which reads these at module level.
os.environ.setdefault("CONFIDENCE_THRESHOLD", "0.5")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_S3_BUCKET", "fake-bucket")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "fake")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "fake")

from app import (
    app,
    init_db,
    save_prediction_session,
    save_detection_object,
)

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "data", "beatles.jpeg")


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    """Point the app at a throwaway database and upload dirs for each test."""
    monkeypatch.setattr("app.DB_PATH", str(tmp_path / "test_predictions.db"))

    upload_dir = tmp_path / "original"
    predicted_dir = tmp_path / "predicted"
    upload_dir.mkdir()
    predicted_dir.mkdir()
    monkeypatch.setattr("app.UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr("app.PREDICTED_DIR", str(predicted_dir))

    init_db()


@pytest.fixture
def client():
    return TestClient(app)


def seed_session(uid, *, predicted_image="pred.jpg", objects=()):
    """Insert a prediction session plus its detection objects directly."""
    save_prediction_session(uid, f"orig-{uid}.jpg", predicted_image)
    for label, score, box in objects:
        save_detection_object(uid, label, score, box)


# --- GET /health -------------------------------------------------------------

def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- POST /predict -----------------------------------------------------------

def test_predict_detects_objects(client):
    with open(TEST_IMAGE, "rb") as f:
        image_bytes = f.read()

    mock_body = MagicMock()
    mock_body.read.return_value = image_bytes

    with patch("app.s3_client") as mock_s3:
        mock_s3.get_object.return_value = {"Body": mock_body}
        mock_s3.put_object.return_value = {}
        response = client.post(
            "/predict",
            json={"image_s3_key": "images/test/original.jpg"},
        )
    assert response.status_code == 200
    body = response.json()
    assert "prediction_uid" in body
    assert isinstance(body["labels"], list)
    assert body["detection_count"] == len(body["labels"])


def test_predict_requires_image_s3_key(client):
    # Missing required field → 422 Unprocessable Entity
    response = client.post("/predict", json={})
    assert response.status_code == 422


# --- GET /prediction/{uid} ---------------------------------------------------

def test_get_prediction_by_uid_found(client):
    seed_session("abc-123", objects=[("person", 0.91, "[10, 20, 100, 200]")])
    response = client.get("/prediction/abc-123")
    assert response.status_code == 200
    body = response.json()
    assert body["uid"] == "abc-123"
    assert len(body["detection_objects"]) == 1
    assert body["detection_objects"][0]["label"] == "person"


def test_get_prediction_by_uid_not_found(client):
    response = client.get("/prediction/does-not-exist")
    assert response.status_code == 404


# --- GET /prediction/{uid}/image ---------------------------------------------

def test_get_prediction_image_found(client):
    seed_session("img-1", predicted_image="predictions/img-1/predicted.jpg")

    mock_body = MagicMock()
    mock_body.read.return_value = b"fake-image-bytes"

    with patch("app.s3_client") as mock_s3:
        mock_s3.get_object.return_value = {"Body": mock_body}
        response = client.get("/prediction/img-1/image")

    assert response.status_code == 200
    assert response.content == b"fake-image-bytes"


def test_get_prediction_image_uid_not_found(client):
    response = client.get("/prediction/nope/image")
    assert response.status_code == 404


def test_get_prediction_image_file_missing(client):
    from botocore.exceptions import ClientError
    seed_session("img-2", predicted_image="predictions/img-2/predicted.jpg")

    with patch("app.s3_client") as mock_s3:
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not Found"}}, "GetObject"
        )
        response = client.get("/prediction/img-2/image")

    assert response.status_code == 404


# --- GET /predictions/label/{label} ------------------------------------------

def test_get_predictions_by_label_with_matches(client):
    seed_session("s1", objects=[
        ("person", 0.91, "[10, 20, 100, 200]"),
        ("car", 0.80, "[0, 0, 50, 50]"),
    ])
    seed_session("s2", objects=[("car", 0.70, "[1, 1, 2, 2]")])

    response = client.get("/predictions/label/person")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["uid"] == "s1"
    # Only the objects matching the requested label are returned.
    assert [o["label"] for o in body[0]["detection_objects"]] == ["person"]


def test_get_predictions_by_label_no_matches(client):
    seed_session("s1", objects=[("car", 0.80, "[0, 0, 50, 50]")])
    response = client.get("/predictions/label/person")
    assert response.status_code == 200
    assert response.json() == []


def test_get_predictions_by_label_empty(client):
    # A blank label (a space) reaches the handler and must be rejected.
    response = client.get("/predictions/label/ ")
    assert response.status_code == 400
    assert response.json()["detail"] == "Label cannot be empty"


# --- GET /predictions/score/{min_score} --------------------------------------

def test_get_predictions_by_score_with_matches(client):
    seed_session("s1", objects=[
        ("person", 0.91, "[10, 20, 100, 200]"),
        ("car", 0.30, "[0, 0, 50, 50]"),
    ])
    response = client.get("/predictions/score/0.5")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["label"] == "person"
    assert body[0]["prediction_uid"] == "s1"


def test_get_predictions_by_score_no_matches(client):
    seed_session("s1", objects=[("car", 0.30, "[0, 0, 50, 50]")])
    response = client.get("/predictions/score/0.9")
    assert response.status_code == 200
    assert response.json() == []


def test_get_predictions_by_score_out_of_range(client):
    response = client.get("/predictions/score/1.5")
    assert response.status_code == 400
    assert response.json()["detail"] == "min_score must be between 0.0 and 1.0"
