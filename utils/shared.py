import asyncio
from pyrogram import Client
from plugins.config import Config
import time

print(f"🧬 Loading utils.shared at {time.time()} (Memory ID: {id(Config)})")

# Initialize the Bot Client here so it can be safely imported by any module
# without causing circular imports or re-running the entry-point script.
plugins = dict(root="plugins")

bot_client = Client(
    Config.SESSION_NAME,
    bot_token=Config.BOT_TOKEN,
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    plugins=plugins,
    sleep_threshold=300,
    workers=40,              # Increased for high concurrency
    upload_boost=True,
    max_concurrent_transmissions=20, # Increased for multiple users
)

# Global HTTP session manager for connection pooling.
# IMPORTANT: aiohttp.ClientSession is bound to the event loop it was created
# on. If we cache one session globally and it gets reused from a different
# loop (e.g. FastAPI's Uvicorn loop creates it, then a coroutine scheduled
# via run_coroutine_threadsafe onto the pyrogram bot loop tries to use it),
# aiohttp's internal Timeout context manager raises:
#     RuntimeError: Timeout context manager should be used inside a task
# because `asyncio.current_task(session._loop)` returns None when we're on
# a different loop. We therefore track the session's bound loop and recreate
# the session whenever the running loop changes.
HTTP_SESSION = None
HTTP_SESSION_LOOP = None  # the asyncio loop the cached session is bound to

async def get_http_session():
    global HTTP_SESSION, HTTP_SESSION_LOOP
    current_loop = asyncio.get_running_loop()

    # Detect loop mismatch (the real cause of "Timeout context manager should
    # be used inside a task"). Recreate the session on the current loop.
    if (
        HTTP_SESSION is None
        or HTTP_SESSION.closed
        or HTTP_SESSION_LOOP is not current_loop
    ):
        # Clean up any stale session bound to a different loop.
        if HTTP_SESSION is not None and not HTTP_SESSION.closed:
            try:
                if HTTP_SESSION_LOOP is not None and HTTP_SESSION_LOOP is not current_loop:
                    # Schedule close on the session's own loop (thread-safe).
                    # Don't await — we're on a different loop now.
                    asyncio.run_coroutine_threadsafe(
                        HTTP_SESSION.close(), HTTP_SESSION_LOOP
                    )
                else:
                    await HTTP_SESSION.close()
            except Exception:
                pass

        import aiohttp
        # Higher limits for aggressive concurrent file size probing
        connector = aiohttp.TCPConnector(
            limit=1000,
            limit_per_host=100,
            force_close=False,            # Reuse connections
            enable_cleanup_closed=True,
        )
        # Use a longer timeout for the session itself to handle slow probes.
        # NOTE: the per-request timeout is enforced by `asyncio.wait_for()` in
        # `_safe_request` (see plugins/helper/upload.py) so we don't rely on
        # aiohttp's internal Timeout context manager for per-call timeouts.
        timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=None)
        HTTP_SESSION = aiohttp.ClientSession(connector=connector, timeout=timeout)
        HTTP_SESSION_LOOP = current_loop
    return HTTP_SESSION

async def close_http_session():
    global HTTP_SESSION, HTTP_SESSION_LOOP
    if HTTP_SESSION and not HTTP_SESSION.closed:
        try:
            await HTTP_SESSION.close()
        except Exception:
            pass
    HTTP_SESSION = None
    HTTP_SESSION_LOOP = None

# Global dictionary for shared progress tracking between Flask and Pyrogram
WEBAPP_PROGRESS: dict[int, dict] = {}
