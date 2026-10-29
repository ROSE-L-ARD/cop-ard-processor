# syntax=docker/dockerfile:1

# ==============================================================================
# Stage 1: Builder
# ==============================================================================
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system

# Install Git (required by uv for fetching private Git dependencies)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /cop_ard_processor

# Copy dependency specifications first to leverage Docker layer caching
COPY pyproject.toml uv.lock ./

# Pre-install dependencies without local package code
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra cluster-plugin --no-install-project --no-editable

# Copy repository source code
COPY . .

# Final project synchronization into .venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra cluster-plugin --no-editable

# Download essential Earth Observation geoid grids directly into venv PyPROJ data directory
RUN uv run python -c "\
import pyproj; \
from cop_ard_app.utils import download_geoid_grid; \
target = pyproj.datadir.get_data_dir(); \
grids = ['us_nga_egm08_25.tif', 'us_nga_egm96_15.tif']; \
[download_geoid_grid(grid_name=g, target_dir=target) for g in grids]; \
"

# Grant read/execute permissions to all files so any host UID can execute them
RUN chmod -R a+rX /cop_ard_processor


# ==============================================================================
# Stage 2: Runtime
# ==============================================================================
FROM python:3.13-slim AS runner

# Install underlying system runtime libraries required by EOPF
RUN apt-get update && apt-get install -y --no-install-recommends \
    libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# Create unprivileged application user and group (UID/GID 10001)
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser

WORKDIR /cop_ard_processor

ARG CI_COMMIT_SHA
LABEL org.opencontainers.image.revision="$CI_COMMIT_SHA" \
      org.opencontainers.image.vendor="ESA" \
      org.opencontainers.image.authors="B-Open"

COPY --from=builder --chown=appuser:appgroup /cop_ard_processor /cop_ard_processor

# Environment Configuration:
# - PATH: Activates the virtual environment directly
# - PYTHONUNBUFFERED: Flushes stdout/stderr immediately for real-time logs
# - *_DIR: Redirects runtime caches to /tmp so arbitrary UIDs never hit permission errors
ENV PATH="/cop_ard_processor/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    NUMBA_CACHE_DIR=/tmp \
    MPLCONFIGDIR=/tmp 

# Switch to default non-root user
USER appuser
