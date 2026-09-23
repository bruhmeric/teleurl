# ─────────────────────────────────────────────────────────────────────────────
# URL Uploader Bot — Render-optimized Dockerfile
#
# Design goals (Render free tier constraints):
#   * 15-min build time limit   → 2-stage install, no Playwright/Node by default
#   * 512 MB RAM                 → heavy services (Playwright, aria2, Node PO token
#                                  server) are optional via env vars and default OFF
#   * Dynamic PORT               → read from $PORT (Config.PORT, default 8080)
#
# To enable heavy services on a paid Render plan, set:
#   ENABLE_PLAYWRIGHT=true
#   ENABLE_PO_TOKEN_SERVER=true
#   ENABLE_ARIA2=true
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# ── System deps ──────────────────────────────────────────────────────────────
#   - ffmpeg / ffprobe : required for video metadata + HLS/DASH downloads
#   - aria2            : optional fast downloader (toggle via ENABLE_ARIA2)
#   - curl, git, gcc   : needed to build some Python wheels
#   - Node.js          : only installed if BUILD_NODE=1 (for PO token server)
#   - Chromium deps    : only installed if BUILD_CHROMIUM=1 (for Playwright)
ARG BUILD_NODE=0
ARG BUILD_CHROMIUM=0

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    git \
    gcc \
    curl \
    ca-certificates \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Conditionally install Node.js for the PO Token server
RUN if [ "$BUILD_NODE" = "1" ]; then \
        curl -sL https://deb.nodesource.com/setup_18.x | bash - \
        && apt-get install -y nodejs; \
    fi

# Conditionally install Chromium system deps for Playwright
RUN if [ "$BUILD_CHROMIUM" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            libnss3 \
            libatk1.0-0 \
            libatk-bridge2.0-0 \
            libcups2 \
            libdrm2 \
            libdbus-1-3 \
            libxkbcommon0 \
            libxcomposite1 \
            libxdamage1 \
            libxfixes3 \
            libxrandr2 \
            libgbm1 \
            libasound2 \
            libpango-1.0-0 \
            libpangocairo-1.0-0 \
            fonts-liberation \
            xvfb \
        && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

# ── Python deps ─────────────────────────────────────────────────────────────
# Ensure pip is up to date
RUN python3 -m pip install --upgrade pip

# Install core deps first (always needed)
# We do NOT pin FastAPI/uvicorn/yt-dlp to latest at runtime; pinned versions
# here match what the Dockerfile originally installed for stability.
RUN python3 -m pip install --no-cache-dir \
    "fastapi>=0.110,<0.115" \
    "uvicorn[standard]>=0.29,<0.31" \
    "pydantic>=2.6,<3" \
    httpx \
    python-multipart \
    "pyroblack>=2.3" \
    tgcrypto \
    "aiohttp==3.9.5" \
    "aiofiles==23.2.1" \
    "motor==3.4.0" \
    "pymongo==4.7.3" \
    "dnspython==2.6.1" \
    "psutil==5.9.8" \
    "filetype==1.2.0" \
    "Pillow==10.3.0" \
    "requests==2.32.3" \
    "yt-dlp" \
    "python-dotenv==1.0.1" \
    aria2p \
    "waitress==3.0.0"

# Conditionally install Playwright + Chromium browser
RUN if [ "$BUILD_CHROMIUM" = "1" ]; then \
        python3 -m pip install --no-cache-dir playwright \
        && python3 -m playwright install chromium; \
    fi

# ── App files ───────────────────────────────────────────────────────────────
COPY package.json package-lock.json* ./
COPY po_server.js dummy_server.py ./
COPY requirements.txt ./
COPY plugins/ ./plugins/
COPY utils/ ./utils/
COPY web/ ./web/
COPY app.py bot.py ./
COPY cookies.txt* ./
COPY .env.example* ./

# Ensure DOWNLOADS directory exists at runtime
RUN mkdir -p DOWNLOADS

# ── Runtime env ─────────────────────────────────────────────────────────────
ENV FFMPEG_PATH=/usr/bin/ffmpeg
ENV PYTHONUNBUFFERED=1
# Render injects PORT automatically; expose it for documentation only.
EXPOSE 8080

# NOTE: No Dockerfile HEALTHCHECK — Render's web service health check is
# configured in the dashboard (or via render.yaml's healthCheckPath).
# Having both can cause Render's blueprint sync to fail.

# Start everything via bot.py — it spawns Uvicorn for the FastAPI mini-app
CMD ["python3", "bot.py"]
