# ── Build stage: install dependencies ───────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Split"
LABEL org.opencontainers.image.description="Shared Expense Calculator"

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY app/ ./

# Data directory for SQLite (will be a Swarm volume mount)
RUN mkdir -p /data

# We removed the addgroup/adduser stuff here.
# By default, Docker runs everything as root.

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/api/auth/me | grep -q "401" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]