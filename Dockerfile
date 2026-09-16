# syntax=docker/dockerfile:1

# --- Stage 1: Builder ---
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system

# Install Git and OpenSSH client for private GitHub dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p -m 0700 /root/.ssh \
    && ssh-keyscan github.com >> /root/.ssh/known_hosts

WORKDIR /cop_ard_processor

# Copy configuration files for dependency layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies via SSH (including cluster-plugin for Bokeh/Dask Dashboard)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=ssh \
    uv sync --frozen --extra cluster-plugin --no-install-project --no-editable

# Copy full repository source code
COPY . .

# Perform final project sync
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=ssh \
    uv sync --frozen --extra cluster-plugin --no-editable

# Download PROJ grid directly into PyPROJ's internal data directory after all syncs complete
RUN .venv/bin/python -c "\
import urllib.request, os, pyproj; \
datadir = pyproj.datadir.get_data_dir(); \
url = 'https://cdn.proj.org/us_nga_egm08_25.tif'; \
dest = os.path.join(datadir, 'us_nga_egm08_25.tif'); \
print(f'Downloading {url} to {dest}...'); \
urllib.request.urlretrieve(url, dest); \
print('Download complete.'); \
aliases = ['Und_min1x1_egm2008_isw=82_WGS84_TideFree', 'us_nga_egm2008_1.tif', 'us_nga_egm08_1.tif']; \
[os.symlink('us_nga_egm08_25.tif', os.path.join(datadir, a)) for a in aliases if not os.path.exists(os.path.join(datadir, a))]; \
"

# --- Stage 2: Runtime ---
FROM python:3.13-slim AS runner

# Install system C libraries required by EOPF readers
RUN apt-get update && apt-get install -y --no-install-recommends \
    libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /cop_ard_processor

ARG CI_COMMIT_SHA
LABEL org.opencontainers.image.revision="$CI_COMMIT_SHA" \
      org.opencontainers.image.vendor="ESA" \
      org.opencontainers.image.authors="B-Open"

# Copy application directory including virtual environment and PROJ grids
COPY --from=builder /cop_ard_processor /cop_ard_processor

# Automatically activate virtual environment
ENV PATH="/cop_ard_processor/.venv/bin:$PATH"
