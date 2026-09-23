import os
import asyncio
import time
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from plugins.config import Config
from utils.shared import WEBAPP_PROGRESS, bot_client

# Initialize FastAPI
app = FastAPI(title="URL Uploader API")

# Add CORS Middleware for Telegram WebApp compatibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the static files from 'web' folder
app.mount("/web", StaticFiles(directory="web"), name="web")

# Runtime flags
app.is_ready = False
app.is_shutting_down = False
app.bot_loop = None

# Global cache for index.html
_INDEX_HTML_CACHE = None

async def prune_progress_task():
    """Background task to keep memory low by pruning old progress data."""
    while True:
        try:
            now = time.time()
            # Remove entries that haven't been updated for 1 hour
            to_del = [uid for uid, info in WEBAPP_PROGRESS.items()
                      if now - info.get("_last_update", now) > 3600]
            for uid in to_del:
                del WEBAPP_PROGRESS[uid]
        except Exception:
            pass
        await asyncio.sleep(600) # Check every 10 mins


async def keep_alive_task():
    """
    Periodically ping our own /health endpoint to mitigate Render's
    15-minute inactivity sleep. Disabled when KEEP_ALIVE_INTERVAL=0.
    """
    if Config.KEEP_ALIVE_INTERVAL <= 0:
        return
    # Local import to avoid top-level aiohttp dependency cycle
    import aiohttp
    # Determine self URL — prefer WEBAPP_URL, fall back to localhost:PORT
    base_url = Config.WEBAPP_URL or f"http://127.0.0.1:{Config.PORT}"
    health_url = f"{base_url}/health"
    interval = max(60, Config.KEEP_ALIVE_INTERVAL)  # at least 1 min
    Config.LOGGER.info(f".keep_alive: pinging {health_url} every {interval}s")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await asyncio.sleep(interval)
                async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        Config.LOGGER.info(f".keep_alive: status={resp.status}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                Config.LOGGER.info(f".keep_alive: ping failed ({e}); retrying")


@app.get("/health")
async def health():
    """Health check — supports GET and HEAD (Render/Koyeb probe).

    Returns 200 even during startup so Render's deploy probe passes quickly;
    the Telegram bot itself reports readiness via the `is_ready` flag in
    the payload for debugging, but we don't fail the probe (Render will
    otherwise mark the deploy as failed and roll back).
    """
    if app.is_shutting_down:
        return JSONResponse(
            status_code=503,
            content={"status": "shutting_down", "ready": False},
        )
    return {
        "status": "ok",
        "ready": app.is_ready,
        "bot_connected": bool(getattr(bot_client, "is_connected", False)),
    }


@app.get("/api/config")
async def api_config():
    """Return public configuration values to the frontend."""
    return {
        "adsgram_block_id": Config.ADSGRAM_BLOCK_ID,
        "ready": app.is_ready,
    }


class FormatsRequest(BaseModel):
    url: str


@app.post("/api/formats")
async def api_formats(req: FormatsRequest):
    """Endpoint for MiniApp to extract video qualities without uploading."""
    if not app.is_ready:
        raise HTTPException(status_code=503, detail="Bot is not ready")

    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="No URL provided")

    # YouTube is gated behind ALLOW_YOUTUBE=true (default false) — see Config
    if not Config.ALLOW_YOUTUBE and (
        "youtube.com" in url.lower() or "youtu.be" in url.lower()
    ):
        raise HTTPException(status_code=403, detail="YouTube downloading not allowed (set ALLOW_YOUTUBE=true to enable).")

    from plugins.helper.upload import fetch_ytdlp_formats

    try:
        res = await fetch_ytdlp_formats(url)
        return res
    except Exception as e:
        Config.LOGGER.exception(f"API Formats Error for {url}")
        raise HTTPException(status_code=500, detail=str(e))


class DownloadRequest(BaseModel):
    url: str
    chat_id: int
    format_id: str = None
    mode: str = "media"
    filename: str = None


@app.post("/api/download")
async def api_download(req: DownloadRequest):
    """Triggered when user clicks 'Beam to Chat' in the MiniApp"""
    if not app.is_ready:
        raise HTTPException(status_code=503, detail="Bot is not ready")

    url = (req.url or "").strip()
    if not url or not req.chat_id:
        raise HTTPException(status_code=400, detail="URL or chat_id missing.")

    # YouTube is gated behind ALLOW_YOUTUBE=true (default false) — see Config
    if not Config.ALLOW_YOUTUBE and (
        "youtube.com" in url.lower() or "youtu.be" in url.lower()
    ):
        raise HTTPException(status_code=403, detail="YouTube downloading not allowed (set ALLOW_YOUTUBE=true to enable).")

    from plugins.commands import trigger_webapp_download

    try:
        if app.bot_loop:
            asyncio.run_coroutine_threadsafe(
                trigger_webapp_download(req.chat_id, url, req.format_id, req.mode, req.filename),
                app.bot_loop
            )
        else:
            # Fallback if loop isn't captured yet
            asyncio.create_task(trigger_webapp_download(req.chat_id, url, req.format_id, req.mode, req.filename))
        return {"status": "queued"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CancelRequest(BaseModel):
    user_id: int


@app.post("/api/cancel")
async def api_cancel(req: CancelRequest):
    if not app.is_ready:
        raise HTTPException(status_code=503, detail="Bot is not ready")

    user_id = req.user_id
    from plugins.commands import ACTIVE_TASKS
    task_info = ACTIVE_TASKS.get(user_id)
    if not task_info:
        raise HTTPException(status_code=404, detail="No active process to cancel.")

    task, cancel_ref = task_info
    cancel_ref[0] = True

    if app.bot_loop:
        app.bot_loop.call_soon_threadsafe(task.cancel)
    else:
        task.cancel()

    return {"status": "cancelled"}


@app.get("/api/progress")
async def api_progress(user_id: int):
    """Endpoint for MiniApp to poll live download/upload progress."""
    if not app.is_ready:
        raise HTTPException(status_code=503, detail="Bot is not ready")

    progress_data = WEBAPP_PROGRESS.get(user_id)

    if progress_data:
        # Refresh last-update stamp so prune_progress_task doesn't reap it
        progress_data["_last_update"] = time.time()
        return progress_data
    else:
        return {"action": "idle", "percentage": 0}


# ── Adsgram Reward Postback ───────────────────────────────────────────────────
# Adsgram fires a server-to-server GET to your reward URL after a rewarded ad
# finishes, replacing [userId] with the user's Telegram ID.
# Reference: https://adsgram.ai/blog/adsgram/telegram-mini-app-tma-development-mistakes-and-how-to-avoid-them
#   - Must accept HTTPS GET on port 443
#   - Must include the [userId] placeholder in the dashboard
#   - Does NOT fire in debug mode
#   - Available for apps above 50,000 daily average users
#
# Set in Adsgram dashboard as:
#   https://telegram-url-uploader-x3u8.onrender.com/api/adsgram/reward?userid=[userId]


@app.get("/api/adsgram/reward")
async def adsgram_reward(userid: int = Query(..., description="Telegram user ID, injected by Adsgram")):
    """
    Receive Adsgram's reward postback.

    We don't currently track per-user credits, so this endpoint just
    acknowledges the postback with 200 OK and logs it for analytics.
    If you want to grant the user a benefit (priority queue, skip next
    interstitial, etc.), wire that up here.
    """
    Config.LOGGER.info(f"💰 Adsgram reward postback received for user {userid}")
    # Future: increment a credit counter, push a notification via bot, etc.
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "userid": userid, "rewarded": True},
    )


# ── Sniffer API Compatibility (link-api) ──────────────────────────────────────

@app.get("/api/link")
async def link_api_info():
    """Link-API discovery — returns available endpoints."""
    return {
        "message": "Direct Link Grabber API (integrated) — IDM-style",
        "endpoints": {
            "GET /grab?url=<URL>": "Grab links from any video URL",
            "POST /grab": '{"url": "...", "use_browser": true, "timeout": 25}',
            "POST /extract": '{"url": "..."} — yt-dlp compatible formats',
            "GET /health": "API status check",
        },
    }


class LinkRequest(BaseModel):
    """Request body for POST /grab — link-api compatibility."""
    url: str
    use_browser: bool = True  # False = force yt-dlp only
    timeout: int = 25  # seconds


@app.get("/grab")
async def grab_get(
    url: str = Query(..., description="Any video page URL"),
    use_browser: bool = Query(True, description="Use headless browser interception"),
    timeout: int = Query(25, description="Timeout in seconds"),
):
    """Extract direct media links from any video URL (link-api compatible)."""
    try:
        from plugins.helper.extractor import extract_links
        result = await extract_links(url, use_browser=use_browser, timeout=timeout)
        if not result.get("links"):
            raise HTTPException(status_code=400, detail=f"No media links found for: {url}")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Extraction error: {str(e)}")


@app.post("/grab")
async def grab_post(req: LinkRequest):
    """Extract direct media links from any video URL (POST — link-api compatible)."""
    try:
        from plugins.helper.extractor import extract_links
        result = await extract_links(
            req.url,
            use_browser=req.use_browser,
            timeout=req.timeout,
        )
        if not result.get("links"):
            raise HTTPException(status_code=400, detail=f"No media links found for: {req.url}")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Extraction error: {str(e)}")


@app.post("/extract")
async def extract_post(request: Request):
    """Legacy yt-dlp extraction compatibility endpoint."""
    try:
        data = await request.json()
        url = data.get("url")
        if not url:
            return {"error": "Missing 'url' in JSON body", "formats": []}

        from plugins.helper.extractor import extract_raw_ytdlp
        result = await extract_raw_ytdlp(url)
        return result
    except Exception as e:
        return {"error": str(e), "formats": [], "title": "Extraction Failed"}


# ── HTML & static serving (MUST be registered AFTER all specific routes) ──────
# Otherwise the catch-all /{path:path} would shadow /api/* and /grab/*.
@app.get("/", response_class=HTMLResponse)
async def index():
    global _INDEX_HTML_CACHE
    if app.is_shutting_down:
        raise HTTPException(status_code=503, detail="Bot is shutting down…")
    if not app.is_ready:
        raise HTTPException(status_code=503, detail="Bot is starting…")

    if _INDEX_HTML_CACHE:
        return _INDEX_HTML_CACHE

    try:
        html_path = os.path.join("web", "index.html")
        if not os.path.exists(html_path):
            raise HTTPException(status_code=404, detail="404 - Web assets missing")

        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
            # Inject Block ID directly into HTML
            content = content.replace("{{ADSGRAM_BLOCK_ID}}", Config.ADSGRAM_BLOCK_ID)
            _INDEX_HTML_CACHE = content
            return content
    except Exception as e:
        Config.LOGGER.error(f"Error serving index: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.get("/{path:path}")
async def serve_static(path: str):
    """Catch-all static file server for /web assets.
    Registered last so it never shadows /api/*, /grab, /extract, /health, etc.
    """
    file_path = os.path.join("web", path)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return FileResponse(file_path)
    raise HTTPException(status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=Config.PORT)
