"""Environment-driven configuration for the Zero-Knowledge Image Vault bot.

All settings come from environment variables (or a local .env file when
python-dotenv is installed).  Nothing here is ever written to disk.
"""
from __future__ import annotations

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # dotenv is optional; env vars also work directly
    pass

# --- Telegram ---------------------------------------------------------------
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
# Channel to post blurred previews to, e.g. "@my_vault_channel" or "-1001234567890"
CHANNEL_ID: str = os.getenv("CHANNEL_ID", "").strip()

# --- Access control ---------------------------------------------------------
# Telegram user id of the owner.  If left empty, the FIRST user to send /start
# to the bot claims ownership (bootstrap mode) and the id is stored in the DB.
OWNER_ID: int = int(os.getenv("OWNER_ID", "0") or 0)
# Optional extra viewer ids, comma/space separated.
ALLOWED_USER_IDS: set[int] = {
    int(x)
    for x in os.getenv("ALLOWED_USER_IDS", "").replace(",", " ").split()
    if x.strip().isdigit()
}
# Join-to-unlock: when CHANNEL_ID is set, ANYONE who joined that channel may
# use the bot (upload, request, view).  Set to 0 to restrict to owner/whitelist.
# The bot must be an admin of the channel for the membership check to work
# (it already has to be, to post previews).
ALLOW_CHANNEL_MEMBERS: bool = os.getenv(
    "ALLOW_CHANNEL_MEMBERS", "1").strip().lower() not in ("0", "false", "no", "off")

# --- Crypto -----------------------------------------------------------------
# Master passphrase.  NEVER stored anywhere.  An Argon2id key derived from it
# wraps every per-image AES key, so a stolen vault.db alone reveals nothing.
MASTER_PASSPHRASE: str = os.getenv("MASTER_PASSPHRASE", "")

# --- Zero-knowledge web viewer ----------------------------------------------
# Public HTTPS base URL of this bot host, e.g. "https://vault.example.com".
# On render.com this is AUTO-DETECTED from RENDER_EXTERNAL_URL, so /secret
# links work with zero configuration.  The decryption key travels ONLY in
# the URL #fragment, which browsers never send to the server.
BASE_URL: str = (os.getenv("BASE_URL", "").strip().rstrip("/")
                 or os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/"))
# Render injects PORT for web services and health-checks it - honour it
# first so the viewer is reachable on Render without any extra config.
WEB_PORT: int = int(os.getenv("PORT") or os.getenv("WEB_PORT") or "8080")
LINK_TTL: int = int(os.getenv("LINK_TTL_SECONDS", "600"))   # link validity window
VIEW_TTL: int = int(os.getenv("VIEW_TTL_SECONDS", "60"))    # in-chat photo lifetime
# Cover the decrypted photo with a tap-to-reveal spoiler on /get (Telegram's
# own view-once-style overlay; the photo still self-deletes after VIEW_TTL).
# True view-once media is not available to bots via the Bot API.
SPOILER_ON_GET: bool = os.getenv(
    "SPOILER_ON_GET", "1").strip().lower() not in ("0", "false", "no", "off")
# Stamp a per-link watermark ("ONE-TIME . <tag>") on the zero-knowledge viewer
# canvas: deterrence for screen capture plus traceability to the link issuance.
WATERMARK_ZK: bool = os.getenv(
    "WATERMARK_ZK", "1").strip().lower() not in ("0", "false", "no", "off")

# --- Storage ----------------------------------------------------------------
DB_PATH: str = os.getenv("DB_PATH", "vault.db")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# Telegram Bot API hard limit for getFile() downloads is 20 MB.
MAX_UPLOAD_BYTES: int = 19 * 1024 * 1024


def _on_render() -> bool:
    """True when running on render.com (Render injects service env vars)."""
    return bool(os.getenv("RENDER_EXTERNAL_URL") or os.getenv("RENDER_SERVICE_NAME"))


def problems() -> list[str]:
    """Non-fatal and fatal configuration issues, for logging at startup."""
    issues: list[str] = []
    if not BOT_TOKEN:
        issues.append("FATAL: BOT_TOKEN is missing (get one from @BotFather).")
    if not MASTER_PASSPHRASE:
        issues.append("FATAL: MASTER_PASSPHRASE is missing "
                      "(the vault cannot wrap image keys without it).")
    elif len(MASTER_PASSPHRASE) < 8:
        issues.append("WARN: MASTER_PASSPHRASE is shorter than 8 characters.")
    if not CHANNEL_ID:
        issues.append("WARN: CHANNEL_ID is not set - blurred previews will not be posted.")
    if not BASE_URL:
        issues.append("WARN: BASE_URL is not set - /secret zero-knowledge links are disabled.")
    if BASE_URL.startswith("http://") and "localhost" not in BASE_URL:
        issues.append("WARN: BASE_URL uses plain http:// - browsers only allow WebCrypto "
                      "(and thus /secret links) on HTTPS or localhost.")
    if _on_render() and not os.path.abspath(DB_PATH).startswith("/var/"):
        issues.append("WARN: Render detected WITHOUT a Persistent Disk - vault.db sits "
                      "on the EPHEMERAL filesystem and will be LOST on every deploy or "
                      "restart. Attach a disk (mount e.g. /var/data) and set "
                      "DB_PATH=/var/data/vault.db, or switch to starter plan.")
    return issues


def validate() -> None:
    """Abort startup when a fatal setting is missing."""
    fatal = [p for p in problems() if p.startswith("FATAL")]
    if fatal:
        raise SystemExit("Refusing to start:\n  - " + "\n  - ".join(fatal))
