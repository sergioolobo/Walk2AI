# syntax=docker/dockerfile:1
#
# Walk2AI backend image. Build context: the backend/ directory.
#
#   docker build -f infrastructure/docker/backend.Dockerfile -t walk2ai-backend backend
#
# Two stages: dependencies are resolved from the lockfile in a builder, and only the
# virtualenv and application code reach the runtime image, which runs as a non-root user.

ARG PYTHON_VERSION=3.13

# ---------------------------------------------------------------------- builder
FROM python:${PYTHON_VERSION}-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml uv.lock ./

# Exactly the locked dependencies, without development tools.
#
# The optional "extra_ca" build secret is only for building behind a TLS-inspecting
# proxy (pass: --secret id=extra_ca,src=/path/to/ca-bundle.crt). Normal builds do not
# need it and nothing from it is stored in any image layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then \
        export SSL_CERT_FILE=/run/secrets/extra_ca PIP_CERT=/run/secrets/extra_ca; \
    fi \
    && pip install --no-cache-dir uv==0.11.32 \
    && uv sync --frozen --no-dev

# ---------------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim AS runtime

RUN groupadd --system --gid 10001 walk2ai \
    && useradd --system --uid 10001 --gid walk2ai --home-dir /app --shell /usr/sbin/nologin walk2ai

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER walk2ai
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
