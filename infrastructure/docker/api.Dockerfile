# --- Remotion engine image ---------------------------------------------------
# The video engine needs Node 18+ with the Remotion packages installed, and it shells out to
# `node render.mjs`. It also needs Chromium's shared libraries, because Remotion launches a
# headless browser to compose frames. This stage is shared by the API (which renders in-process by
# default) and the dedicated render worker.
FROM node:22-bookworm-slim AS media

# Chromium runtime dependencies for Remotion's headless shell plus fonts, so serif/sans captions do
# not render as empty boxes. `fonts-liberation` supplies the Liberation Serif/Sans faces that stand
# in for Georgia / Arial in LLM-authored code scenes, so authored type stays readable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        fonts-dejavu-core fonts-liberation fonts-liberation2 fonts-noto-color-emoji \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
        libcairo2 libatspi2.0-0 libx11-6 libx11-xcb1 libxcb1 libxext6 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/remotion
COPY services/media-engine/remotion/package.json services/media-engine/remotion/package-lock.json ./
# `--include=dev` is explicit: some build hosts export NODE_ENV=production, which would silently
# skip the type-checking toolchain. The runtime engine needs every listed dependency, GSAP included.
RUN npm ci --no-audit --no-fund --include=dev

# Remotion downloads a headless Chromium on first use. Doing it at build time keeps the first
# render from timing out on a cold container, and the cache lives with the engine.
RUN npx remotion browser ensure

COPY services/media-engine/remotion/tsconfig.json ./
COPY services/media-engine/remotion/src ./src
COPY services/media-engine/remotion/render.mjs ./render.mjs
# Vendor staging (GSAP for code scenes) and the engine self-checks used by container_check.py.
COPY services/media-engine/remotion/assets.mjs ./assets.mjs
COPY services/media-engine/remotion/scripts ./scripts

# Fail the build, not the first render, if the engine is broken: vendor assets (GSAP) must exist,
# staging must work, and the compositions (including code scenes) must bundle. A full frame render
# is exercised by infrastructure/docker/container_check.py, which is faster to run than to bake.
RUN node scripts/verify-engine.mjs


# --- API ---------------------------------------------------------------------
FROM python:3.12-slim AS api

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    REMOTION_DIR=/opt/remotion \
    REMOTION_GL=swangle

# ffprobe inspects finished renders (read-only). Composition and encoding belong to Remotion.
# The Chromium libraries are required for the engine's headless browser inside this image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates \
        fonts-dejavu-core fonts-liberation fonts-liberation2 fonts-noto-color-emoji \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
        libcairo2 libatspi2.0-0 libx11-6 libx11-xcb1 libxcb1 libxext6 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Node runtime for the Remotion CLI (same major version as the engine image).
COPY --from=media /usr/local/bin/node /usr/local/bin/node
COPY --from=media /usr/local/lib/node_modules /usr/local/lib/node_modules

WORKDIR /app
COPY apps/api/pyproject.toml ./pyproject.toml
COPY apps/api/app ./app
RUN pip install ".[temporal]" \
    && useradd --create-home --uid 10001 factory \
    && mkdir -p /app/storage && chown -R factory:factory /app/storage
COPY apps/api/alembic ./alembic
COPY apps/api/alembic.ini ./alembic.ini
# Operator scripts that are meant to run inside the container (retry, diagnostics).
COPY apps/api/scripts ./scripts
# Container-level smoke test: `python /app/infrastructure/docker/container_check.py`.
COPY infrastructure/docker/container_check.py ./infrastructure/docker/container_check.py

# The engine, its node_modules and the shared Chromium cache. Owned by root and only read at
# runtime, which is fine for the unprivileged user. Do NOT chown/chmod recursively here: rewriting
# ownership of thousands of node_modules files takes ~15 minutes on Docker Desktop for Windows and
# is the single slowest step in this build. Only the browser cache must be writable.
COPY --from=media /opt/remotion /opt/remotion
RUN mkdir -p /var/tmp/factory \
    && chown factory:factory /var/tmp/factory
# Temporary render staging (assets, stills) and the browser cache live under a writable path.
ENV TMPDIR=/var/tmp/factory \
    REMOTION_BROWSER_CACHE=/var/tmp/factory/remotion

USER factory
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/healthz || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
