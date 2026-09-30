# ── Stage 1: build the React frontend ────────────────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
# Production build calls the API on the same origin (VITE_API_BASE unset -> "")
RUN npm run build


# ── Stage 2: Python deps (wheels) ────────────────────────────────────────────
FROM python:3.11-slim AS pydeps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt


# ── Stage 3: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# ASTAP command-line solver. Overridable if the upstream URL changes.
ARG ASTAP_CLI_URL=https://sourceforge.net/projects/astap-program/files/linux_installer/astap_command-line_version_Linux_amd64.zip/download

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget unzip ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install ASTAP CLI to /usr/local/bin/astap
RUN set -eux; \
    wget -O /tmp/astap_cli.zip "${ASTAP_CLI_URL}"; \
    unzip -o /tmp/astap_cli.zip -d /tmp/astap; \
    bin="$(find /tmp/astap -type f -iname 'astap_cli' -o -type f -iname 'astap' | head -n1)"; \
    cp "$bin" /usr/local/bin/astap; \
    chmod +x /usr/local/bin/astap; \
    rm -rf /tmp/astap /tmp/astap_cli.zip; \
    /usr/local/bin/astap || true

COPY --from=pydeps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY backend/ /app/
COPY --from=frontend /build/dist /app/static

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/config \
    STATIC_DIR=/app/static \
    ASTAP_PATH=/usr/local/bin/astap \
    ASTAP_DB_DIR=/config/astap_db \
    # Pin math-library threading so each job uses ONE core. Parallelism is then
    # controlled by WORKER_CONCURRENCY × worker replicas — predictable, doesn't
    # fan numpy/OpenBLAS across every core and swamp the NAS.
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    VECLIB_MAXIMUM_THREADS=1

VOLUME ["/config"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD wget -qO- http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
