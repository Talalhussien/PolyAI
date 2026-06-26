from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, Response
from prometheus_fastapi_instrumentator import Instrumentator
from ultralytics import YOLO
from PIL import Image
from pydantic import BaseModel
import sqlite3
import logging
import os
import uuid
import shutil
import time
import torch

torch.cuda.is_available = lambda: False


class PredictResponse(BaseModel):
    prediction_uid: str
    detection_count: int
    labels: list[str]
    time_took: float


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

app = FastAPI()


@app.on_event("shutdown")
def shutdown_event():   # pragma: no cover  ← ignored by coverage
    logging.info("Received shutdown request -- YOLO service is shutting down gracefully...")


Instrumentator().instrument(app).expose(app)

_raw_threshold = os.environ.get("CONFIDENCE_THRESHOLD")
if _raw_threshold is not None:
    CONFIDENCE_THRESHOLD = float(_raw_threshold)
    logging.info(f"CONFIDENCE_THRESHOLD set to {CONFIDENCE_THRESHOLD} (from environment)")
else:  # pragma: no cover - import-time default; tests set CONFIDENCE_THRESHOLD before import
    CONFIDENCE_THRESHOLD = 0.5
    logging.info(f"CONFIDENCE_THRESHOLD not set, using default: {CONFIDENCE_THRESHOLD}")

UPLOAD_DIR = "uploads/original"
PREDICTED_DIR = "uploads/predicted"
DB_PATH = "predictions.db"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PREDICTED_DIR, exist_ok=True)

model = YOLO("yolov8n.pt")


def init_db():
    """Initialize the SQLite database and create tables if they don't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prediction_sessions (
                uid TEXT PRIMARY KEY,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                original_image TEXT,
                predicted_image TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS detection_objects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_uid TEXT,
                label TEXT,
                score REAL,
                box TEXT,
                FOREIGN KEY (prediction_uid) REFERENCES prediction_sessions (uid)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prediction_uid ON detection_objects (prediction_uid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_label ON detection_objects (label)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_score ON detection_objects (score)")


def save_prediction_session(uid, original_image, predicted_image):
    """Save a prediction session to the database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO prediction_sessions (uid, original_image, predicted_image)
            VALUES (?, ?, ?)
        """, (uid, original_image, predicted_image))


def save_detection_object(prediction_uid, label, score, box):
    """Save a single detected object to the database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO detection_objects (prediction_uid, label, score, box)
            VALUES (?, ?, ?, ?)
        """, (prediction_uid, label, score, str(box)))


@app.post("/predict", response_model=PredictResponse)
def predict(file: UploadFile = File(...)):
    """Run YOLO object detection on the uploaded image and return structured results."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Uploaded file must be an image")

    ext = os.path.splitext(file.filename)[1]
    uid = str(uuid.uuid4())
    original_path = os.path.join(UPLOAD_DIR, uid + ext)
    predicted_path = os.path.join(PREDICTED_DIR, uid + ext)

    with open(original_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    start = time.time()
    results = model(original_path, device="cpu", conf=CONFIDENCE_THRESHOLD)

    annotated_frame = results[0].plot()
    annotated_image = Image.fromarray(annotated_frame)
    annotated_image.save(predicted_path)
    time_took = round(time.time() - start, 3)

    save_prediction_session(uid, original_path, predicted_path)

    detected_labels = []
    for box in results[0].boxes:
        label_idx = int(box.cls[0].item())
        label = model.names[label_idx]
        score = float(box.conf[0])
        bbox = box.xyxy[0].tolist()
        save_detection_object(uid, label, score, bbox)
        detected_labels.append(label)

    return PredictResponse(
        prediction_uid=uid,
        detection_count=len(results[0].boxes),
        labels=detected_labels,
        time_took=time_took,
    )


@app.get("/prediction/{uid}")
def get_prediction_by_uid(uid: str):
    """Get a prediction session by uid with all its detected objects."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        session = conn.execute(
            "SELECT * FROM prediction_sessions WHERE uid = ?", (uid,)
        ).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Prediction not found")

        objects = conn.execute(
            "SELECT * FROM detection_objects WHERE prediction_uid = ?", (uid,)
        ).fetchall()

        return {
            "uid": session["uid"],
            "timestamp": session["timestamp"],
            "original_image": session["original_image"],
            "predicted_image": session["predicted_image"],
            "detection_objects": [
                {
                    "id": obj["id"],
                    "label": obj["label"],
                    "score": obj["score"],
                    "box": obj["box"]
                } for obj in objects
            ]
        }


@app.get("/prediction/{uid}/image")
def get_prediction_image(uid: str):
    """Return the annotated (bounding-box) image for a prediction."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT predicted_image FROM prediction_sessions WHERE uid = ?", (uid,)
        ).fetchone()
    if not row or not os.path.exists(row[0]):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(row[0])


@app.get("/predictions/label/{label}")
def get_predictions_by_label(label: str):
    """Return all prediction sessions that contain at least one object with the given label."""
    if not label.strip():
        raise HTTPException(status_code=400, detail="Label cannot be empty")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        sessions = conn.execute(
            """
            SELECT DISTINCT ps.uid, ps.timestamp
            FROM prediction_sessions ps
            JOIN detection_objects det ON det.prediction_uid = ps.uid
            WHERE det.label = ?
            ORDER BY ps.timestamp
            """,
            (label,),
        ).fetchall()

        results = []
        for session in sessions:
            objects = conn.execute(
                """
                SELECT id, label, score, box
                FROM detection_objects
                WHERE prediction_uid = ? AND label = ?
                """,
                (session["uid"], label),
            ).fetchall()
            results.append({
                "uid": session["uid"],
                "timestamp": session["timestamp"],
                "detection_objects": [
                    {
                        "id": obj["id"],
                        "label": obj["label"],
                        "score": obj["score"],
                        "box": obj["box"],
                    }
                    for obj in objects
                ],
            })

        return results


@app.get("/predictions/score/{min_score}")
def get_predictions_by_score(min_score: float):
    """Return all detection objects whose confidence score is >= min_score."""
    if min_score < 0.0 or min_score > 1.0:
        raise HTTPException(status_code=400, detail="min_score must be between 0.0 and 1.0")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        objects = conn.execute(
            """
            SELECT id, prediction_uid, label, score, box
            FROM detection_objects
            WHERE score >= ?
            ORDER BY score DESC
            """,
            (min_score,),
        ).fetchall()

        return [
            {
                "id": obj["id"],
                "prediction_uid": obj["prediction_uid"],
                "label": obj["label"],
                "score": obj["score"],
                "box": obj["box"],
            }
            for obj in objects
        ]


@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    init_db()

    uvicorn.run(app, host="0.0.0.0", port=8080)
