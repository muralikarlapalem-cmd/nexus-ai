# ─────────────────────────────────────────────────────────────
#  NEXUS AI Backend  ·  Dockerfile
#  Multi-stage: deps → builder → runtime
# ─────────────────────────────────────────────────────────────

# ── Stage 1: dependency install ───────────────────────────────
FROM python:3.12-slim AS deps

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# CPU-only torch first (~180 MB). The reranker runs on the CPU; without this,
# sentence-transformers pulls the CUDA build (several GB of GPU libraries)
# and a rebuild took over an hour on a slow connection.
# It's its own layer, before requirements.txt is copied, so a change to
# requirements.txt doesn't download torch again.
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.14.0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Non-root user for security
RUN groupadd -r nexus && useradd -r -m -g nexus nexus

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Runtime system deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Download the reranker model into the image (~90 MB). Otherwise it was
# downloaded from Hugging Face during the first query after every container
# re-create, which made that query take ~30s longer. Must match
# RERANKER_MODEL / rag_settings.reranker_model.
ARG RERANKER_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
USER nexus
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('${RERANKER_MODEL}', device='cpu')"
USER root

# Copy application code
COPY . .

RUN chown -R nexus:nexus /app
USER nexus

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health || exit 1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
