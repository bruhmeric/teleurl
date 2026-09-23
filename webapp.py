"""Zero-knowledge one-time web viewer.

How it stays zero-knowledge
---------------------------
* The bot builds ``https://<host>/v/<token>#<base64url DEK>``.
* Browsers NEVER send the URL fragment (#...) to the server - the AES key
  exists only in the user's browser and in the link itself.
* ``GET /v/<token>``          -> static HTML shell (no secrets).
* ``GET /v/<token>/data``     -> one-time JSON with ciphertext, nonce and the
  AAD (image id).  The page passes the AAD back as AES-GCM ``additionalData``
  — the ciphertext is bound to it server-side, and omitting it is exactly
  what produces a WebCrypto ``OperationError``.
  The claim is atomic (UPDATE ... WHERE link_used=0), so a second fetch gets
  410 Gone.  The server never decrypts anything for viewing.
* The page decrypts with the browser's built-in WebCrypto (AES-GCM), strips
  the key from the address bar, and blanks itself after 60 seconds.

Anti-save hardening on the page (what it blocks / what it cannot)
------------------------------------------------------------------
Blocked:  right-click context menu, drag-and-drop, text/canvas selection,
          clipboard copy/cut, Ctrl/Cmd+S / P / U, F12 & devtools shortcuts
          (3 strikes -> view closes), the Print Screen key (view closes),
          printing (blank page), iOS long-press "Save to Photos"
          (-webkit-touch-callout + pointer-events:none), page backgrounding
          (canvas cleared while hidden).  The image is painted to a
          <canvas> from an ImageBitmap - there is no <img> tag, no blob URL
          in the DOM (except a legacy fallback), and nothing cacheable
          (Cache-Control: no-store).  An optional per-link watermark
          ("ONE-TIME . <tag>") makes any capture that DOES happen traceable
          to this specific link issuance.

Cannot be blocked by ANY web page: OS-level screenshots (Print Screen,
snipping tools, screen recorders) and photographing the screen with another
device.  Only DRM video pipelines (EME/Widevine) can blank OS captures, and
those do not exist for plain still images.  A determined user with devtools
can also extract canvas pixels (canvas.toDataURL).  The goal here is to make
every casual in-browser saving path impossible, not to defeat physics.

Note: WebCrypto requires a secure context - serve this over HTTPS (or use
localhost during development), otherwise the page will show an error.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

import config
from crypto import b64url

if TYPE_CHECKING:  # pragma: no cover
    from db import Vault

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex, nofollow">
<title>One-time decrypted view</title>
<style>
  :root { color-scheme: dark; }
  * { -webkit-user-select:none; user-select:none; -webkit-touch-callout:none; }
  body { margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#101317; color:#e8eaed;
         font:15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  main { max-width: min(92vw, 900px); text-align:center; padding:24px; }
  h1 { font-size:19px; font-weight:600; margin:0 0 6px; }
  #status { color:#9aa3ad; margin:0 0 18px; }
  canvas#cv { max-width:100%; max-height:72vh; border-radius:10px;
              box-shadow:0 8px 40px rgba(0,0,0,.5); pointer-events:none;
              -webkit-user-drag:none; }
  .hint { color:#7c848d; font-size:13px; margin-top:18px; }
  code { background:#1b2027; padding:1px 5px; border-radius:4px; }
  @media print { body { display:none !important; } }
</style>
</head>
<body>
<main>
  <h1>&#128275; One-Time Decrypted View</h1>
  <p id="status">Decrypting in your browser&hellip;</p>
  <canvas id="cv" hidden></canvas>
  <p class="hint">The key travels only in the URL <code>#fragment</code> &mdash; it was
     never sent to the server. Right-click, drag, saving, printing and clipboard
     are disabled; the view blanks on app switch and after 60&nbsp;s.<br>
     This link was single-use and is now dead.</p>
</main>
<script>
const WATERMARK = __WATERMARK__;
const status = document.getElementById('status');
const cv = document.getElementById('cv');
const ctx = cv.getContext('2d');
let drawable = null, cleared = false, strikes = 0;

function b64uToBytes(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  const bin = atob(s);
  return Uint8Array.from(bin, c => c.charCodeAt(0));
}

function fitCanvas() {
  if (!drawable) return;
  const maxW = Math.min(window.innerWidth - 36, 900);
  const maxH = Math.round(window.innerHeight * 0.72);
  const s = Math.min(maxW / drawable.width, maxH / drawable.height, 1);
  cv.width = Math.max(1, Math.round(drawable.width * s));
  cv.height = Math.max(1, Math.round(drawable.height * s));
}

function stamp() {          /* per-link watermark: deters and traces captures */
  const tag = (location.pathname.split('/v/')[1] || '').slice(0, 8).toUpperCase();
  ctx.save();
  ctx.font = '600 13px system-ui, sans-serif';
  ctx.textAlign = 'left';
  ctx.lineWidth = 3;
  ctx.strokeStyle = 'rgba(0,0,0,0.45)';
  ctx.fillStyle = 'rgba(255,255,255,0.75)';
  const label = 'ONE-TIME \u00b7 ' + (tag || 'VIEW');
  ctx.strokeText(label, 12, cv.height - 12);
  ctx.fillText(label, 12, cv.height - 12);
  ctx.globalAlpha = 0.10;
  ctx.translate(cv.width / 2, cv.height / 2);
  ctx.rotate(-Math.PI / 7);
  ctx.font = '700 ' + Math.max(20, Math.round(cv.width / 13)) + 'px system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.fillStyle = '#ffffff';
  ctx.fillText('ONE-TIME VIEW', 0, 0);
  ctx.restore();
}

function draw() {
  if (!drawable || cleared) return;
  fitCanvas();
  ctx.drawImage(drawable, 0, 0, cv.width, cv.height);
  if (WATERMARK) stamp();
  cv.hidden = false;
}

function blank(msg) {       /* destroy the pixels and end the session */
  if (cleared) return;
  cleared = true;
  ctx.clearRect(0, 0, cv.width, cv.height);
  cv.hidden = true;
  try { if (drawable && drawable.close) drawable.close(); } catch (e) {}
  drawable = null;
  status.textContent = msg;
}

/* ---- anti-save hardening ------------------------------------------------- */
document.addEventListener('contextmenu', (e) => e.preventDefault());
document.addEventListener('dragstart',  (e) => e.preventDefault());
document.addEventListener('selectstart',(e) => e.preventDefault());
document.addEventListener('copy',       (e) => e.preventDefault());
document.addEventListener('cut',        (e) => e.preventDefault());
document.addEventListener('visibilitychange', () => {
  if (document.hidden) ctx.clearRect(0, 0, cv.width, cv.height);  /* hide from app switcher */
  else draw();
});
window.addEventListener('beforeprint', () => ctx.clearRect(0, 0, cv.width, cv.height));
window.addEventListener('afterprint', draw);
window.addEventListener('resize', draw);
window.addEventListener('keydown', (e) => {
  const k = (e.key || '').toLowerCase();
  const mod = e.ctrlKey || e.metaKey;
  const blocked = (mod && ['s', 'p', 'u'].indexOf(k) >= 0) ||
                  k === 'f12' ||
                  (mod && e.shiftKey && ['i', 'j', 'c'].indexOf(k) >= 0);
  if (!blocked) return;
  e.preventDefault();
  if (++strikes >= 3) {
    blank('\ud83d\udd12 Repeated save/devtools attempts \u2014 view closed.');
  } else {
    status.textContent = '\ud83d\udeab Saving, printing and devtools are disabled on this page.';
  }
});
window.addEventListener('keyup', (e) => {
  /* the OS may still grab the frame, but the view dies immediately after */
  if (e.key === 'PrintScreen' || e.code === 'PrintScreen') {
    blank('\ud83d\udcf8 Print Screen detected \u2014 view closed.');
  }
});

(async () => {
  const frag = location.hash.slice(1);
  if (!frag) { status.textContent = '\u274c No key found in the URL fragment.'; return; }
  if (!window.crypto || !crypto.subtle) {
    status.textContent = '\u274c WebCrypto is unavailable \u2014 open this link over HTTPS.'; return;
  }
  try {
    const keyBytes = b64uToBytes(frag);
    if (keyBytes.length !== 32) throw new Error('unexpected key length');
    const key = await crypto.subtle.importKey('raw', keyBytes, {name: 'AES-GCM'},
                                              false, ['decrypt']);
    const r = await fetch(location.pathname + '/data', {cache: 'no-store'});
    if (r.status === 410) { status.textContent = '\u274c Link already used or expired.'; return; }
    if (!r.ok) { status.textContent = '\u274c Fetch failed (' + r.status + ').'; return; }
    const j = await r.json();
    const pt = await crypto.subtle.decrypt(
      {name: 'AES-GCM',
       iv: b64uToBytes(j.nonce),
       additionalData: new TextEncoder().encode(j.aad)},
      key, b64uToBytes(j.ct));

    /* canvas pipeline: no img element, no persistent blob URL in the DOM */
    if (self.createImageBitmap) {
      drawable = await createImageBitmap(new Blob([pt], {type: j.mime}));
    } else {               /* legacy fallback: img element via short-lived blob URL */
      const url = URL.createObjectURL(new Blob([pt], {type: j.mime}));
      const im = new Image();
      im.src = url;
      await im.decode();
      drawable = im;
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
    draw();
    status.textContent = '\u2705 Decrypted locally \u2014 no server saw the image.';
    history.replaceState(null, '', location.pathname);   /* strip the key from the URL */
    setTimeout(() => blank('\ud83e\uddf9 Cleared. Reloading will not work \u2014 the link is dead.'), 60000);
  } catch (e) {
    status.textContent = '\u274c Decryption failed: ' + e;
  }
})();
</script>
</body>
</html>"""


@web.middleware
async def security_headers(request, handler):
    resp = await handler(request)
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; img-src blob:; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'")
    return resp


async def _page(request: web.Request) -> web.Response:
    page = PAGE.replace("__WATERMARK__", "true" if config.WATERMARK_ZK else "false")
    return web.Response(text=page, content_type="text/html", headers=NO_STORE)


async def _data(request: web.Request) -> web.Response:
    vault = request.app["vault"]
    token = request.match_info["token"]
    row = await vault.claim_link(token)          # atomic one-time claim
    if row is None:
        return web.json_response(
            {"error": "link already used or expired"}, status=410, headers=NO_STORE)
    return web.json_response({
        "nonce": b64url(row["nonce"]),
        "ct": b64url(row["ct"]),
        "aad": row["id"],   # AES-GCM additional data the page must pass
        "mime": row["mime"],
    }, headers=NO_STORE)


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True}, headers=NO_STORE)


def build_app(vault: "Vault") -> web.Application:
    app = web.Application(middlewares=[security_headers])
    app["vault"] = vault
    app.router.add_get("/", _health)
    app.router.add_get("/v/{token}", _page)
    app.router.add_get("/v/{token}/data", _data)
    return app
