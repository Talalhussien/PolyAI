---
name: yolo-api-tests
description: Use when writing or updating tests for the YOLO FastAPI service after the SQLAlchemy refactor, covering any database-backed endpoint.
---

# YOLO API Tests

Use this skill to write safe, repeatable tests for the YOLO FastAPI API after the SQLAlchemy refactor.

## Course Safety

This course repo asks agents not to run terminal commands for students.

Do not run `pytest`, `python`, `grep`, `rg`, `git`, `pip`, `docker`, or other shell commands unless the user explicitly overrides that rule. Write or update the tests, then tell the student the exact commands to run manually.

## Precondition

Before writing database-backed API tests, inspect the app files.

If the app still uses raw `sqlite3`, `DB_PATH`, raw SQL persistence, or `init_db()`, **do not ask the user — automatically perform the full SQLAlchemy refactor by following all steps in the `yolo-api-data-layer` skill, then continue writing the tests.** Do not stop, do not ask for confirmation, do not wait. The refactor and the tests are one continuous task.

If something cannot be determined from the task or files, say: `Not clear from the task/files.`

## Required Test Design

Tests must:

1. Use `pytest`.
2. Use `fastapi.testclient.TestClient`.
3. Use a temporary SQLite database through SQLAlchemy.
4. Create a fresh database per test or per fixture scope with `tmp_path`.
5. Create tables with `Base.metadata.create_all(bind=engine)`.
6. Clean up with `Base.metadata.drop_all(bind=engine)` and `engine.dispose()`.
7. Override FastAPI's database dependency with `app.dependency_overrides[get_db]`.
8. Use the real SQLAlchemy models from `models.py`.
9. Avoid duplicate model definitions inside tests.
10. Assert status codes and response bodies.
11. Mock YOLO inference when testing `/predict`.
12. Preserve existing endpoint response shapes.

## Forbidden Database Patterns

Do not use these in tests:

```python
import sqlite3
sqlite3.connect(...)
conn.execute(...)
cursor.execute(...)
init_db()
DB_PATH
monkeypatch.setattr(..., "DB_PATH", ...)
```

Do not monkeypatch `SessionLocal`, `engine`, or other internals of the `db` module.

The only allowed database override is FastAPI dependency override:

```python
app.dependency_overrides[get_db] = override_get_db
```

Monkeypatch is allowed for YOLO inference/model behavior, but not for replacing the database layer.

## Fixture Pattern

Adapt imports to the project structure. If tests run from `services/yolo/`, direct imports may be correct:

```python
from app import app
from db import get_db
from models import Base, PredictionSession, DetectionObject
```

If the app is imported as a package, use package imports instead:

```python
from services.yolo.app import app
from services.yolo.db import get_db
from services.yolo.models import Base, PredictionSession, DetectionObject
```

Use this fixture pattern:

```python
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import app
from db import get_db
from models import Base, PredictionSession, DetectionObject


@pytest.fixture
def db_session_factory(tmp_path):
    test_db_path = tmp_path / "test.db"
    test_database_url = f"sqlite:///{test_db_path}"

    engine = create_engine(
        test_database_url,
        connect_args={"check_same_thread": False},
    )

    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )

    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    try:
        yield TestingSessionLocal
    finally:
        app.dependency_overrides.pop(get_db, None)
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def client(db_session_factory):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(db_session_factory):
    session = db_session_factory()
    try:
        yield session
    finally:
        session.close()
```

## Seed Helper

Use SQLAlchemy models to seed database state:

```python
def seed_prediction_session(
    db,
    uid,
    *,
    original_image=None,
    predicted_image=None,
    objects=None,
):
    prediction = PredictionSession(
        uid=uid,
        original_image=original_image or f"original-{uid}.jpg",
        predicted_image=predicted_image or f"predicted-{uid}.jpg",
    )

    db.add(prediction)
    db.commit()

    for label, score, box in objects or []:
        detection = DetectionObject(
            prediction_uid=uid,
            label=label,
            score=score,
            box=box,
        )
        db.add(detection)

    db.commit()
    return prediction
```

If the model has extra required fields, include them in the helper.

## Testing `/predict`

When testing `/predict`, do not run real YOLO inference.

Inspect the app to find the exact function, object, or method that performs inference. Patch that smallest unit so the endpoint receives deterministic detection data.

A `/predict` test should verify:

1. The endpoint returns the existing success status code.
2. The response body has the same fields as before the refactor.
3. A `PredictionSession` row is created.
4. Related `DetectionObject` rows are created.
5. Stored labels, scores, and boxes match the mocked prediction result.

If the existing response shape contains these keys, assert them exactly:

```python
body = response.json()

assert "prediction_uid" in body
assert "detection_count" in body
assert "labels" in body
assert "time_took" in body
```

Then verify persistence through SQLAlchemy:

```python
uid = body["prediction_uid"]

saved_session = db.query(PredictionSession).filter_by(uid=uid).first()
assert saved_session is not None

saved_objects = (
    db.query(DetectionObject)
    .filter_by(prediction_uid=uid)
    .all()
)
assert len(saved_objects) == body["detection_count"]
```

## Testing Read Endpoints

For endpoints that read predictions, seed data directly with SQLAlchemy instead of calling `/predict`.

Example:

```python
def test_get_prediction_by_uid_found(client, db):
    seed_prediction_session(
        db,
        "abc-123",
        objects=[
            ("person", 0.91, "[10, 20, 100, 200]"),
        ],
    )

    response = client.get("/prediction/abc-123")

    assert response.status_code == 200

    body = response.json()
    assert body["uid"] == "abc-123"
    assert "detection_objects" in body
    assert body["detection_objects"][0]["label"] == "person"
```

Use the actual route path and response fields from the app. If the route is `/predictions/{uid}` instead of `/prediction/{uid}`, use the actual route.

## Testing Recent Predictions

For `GET /predictions/recent`, seed more than 10 sessions with different timestamps.

Assert:

- Response status code is correct.
- At most 10 sessions are returned.
- The newest session appears first.
- Empty database returns the app's expected empty response shape.

## Testing Delete Endpoints

For delete behavior, verify both parent and child rows:

```python
def test_delete_prediction_removes_session_and_objects(client, db):
    seed_prediction_session(
        db,
        "abc-123",
        objects=[
            ("person", 0.91, "[1, 2, 3, 4]"),
            ("car", 0.80, "[5, 6, 7, 8]"),
        ],
    )

    response = client.delete("/predictions/abc-123")

    assert response.status_code == 200

    session_row = db.query(PredictionSession).filter_by(uid="abc-123").first()
    assert session_row is None

    objects = (
        db.query(DetectionObject)
        .filter_by(prediction_uid="abc-123")
        .all()
    )
    assert objects == []
```

Use the actual delete route and expected status code from the app.

Also test missing UID behavior.

## Status Code Rule

Every test that calls a mutation endpoint (POST, PUT, DELETE, PATCH) **must assert `response.status_code` before checking the database**. If the status code is wrong, the test must fail at that line — not at a silent DB check that hides the real cause.

```python
response = client.post("/predictions/pred-1/feedback", json={"rating": 4})
assert response.status_code == 200  # fail here if the endpoint rejected the payload
feedback_rows = db.query(UserFeedback).filter_by(prediction_uid="pred-1").all()
assert len(feedback_rows) == 1
```

If this assertion is missing and the endpoint returns 422, the DB check will show `0 == 1` with no indication that the request was rejected.

## Payload Validation Rule

Before writing a test payload, read the actual Pydantic request model in `app.py` to find required and optional fields.

- If a field is `Optional[str]`, `None` is valid.
- If a field is `str`, sending `None` causes a 422. Either omit the field or send a string value.
- Never send `None` for a field unless the model explicitly marks it as `Optional`.

```python
# Wrong if comment is str, not Optional[str]:
json={"rating": 3, "comment": None}

# Correct:
json={"rating": 3}             # omit optional field
json={"rating": 3, "comment": "good"}  # or send a real value
```

## Testing User Feedback

When a `UserFeedback` model or endpoint exists, tests should verify:

- Feedback can be stored for an existing prediction.
- Feedback is linked to the correct prediction UID.
- Missing prediction UID returns the expected error.
- Existing prediction endpoints keep their previous response shape.

Always assert `response.status_code` before checking `db.query(UserFeedback)`.

Import `UserFeedback` from `models.py`; do not define it again in tests.

## Manual Verification To Give The Student

At the end, tell the student to run the appropriate command manually from the YOLO service directory, for example:

```bash
pytest tests/
```

Also tell them to inspect that tests do not contain old database patterns:

```bash
grep -R "sqlite3\|DB_PATH\|init_db\|cursor.execute\|conn.execute" tests/
```

Do not run these commands yourself in this course repo unless the user explicitly allows it.

## Completion Criteria

Test work is complete when:

1. Tests use SQLAlchemy models.
2. Tests use isolated temporary SQLite databases.
3. Tests use `app.dependency_overrides[get_db]`.
4. Tests do not use `sqlite3`.
5. Tests do not use raw SQL.
6. Tests do not use `DB_PATH`.
7. Tests do not call `init_db()`.
8. Tests mock YOLO inference for `/predict`.
9. Tests assert response status codes and response bodies.
10. Tests verify database persistence through ORM queries.
