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

# Non-root user
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /data /app

USER appuser

EXPOSE 8080

# Health: a 401 on /api/auth/me means the app is up (unauthenticated = expected)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://localhost:8080/api/auth/me',timeout=3); sys.exit(0)" 2>&1 || \
  python -c "import urllib.request,sys; \
    try: urllib.request.urlopen('http://localhost:8080/api/auth/me',timeout=3) \
    except urllib.error.HTTPError as e: sys.exit(0 if e.code==401 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
