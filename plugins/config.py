import os
import logging

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
    level=logging.INFO,
)


def _str_to_bool(val, default=False):
    if not val:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on", "y")


class Config:
    # ── Telegram ──────────────────────────────────────
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
    API_ID: int = int(os.environ.get("API_ID", 0) or 0)
    API_HASH: str = os.environ.get("API_HASH", "")
    BOT_USERNAME: str = os.environ.get("BOT_USERNAME", "UrlUploaderBot")

    # ── Owner / Admins ────────────────────────────────
    OWNER_ID: int = int(os.environ.get("OWNER_ID", 0) or 0)
    ADMIN: set = set(
        int(x) for x in os.environ.get("ADMIN", "").split() if x.isdigit()
    )
    BANNED_USERS: set = set(
        int(x) for x in os.environ.get("BANNED_USERS", "").split() if x.isdigit()
    )

    # ── Channels ──────────────────────────────────────
    LOG_CHANNEL: int = int(os.environ.get("LOG_CHANNEL", 0) or 0)
    UPDATES_CHANNEL: str = os.environ.get("UPDATES_CHANNEL", "")

    # ── Database ──────────────────────────────────────
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

    # ── File handling ─────────────────────────────────
    DOWNLOAD_LOCATION: str = os.path.abspath(
        os.environ.get("DOWNLOAD_LOCATION", "./DOWNLOADS")
    )
    MAX_FILE_SIZE: int = 2_097_152_000          # ~2 GB (Pyrogram MTProto limit)
    CHUNK_SIZE: int = int(os.environ.get("CHUNK_SIZE", 10240)) * 1024  # KB → bytes

    # ── Misc ──────────────────────────────────────────
    LOGGER = logging
    DEF_WATER_MARK_FILE: str = "@" + BOT_USERNAME
    PROCESS_MAX_TIMEOUT: int = 3600
    SESSION_STRING: str = os.environ.get("SESSION_STRING", "")  # optional premium session for 4 GB
    COOKIES_FILE: str = os.environ.get("COOKIES_FILE", "cookies.txt")
    PROXY: str = os.environ.get("PROXY", "")
    FFMPEG_PATH: str = os.environ.get("FFMPEG_PATH", "ffmpeg")
    SESSION_NAME: str = "url_uploader_bot"

    # ── External API endpoints ────────────────────────
    # Cobalt API for social media downloads (Instagram, TikTok, Facebook, etc.).
    # Default is the public `dwnld.nichind.dev` cluster, which is itself a
    # meta-aggregator that tries ~17 internal cobalt instances and returns
    # the first successful result. This is the most reliable free option.
    # Override by setting COBALT_API_URL to your own self-hosted instance.
    COBALT_API_URL: str = os.environ.get(
        "COBALT_API_URL", "https://dwnld.nichind.dev"
    )
    # Comma-separated list of fallback cobalt URLs to try if the primary
    # COBALT_API_URL fails. Example:
    #   COBALT_API_FALLBACKS=https://cobalt-api.example1.com,https://cobalt-api.example2.com
    # If empty, we auto-add the default `dwnld.nichind.dev` cluster as a
    # fallback so the bot is resilient even if the primary URL is a dead
    # koyeb/railway instance that sleeps when idle.
    _user_fallbacks: list = [
        u.strip() for u in os.environ.get("COBALT_API_FALLBACKS", "").split(",")
        if u.strip()
    ]
    _default_fallbacks: list = ["https://dwnld.nichind.dev"]
    COBALT_API_FALLBACKS: list = _user_fallbacks if _user_fallbacks else _default_fallbacks
    LINK_API_URL: str = os.environ.get(
        "LINK_API_URL", "https://native-serene-maduranga11-43790d26.koyeb.app"
    )
    ALLOW_BOT_URL_UPLOAD: bool = _str_to_bool(
        os.environ.get("ALLOW_BOT_URL_UPLOAD", "True"), default=True
    )
    ADSGRAM_BLOCK_ID = os.environ.get("ADSGRAM_BLOCK_ID", "int-23574")

    # ── Ad / sponsor removal ───────────────────────────
    # Comma-separated list of SponsorBlock categories to strip out of the
    # downloaded file. "default" = sponsor, selfpromo, interaction, intro,
    # outro, preview, music_offtopic. Set to "" or "none" to disable.
    # See https://github.com/yt-dlp/yt-dlp#sponsorblock-options
    SPONSORBLOCK_REMOVE: str = os.environ.get(
        "SPONSORBLOCK_REMOVE", "default"
    )
    # Optional custom SponsorBlock API endpoint (defaults to the public one).
    SPONSORBLOCK_API: str = os.environ.get("SPONSORBLOCK_API", "")

    # ── YouTube policy ─────────────────────────────────
    # The Mini App endpoints (/api/formats, /api/download) currently block
    # YouTube to keep the bot on the safe side of ToS. Set ALLOW_YOUTUBE=true
    # to enable YouTube downloads through the bot. Make sure you have a
    # valid cookies.txt and that you comply with YouTube's ToS in your
    # jurisdiction before enabling this.
    ALLOW_YOUTUBE: bool = _str_to_bool(
        os.environ.get("ALLOW_YOUTUBE", "false"), default=False
    )

    # ── Render / PaaS deployment knobs ────────────────
    # Render injects PORT; fall back to 8080 for local dev.
    PORT: int = int(os.environ.get("PORT", 8080) or 8080)

    # Public base URL of this deployment (used to build the Telegram WebApp URL).
    # Priority:
    #   1. Explicit WEBAPP_URL env var (highest priority)
    #   2. RENDER_EXTERNAL_URL (auto-injected by Render — zero-config!)
    #   3. Empty string (warning shown at startup)
    WEBAPP_URL: str = (
        os.environ.get("WEBAPP_URL", "").rstrip("/")
        or os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    )

    # True when running on Render (Render injects RENDER=true at build/runtime).
    IS_RENDER: bool = _str_to_bool(os.environ.get("RENDER", "false"), default=False)

    # ── Optional heavy services (disabled by default on Render free tier) ──
    # Each of these consumes significant RAM and is not strictly required
    # for the core download/upload flow.
    ENABLE_PLAYWRIGHT: bool = _str_to_bool(
        os.environ.get("ENABLE_PLAYWRIGHT", "false"), default=False
    )
    ENABLE_PO_TOKEN_SERVER: bool = _str_to_bool(
        os.environ.get("ENABLE_PO_TOKEN_SERVER", "false"), default=False
    )
    ENABLE_ARIA2: bool = _str_to_bool(
        os.environ.get("ENABLE_ARIA2", "false"), default=False
    )
    # Self-ping the /health endpoint every N seconds to mitigate Render's
    # 15-minute inactivity sleep. Set KEEP_ALIVE_INTERVAL=0 to disable.
    # Default: 600s (10 min) only on Render; off by default elsewhere.
    KEEP_ALIVE_INTERVAL: int = int(
        os.environ.get("KEEP_ALIVE_INTERVAL", "600" if IS_RENDER else "0") or 0
    )
