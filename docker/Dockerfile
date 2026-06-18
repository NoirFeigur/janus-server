# syntax=docker/dockerfile:1
# Janus Server image — one image, three roles (app / arq-worker / arq-scheduler).
# The role is chosen by the compose `command`, not by separate images: a single
# build artifact keeps "modular monolith, same code, different start args" honest
# (README: 按角色拆进程,同一份代码换启动参数).
#
# Multi-stage with uv: the builder resolves the locked dependency set into a
# self-contained venv; the runtime stage copies only that venv + source, so the
# final image carries no build toolchain. uv.lock is the source of truth (frozen
# install — never re-resolve at build time, supply-chain reproducibility).

# ---- builder: resolve the locked venv -------------------------------------
FROM python:3.11-slim AS builder

# uv from its official distroless image (pinned tag = reproducible toolchain).
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Layer the dependency install separately from the source: only re-resolve when
# the lock/manifest changes, not on every source edit. --frozen fails if the
# lockfile is stale (no silent drift), --no-install-project installs only deps.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Now bring in the source and install the project itself into the same venv.
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime: copy the venv + source, drop the toolchain ------------------
FROM python:3.11-slim AS runtime

# Run as an unprivileged user (never root in the container).
RUN groupadd --system janus && useradd --system --gid janus --no-create-home janus

WORKDIR /app

# The resolved venv and the application source from the builder.
COPY --from=builder --chown=janus:janus /app/.venv /app/.venv
COPY --from=builder --chown=janus:janus /app/src /app/src
COPY --from=builder --chown=janus:janus /app/alembic /app/alembic
COPY --from=builder --chown=janus:janus /app/alembic.ini /app/alembic.ini

# Put the venv on PATH so `uvicorn` / `arq` resolve without `uv run`.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER janus

EXPOSE 8000

# Default role = the web app. compose overrides `command` for worker/scheduler.
# --proxy-headers + --forwarded-allow-ips: replicas sit behind nginx, so honor
# X-Forwarded-* (real client IP for logging / throttling). Bind 0.0.0.0 (in-net).
CMD ["uvicorn", "src.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
