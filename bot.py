import os
import subprocess
import sys
import threading
import asyncio
import time

# ── CRITICAL: pin a single asyncio event loop BEFORE any pyrogram import ──
# Pyrogram's Client.__init__ captures asyncio.get_event_loop() at instantiation
# time (utils/shared.py creates the Client at import time). If we later use
# asyncio.run(main()) it creates a *new* loop, and pyrogram's executor ends
# up bound to the old one → "Future attached to a different loop" RuntimeError.
#
# Fix: create and set the event loop explicitly here, then later run main() on
# the SAME loop with loop.run_until_complete() instead of asyncio.run().
_MAIN_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_MAIN_LOOP)

from plugins.config import Config
import platform
import zipfile
import urllib.request
import atexit
from pyrogram import Client, idle, filters
import app  # noqa: F401  (registers FastAPI routes & lifecycle hooks)

from utils.shared import bot_client, WEBAPP_PROGRESS

# Register a global ping handler for diagnostics
@bot_client.on_message(filters.command("ping") & filters.private)
async def ping_handler(client, message):
    print(f"📥 Received /ping from {message.from_user.id} at {time.time()}")
    await message.reply_text("🏓 Pong! Bot is alive and well.")


def run_health_server():
    """Run the FastAPI app via Uvicorn in a daemon thread.
    Uses Config.PORT so it works on Render (dynamic PORT) and locally.
    """
    from app import app as api_app
    import uvicorn
    print(f"🌍 Starting FastAPI health & progress server on 0.0.0.0:{Config.PORT} ...")
    uvicorn.run(api_app, host="0.0.0.0", port=Config.PORT, log_level="info")


def setup_po_token_server():
    """
    Ensure the Node.js PO Token server dependencies are installed dynamically
    so we don't need to manually check-in node_modules to GitHub.
    Only run when ENABLE_PO_TOKEN_SERVER=true (default: false on Render free tier
    to save RAM).
    """
    import shutil
    if not Config.ENABLE_PO_TOKEN_SERVER:
        print("ℹ️  PO Token server disabled (set ENABLE_PO_TOKEN_SERVER=true to enable).")
        return None

    if not shutil.which("npm"):
        print("⚠️ Warning: 'npm' not found. Skipping Node.js PO Token server setup.")
        return None

    if not os.path.exists("package.json"):
        print("📦 Initializing package.json for PO Token server...")
        subprocess.run(["npm", "init", "-y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if not os.path.exists("node_modules/youtube-po-token-generator") or not os.path.exists("node_modules/express"):
        print("📦 Installing Express and YouTube PO Token Generator dependencies...")
        subprocess.run(
            ["npm", "install", "express", "youtube-po-token-generator"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print("✅ Node.js dependencies installed.")

    return "po_server.js"


def maybe_start_aria2():
    """Start the aria2c RPC daemon if ENABLE_ARIA2=true (default: false on Render)."""
    if not Config.ENABLE_ARIA2:
        print("ℹ️  aria2c disabled (set ENABLE_ARIA2=true to enable).")
        return
    if not shutil.which("aria2c"):
        print("⚠️ aria2c not installed; skipping daemon.")
        return
    try:
        aria_cmd = [
            "aria2c",
            "--enable-rpc",
            "--rpc-listen-all=true",
            "--rpc-allow-origin-all=true",
            "--max-connection-per-server=16",
            "--split=16",
            "--min-split-size=1M",
            "--max-overall-download-limit=0",
            "--file-allocation=none",
            "--max-concurrent-downloads=100",
            "-D"
        ]
        subprocess.Popen(aria_cmd)
        print("✅ aria2c daemon started with optimized high-concurrency flags.")
    except Exception as e:
        print(f"⚠️ Failed to start aria2c daemon: {e}")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("🚀  URL Uploader Bot — Starting…")
    print("=" * 60 + "\n")

    # ── Validate required environment variables ──────────────────────────
    missing = []
    if not Config.BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not Config.API_ID:
        missing.append("API_ID")
    if not Config.API_HASH:
        missing.append("API_HASH")
    if missing:
        print(f"❌ FATAL: Missing required environment variables: {', '.join(missing)}")
        print("   Set them in .env, in your Render dashboard, or via `export`.")
        sys.exit(1)

    if not Config.WEBAPP_URL:
        print(
            "⚠️  WARNING: WEBAPP_URL is not set and RENDER_EXTERNAL_URL was not "
            "auto-injected. The Mini App launch button in /start will not work. "
            "On Render, this is set automatically after the first deploy."
        )
    else:
        print(f"🌐 Mini App URL: {Config.WEBAPP_URL}")
        if Config.IS_RENDER:
            print("🚀 Running on Render — keep-alive enabled by default.")

    # Ensure download folder exists and is clean on startup
    if os.path.exists(Config.DOWNLOAD_LOCATION):
        import shutil
        try:
            shutil.rmtree(Config.DOWNLOAD_LOCATION)
            print("🧹 Cleaned old DOWNLOADS folder on startup.")
        except Exception as e:
            print(f"⚠️ Could not clean DOWNLOADS folder: {e}")
    os.makedirs(Config.DOWNLOAD_LOCATION, exist_ok=True)

    # Handle cookies from environment variable (useful for cloud deploys)
    # Cloud env vars may store newlines as literal \n — convert them
    cookies_data = os.environ.get("COOKIES_DATA", "")
    if cookies_data:
        cookies_data = cookies_data.replace("\\n", "\n")
        try:
            with open(Config.COOKIES_FILE, "w", encoding="utf-8") as f:
                f.write(cookies_data)
            print(f"🍪 Cookies written to {Config.COOKIES_FILE} from COOKIES_DATA env var.")
        except Exception as e:
            print(f"❌ Failed to write cookies file: {e}")

    # ── Start Background Services ──────────────────────────────────────────

    # PO Token server (optional, off by default)
    print("🚀 Starting youtube-po-token-generator (Node.js) server...")
    po_script = setup_po_token_server()
    pot_process = None
    if po_script and os.path.exists(po_script):
        try:
            pot_cmd = ["node", po_script]
            pot_process = subprocess.Popen(
                pot_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            print("✅ Node.js PO Token server started on port 4416.")
            atexit.register(lambda: pot_process.terminate() if pot_process else None)
        except Exception as e:
            print(f"⚠️ Failed to start Node.js PO Token server: {e}")

    # Aria2c daemon (optional, off by default)
    maybe_start_aria2()

    # Start FastAPI health server in background thread (required by Render/Koyeb)
    # Health check returns 503 only during shutdown (see app.py); it returns 200
    # even during startup so Render's deploy probe passes quickly.
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print(f"🌐 Health server started on port {Config.PORT}")

    # ── Lifecycle: start → mark healthy → idle → shutdown ────────────────
    async def main():
        print("🔧 Initializing main coroutine...")
        print("🔗 Connecting bot client...")
        await bot_client.start()
        print("✅ Bot client started.")

        try:
            me = await bot_client.get_me()
            print(f"✅ Logged in as: @{me.username}")
        except Exception as e:
            print(f"⚠️ Could not get bot info: {e}")

        # Capture the active asyncio loop so FastAPI threads can dispatch tasks to it
        print("🌀 Capturing event loop...")
        from app import app as fastapi_app, prune_progress_task, keep_alive_task
        fastapi_app.bot_loop = asyncio.get_running_loop()

        # Start the background pruning task
        asyncio.create_task(prune_progress_task())
        print("🧹 Progress pruning task started.")

        # Start the self-ping keep-alive task (mitigates Render sleep)
        if Config.KEEP_ALIVE_INTERVAL > 0:
            asyncio.create_task(keep_alive_task())
            print(f"💓 Keep-alive task started (interval={Config.KEEP_ALIVE_INTERVAL}s).")
        else:
            print("🔕 Keep-alive disabled (KEEP_ALIVE_INTERVAL=0).")

        # Mark health check as ready — Render now knows the bot is fully online
        fastapi_app.is_ready = True
        print("🎊 BOT IS ALIVE 🎊 (health check → ready:true)")

        # Use Pyrogram's own idle() — handles SIGTERM/SIGINT properly
        await idle()

        # Signal received — mark as shutting down
        print("👋 Bot stopping cleanly. Goodbye!")
        fastapi_app.is_shutting_down = True
        await bot_client.stop()

    # Run everything manually since we want more control over start/stop
    # NOTE: we use the pinned loop from module top (not asyncio.run, which
    # would create a NEW loop and trigger pyrogram's "Future attached to a
    # different loop" error). See the comment at the top of this file.
    print("🎬 Starting event loop...")
    try:
        _MAIN_LOOP.run_until_complete(main())
    except Exception as e:
        print(f"❌ Bot crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # Drain any pending callbacks before closing the loop
        try:
            _MAIN_LOOP.run_until_complete(_MAIN_LOOP.shutdown_asyncgens())
        except Exception:
            pass
        try:
            _MAIN_LOOP.close()
        except Exception:
            pass
