# syntax=docker/dockerfile:1

# --- Stage 1: Builder ---
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system

# Install Git (required by uv to fetch private HTTPS dependencies via .netrc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /cop_ard_processor

# Copy configuration files for dependency layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies using BuildKit secret mount for .netrc authentication
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=netrc,target=/root/.netrc \
    uv sync --frozen --extra cluster-plugin --no-install-project --no-editable

# Copy repository source code
COPY . .

# Final project sync
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=netrc,target=/root/.netrc \
    uv sync --frozen --extra cluster-plugin --no-editable

# Download essential Earth Observation geoid grids (EGM2008 + EGM96)
RUN uv run python -c "\
import pyproj; \
from cop_ard_app.utils import download_geoid_grid; \
target = pyproj.datadir.get_data_dir(); \
grids = ['us_nga_egm08_25.tif', 'us_nga_egm96_15.tif']; \
[download_geoid_grid(grid_name=g, target_dir=target) for g in grids]; \
"

# --- Stage 2: Runtime ---
FROM python:3.13-slim AS runner

# Install system C libraries required by EOPF readers
RUN apt-get update && apt-get install -y --no-install-recommends \
    libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root system user and group for production container hardening
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser

WORKDIR /cop_ard_processor

ARG CI_COMMIT_SHA
LABEL org.opencontainers.image.revision="$CI_COMMIT_SHA" \
      org.opencontainers.image.vendor="ESA" \
      org.opencontainers.image.authors="B-Open"

# Copy application directory and set permissions for non-root user
COPY --from=builder --chown=appuser:appgroup /cop_ard_processor /cop_ard_processor

# Switch to non-root user
USER appuser

# Automatically activate virtual environment
ENV PATH="/cop_ard_processor/.venv/bin:$PATH"
