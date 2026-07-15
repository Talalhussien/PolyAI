# ── Stage 1: builder ────────────────────────────────────────────────────────
FROM python:3.12-slim-trixie AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1-mesa-dev libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Create a self-contained venv outside /app so it copies cleanly to the next stage
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only requirement files first — pip layers are cached until these change
COPY torch-requirements.txt requirements.txt ./
RUN pip install --no-cache-dir -r torch-requirements.txt \
 && pip install --no-cache-dir -r requirements.txt

# ── Stage 2: runner ─────────────────────────────────────────────────────────
FROM python:3.12-slim-trixie AS runner

# System libs needed by OpenCV at runtime + curl for HEALTHCHECK
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1-mesa-dev libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

# Copy only the installed venv — no pip, no build tooling
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Application code copied last — changes here do not invalidate dependency layers
COPY . .

RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
