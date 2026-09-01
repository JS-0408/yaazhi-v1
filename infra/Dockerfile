# Dockerfile — Yaazhi Personal AI Operating System
# Multi-stage build: slim Python base + only what's needed for production

# ─────────────────────────────────────────────────────────
# Stage 1: dependency builder
# ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# System build dependencies (compiled wheels: psycopg2, chromadb, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libpq-dev \
    portaudio19-dev \
    ffmpeg \
    git \
    curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --prefix=/install --no-cache-dir -r requirements.txt

# ─────────────────────────────────────────────────────────
# Stage 2: production image
# ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS production

LABEL maintainer="Santhosh <santhosh.25bes7017@vitapstudent.ac.in>"
LABEL version="1.0"
LABEL description="Yaazhi — Personal AI Operating System"

# Runtime system deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    portaudio19-dev \
    ffmpeg \
    curl \
 && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash yaazhi
USER yaazhi
WORKDIR /app

# Copy source code
COPY --chown=yaazhi:yaazhi . .

# Create required runtime directories
RUN mkdir -p \
    /app/knowledge/btech_notes \
    /app/knowledge/ieee_papers \
    /app/knowledge/projects \
    /app/logs \
    /app/data/chroma \
    /app/data/uploads

# Environment defaults (overridden by docker-compose .env)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    YAAZHI_ENV=production \
    API_HOST=0.0.0.0 \
    API_PORT=8000

# Health check — hits the FastAPI /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${API_PORT}/health || exit 1

# Expose FastAPI port
EXPOSE 8000

# D-01 FIX: workers=1 — PyodideSandbox WASM runtime cannot be safely forked
# across uvicorn worker processes. Single worker + uvloop achieves equivalent
# async throughput for this workload on ARM64 (4 cores, 24GB RAM).
CMD ["python", "-m", "uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--log-level", "info"]
