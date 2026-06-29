---
name: yolo-api-data-layer
description: Use for any task involving the YOLO service prediction data — refactoring to SQLAlchemy, adding endpoints that query predictions (recent, by label, by score, delete), adding models or columns (UserFeedback, processing_time_ms), configuring Postgres, writing prediction tests, or fixing database architecture.
---

# YOLO API Data Layer

Use this skill for database-layer work in the YOLO FastAPI service.

The target architecture is:

- SQLAlchemy ORM models in `models.py`
- SQLAlchemy engine/session setup in `db.py`
- SQLite as the default local backend
- PostgreSQL selected through environment variables
- FastAPI `Depends(get_db)` for route database sessions
- No raw `sqlite3` persistence in application code
- Existing endpoint paths, status codes, and response shapes preserved

## Course Safety

This course repo asks agents not to run terminal commands for students.

Do not run `npm`, `pip`, `git`, `python`, `docker`, `grep`, `rg`, or other shell commands unless the user explicitly overrides that rule. Use file read/search/edit tools when available.

At the end, tell the student the exact commands to run manually.

Never run destructive git commands such as `git reset --hard`.

Running `pytest` is allowed — but only once, after all file changes are complete. See **Post-Implementation Test Run**.

## First Inspect

Before editing, inspect the relevant files with read-only file tools:

- The YOLO FastAPI entrypoint, usually `services/yolo/app.py`
- Existing tests, usually under `services/yolo/tests/`
- The dependency file, such as `services/yolo/requirements.txt`
- Any existing database helper code
- Existing endpoint paths, status codes, and response body shapes

Find the current table names, column names, endpoint names, and response keys. Preserve them unless the user explicitly asks for a change.

If something cannot be determined from the task or files, say: `Not clear from the task/files.`

## Refactor Order

For any prompt that touches prediction data:

1. Check whether application code still uses raw SQLite.
2. If raw SQLite exists, complete the SQLAlchemy refactor first.
3. Add the requested feature only after the ORM data layer is in place.
4. Update tests to use SQLAlchemy and FastAPI dependency overrides.
5. Tell the student which verification commands to run manually.

Do not add new database features on top of the old raw SQLite layer.

## Required Files

Create these files beside the YOLO app entrypoint unless the project layout clearly requires package-relative paths:

```text
models.py
db.py
```

In this repo, that will usually mean:

```text
services/yolo/models.py
services/yolo/db.py
```

Use imports that match the project layout. If `app.py`, `models.py`, and `db.py` are in the same folder, use direct imports such as:

```python
from db import get_db, engine
from models import Base, PredictionSession, DetectionObject
```

If they are inside a package, use package-relative imports.

## Models

Create SQLAlchemy ORM models for the prediction tables.

Baseline `models.py`:

```python
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class PredictionSession(Base):
    __tablename__ = "prediction_sessions"

    uid = Column(String, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    original_image = Column(String)
    predicted_image = Column(String)

    detection_objects = relationship(
        "DetectionObject",
        back_populates="prediction_session",
        cascade="all, delete-orphan",
    )


class DetectionObject(Base):
    __tablename__ = "detection_objects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_uid = Column(String, ForeignKey("prediction_sessions.uid"), nullable=False)
    label = Column(String)
    score = Column(Float)
    box = Column(String)

    prediction_session = relationship(
        "PredictionSession",
        back_populates="detection_objects",
    )
```

If the existing SQLite schema has additional columns, include them in the ORM models. Keep existing column names unless the prompt explicitly asks to rename them.

If existing responses or tests expect `box` as a string, keep it as `String`.

## Database Setup

Create `db.py` with SQLAlchemy engine and session setup.

Baseline `db.py`:

```python
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DB_BACKEND = os.getenv("DB_BACKEND", "sqlite").lower()

DB_USER = os.getenv("DB_USER", "user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "pass")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "predictions")

if DB_BACKEND == "postgres":
    DATABASE_URL = (
        f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )
elif DB_BACKEND == "sqlite":
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./predictions.db")
else:
    raise ValueError(f"Unsupported DB_BACKEND: {DB_BACKEND}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite")
    else {},
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

Add SQLAlchemy table creation to the app startup path or module initialization, following the project style:

```python
from db import engine
from models import Base

Base.metadata.create_all(bind=engine)
```

Do not keep `init_db()` as the main database creation mechanism. Do not keep raw `CREATE TABLE` SQL.

## FastAPI Integration

Every route that reads or writes prediction data must receive a database session through FastAPI dependency injection:

```python
from fastapi import Depends
from sqlalchemy.orm import Session

from db import get_db
from models import PredictionSession


@app.get("/predictions/{uid}")
def get_prediction(uid: str, db: Session = Depends(get_db)):
    prediction = db.query(PredictionSession).filter_by(uid=uid).first()
    ...
```

Do not call `next(get_db())` inside routes. If helper functions need database access, pass `db` into them explicitly.

## Replace Raw SQLite

Remove application persistence patterns like:

```python
import sqlite3
sqlite3.connect(...)
conn.execute(...)
cursor.execute(...)
CREATE TABLE ...
INSERT INTO ...
SELECT ...
UPDATE ...
DELETE ...
DB_PATH
init_db()
```

Use ORM inserts:

```python
prediction = PredictionSession(
    uid=uid,
    original_image=original_image_path,
    predicted_image=predicted_image_path,
)

db.add(prediction)
db.commit()
db.refresh(prediction)
```

Use ORM inserts for detection objects:

```python
detection = DetectionObject(
    prediction_uid=uid,
    label=label,
    score=score,
    box=box,
)

db.add(detection)
```

Use ORM queries:

```python
prediction = db.query(PredictionSession).filter_by(uid=uid).first()

objects = (
    db.query(DetectionObject)
    .filter_by(prediction_uid=uid)
    .all()
)
```

Use one commit per logical API operation. Roll back if a handled database exception occurs:

```python
try:
    db.add(prediction)
    db.commit()
except Exception:
    db.rollback()
    raise
```

## Preserve Existing API Behavior

For existing endpoints, change only persistence internals.

Preserve:

- Route paths
- HTTP methods
- Status codes
- Error behavior
- Response field names
- Response nesting
- Image file behavior
- YOLO inference behavior

For `/predict`, preserve the existing response shape. If the current app uses these keys, keep them exactly:

```python
{
    "prediction_uid": "...",
    "detection_count": 0,
    "labels": [],
    "time_took": 0.0
}
```

If the current files show different keys, preserve the current keys instead.

## Feature: Recent Predictions

For `GET /predictions/recent`, return the 10 most recent sessions using SQLAlchemy:

```python
recent = (
    db.query(PredictionSession)
    .order_by(PredictionSession.timestamp.desc())
    .limit(10)
    .all()
)
```

If the app has a dynamic route like `/predictions/{uid}`, define `/predictions/recent` before the dynamic route so FastAPI does not treat `recent` as a UID.

Use the app's existing response style.

Add tests for:

- Empty database
- More than 10 sessions returns only 10
- Newest session appears first
- No raw SQL or `sqlite3`

## Feature: Delete Prediction Session

For deleting a prediction session and its detection objects, use ORM behavior:

```python
prediction = db.query(PredictionSession).filter_by(uid=uid).first()

if prediction is None:
    ...

db.delete(prediction)
db.commit()
```

The `PredictionSession.detection_objects` relationship should use `cascade="all, delete-orphan"`, or the route should explicitly delete child `DetectionObject` rows through ORM queries.

Do not use raw `DELETE` SQL.

Add tests that verify:

- Existing UID deletes the parent row
- Related detection objects are deleted
- Missing UID returns the existing not-found style

## Feature: Add `processing_time_ms`

For a prompt like `add processing_time_ms`, add the column to `PredictionSession`:

```python
processing_time_ms = Column(Float)
```

Store the value when creating the prediction session. If the app currently measures seconds as `time_took`, convert consistently before storing milliseconds.

Do not expose the new field in existing responses unless the user asks for that.

Add tests that verify the value is persisted.

## Feature: Add `UserFeedback`

For a prompt like `add a UserFeedback table`, add an ORM model linked to `PredictionSession`:

```python
class UserFeedback(Base):
    __tablename__ = "user_feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_uid = Column(String, ForeignKey("prediction_sessions.uid"), nullable=False)
    rating = Column(Integer)
    comment = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)

    prediction_session = relationship("PredictionSession")
```

If adding a feedback endpoint, follow the existing API style. Validate that the prediction UID exists before storing feedback.

Add tests for:

- Feedback can be stored for an existing prediction
- Missing prediction UID returns the correct error
- Existing prediction endpoints keep their old response shapes

## Feature: Search Or Filter Predictions

For prompts like `get prediction by score and label`, use SQLAlchemy joins and filters:

```python
query = db.query(PredictionSession).join(DetectionObject)

if label is not None:
    query = query.filter(DetectionObject.label == label)

if min_score is not None:
    query = query.filter(DetectionObject.score >= min_score)

predictions = query.distinct().all()
```

Choose an endpoint path consistent with the existing app. If no pattern exists, a reasonable route is:

```text
GET /predictions/search?label=person&min_score=0.8
```

Add tests for label-only, score-only, label plus score, and no matches.

## Dependencies

Add dependencies to the existing dependency file, usually `services/yolo/requirements.txt`:

```text
SQLAlchemy
psycopg2-binary
```

Do not introduce a new package manager.

Do not install dependencies yourself in this course repo. Tell the student which install command to run manually.

## Tests

Use the `yolo-api-tests` skill when updating tests.

Tests must:

- Use `pytest`
- Use `fastapi.testclient.TestClient`
- Use a temporary SQLite database through SQLAlchemy
- Create tables with `Base.metadata.create_all(bind=engine)`
- Override FastAPI with `app.dependency_overrides[get_db]`
- Avoid `sqlite3`, `DB_PATH`, raw SQL, and `init_db()`
- Mock YOLO inference for `/predict`
- Assert status codes and response bodies
- Verify persistence through SQLAlchemy queries

## Post-Implementation Test Run

After ALL file changes are complete and no further edits remain, run the tests once:

```bash
pytest tests/
```

**If tests pass:** report the passing output and stop.

**If tests fail:** stop immediately. Do not edit any files. Do not attempt to fix the failures. Report only:

1. Which tests failed (test name and file)
2. The exact error message and traceback
3. The likely cause in one sentence
4. The manual steps the student should take to investigate or fix

Wait for the student to send a new explicit prompt before making any further changes.

## Completion Checklist

Before reporting completion, verify by inspection that:

1. `models.py` exists and defines `Base`, `PredictionSession`, and `DetectionObject`.
2. `db.py` exists and defines `engine`, `SessionLocal`, and generator dependency `get_db()`.
3. Application persistence code does not import `sqlite3`.
4. Application persistence code does not call `sqlite3.connect`.
5. Application persistence code does not use raw SQL strings for prediction data.
6. Application persistence code does not use `DB_PATH`.
7. Application persistence code does not call `init_db()`.
8. Routes that read or write prediction data use `db: Session = Depends(get_db)`.
9. Existing endpoint paths, status codes, and response shapes are preserved.
10. Tests use SQLAlchemy models and `app.dependency_overrides[get_db]`.

Because this course repo asks students to run commands themselves, do not claim tests passed unless the user provides test output.

## Final Response

In the final response, report:

- Files changed
- That raw SQLite persistence was replaced with SQLAlchemy ORM
- Whether SQLite remains the default backend
- How PostgreSQL is configured
- Whether tests were updated
- Exact commands the student should run manually
- Any behavior that was not clear from the task/files

Keep the response short and concrete.
