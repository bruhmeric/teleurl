# 🔐 Zero-Knowledge Image Vault — Telegram Bot

A privacy-first Telegram bot that seals images under AES-256-GCM, keeps
**only ciphertext on disk**, publishes irreversible blurred previews to a
channel, and on request decrypts **in RAM only** to send a self-deleting
photo — or better, hands back a **one-time link that is decrypted inside the
viewer's browser**, so the server never decrypts anything for viewing.

```
you ──photo──▶ bot ──▶ RAM ──AES-256-GCM──▶ vault.db (ciphertext only)
                │                        └─ plaintext zeroised
                └──▶ channel: 26-px mosaic preview (irreversible)

viewer taps "View once" ─▶ spoiler-covered photo, no saving/forwarding,
                          deleted after 60 s
viewer taps "One-time link" ─▶ https://host/v/<token>#<AES-key>
                             browser fetches ciphertext, decrypts locally
                             with WebCrypto; link works exactly once
```

## What is actually protected (honest trust model)

| Stage | Protection |
|---|---|
| Image at rest (`vault.db`, backups) | ✅ AES-256-GCM, per-image keys wrapped under an Argon2id key from your `MASTER_PASSPHRASE`. A stolen database is useless without the passphrase. |
| Bot server memory | ✅ Plaintext exists only briefly in RAM and is zeroised. Never on disk, in logs, or in temp files. ⚠️ Python is a GC'd language — wiping is best-effort; see *Limitations*. |
| Channel preview | ✅ 26-pixel mosaic + blur — the original detail no longer exists in the pixels, so it cannot be reversed. EXIF is stripped (fresh JPEG). |
| `/get` in-chat photo | ⚠️ Decrypted in RAM, sent with `protect_content` (blocks forwarding/saving in clients), covered by a tap-to-reveal spoiler, and deleted after 60 s. **True view-once media is impossible for bots** — the Bot API has no such parameter (verified against Bot API 9.x/10.x; the newer *Ephemeral Messages* are per-user group whispers, not view-once). **Telegram's servers necessarily relay the photo in transit** — that is unavoidable for media sent inside Telegram. |
| `/secret` link | ✅ **Strongest mode.** The server serves ciphertext only. The AES key lives in the URL `#fragment`, which browsers never transmit. Decryption is 100% client-side (WebCrypto AES-GCM). Single use + expiry. The viewer page is anti-save hardened — see below. |
| Upload transit | ⚠️ Telegram is the transport for the initial upload, so Telegram sees the original there (no bot can avoid this). End-to-end-avoiding uploads would require client-side encryption before sending. |
| Screen capture | ❌ No system on Earth stops a viewer from photographing their screen with a second device. Trust the recipient, not just the pipe. |

If your requirement is strictly *"the server must never decrypt anything"*,
use `/secret` links only: for viewing, the server is ciphertext-blind.

### Viewer-page hardening (and its honest limits)

The `/secret` page goes out of its way to make saving awkward:

* rendered to a **`<canvas>`** — no `<img>` tag, no "Save image as…", no
  persistent blob URL in the DOM, nothing cacheable (`Cache-Control: no-store`;
  the one-time `/data` endpoint can never be fetched twice);
* right-click, drag-and-drop, selection and clipboard copy/cut disabled;
* Ctrl/Cmd+S / P / U, F12 and devtools shortcuts blocked — three attempts and
  the view closes itself;
* Print Screen keypress closes the view; printing outputs a blank page
  (`@media print` + `beforeprint` blanking);
* iOS long-press "Save to Photos" suppressed (`-webkit-touch-callout`,
  `pointer-events:none`);
* the canvas blanks while the app/tab is backgrounded (no task-switcher
  peeking) and 60 s after decryption;
* optional **per-link watermark** (`WATERMARK_ZK=1`, default on): a
  "ONE-TIME · `<tag>`" stamp ties any capture back to the exact link issuance.

**What no web page can block:** OS-level screenshots (snipping tools, screen
recording) and photographing the screen with another device. Only DRM video
pipelines (EME/Widevine — what Netflix uses) can blank OS captures, and those
exist only for encrypted video, not still images. A determined user with
devtools can also read canvas pixels (`canvas.toDataURL`). The goal of the
hardening is to make every *casual* in-browser saving path impossible, and to
make any capture that does happen degraded (watermark) and single-shot.

## Quick start

1. **Create the bot** — talk to [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
2. **Create a (private) channel**, then in the channel: *Administrators → Add admin → your bot* (needs *Post messages*; being an admin also lets the bot check who has joined — that powers join-to-unlock access).
3. **Deploy** on any VPS with Docker:

   ```bash
   cp .env.example .env
   $EDITOR .env          # BOT_TOKEN, MASTER_PASSPHRASE, CHANNEL_ID, ...
   docker compose up -d --build
   ```

4. **DM your bot** `/start` (the first user to `/start` claims ownership when
   `OWNER_ID` is empty — or pin your id up front via @userinfobot).
5. Send an image. Done — a blurred preview appears in the channel.

### Serving `/secret` links (HTTPS is mandatory)

WebCrypto only runs in a secure context, so expose `WEB_PORT` (default 8080)
via TLS. The simplest option is [Caddy](https://caddyserver.com):

```
vault.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Then set `BASE_URL=https://vault.example.com` in `.env`.

**On Render.com you can skip this entirely** — HTTPS is built in and the URL
is auto-detected (see below).

## Deploy on Render.com

The project ships a [`render.yaml`](render.yaml) Blueprint. Render gives you
automatic HTTPS, so `/secret` zero-knowledge links work with **zero extra
setup** — the public URL is auto-detected from `RENDER_EXTERNAL_URL`, and the
viewer binds to Render's injected `PORT` automatically.

1. Push this folder to a GitHub/GitLab repo:
   ```bash
   git init && git add . && git commit -m "zero-knowledge image vault"
   git remote add origin <your-repo-url> && git push -u origin main
   ```
2. Render Dashboard → **New → Blueprint** → pick the repo. Render reads
   `render.yaml` and asks for the secret values (`sync: false`):
   `BOT_TOKEN`, `MASTER_PASSPHRASE`, `CHANNEL_ID`, `OWNER_ID`.
3. **Apply**. First build takes ~2 min, then the bot is live at
   `https://<service-name>.onrender.com` and the `/` health endpoint answers
   with `{"ok": true}`.
4. DM the bot `/start`, send an image, unlock it in the channel.

### Free-tier gotchas (read before trusting it with images)

| Issue | Consequence | Fix |
|---|---|---|
| Instance sleeps after ~15 min without **inbound** HTTP (Telegram polling is outbound and does not count) | Bot pauses until woken; replies delayed | Ping `https://<service>.onrender.com/` every 10 min with a free monitor (UptimeRobot, cron-job.org), or upgrade to a paid plan |
| Ephemeral filesystem | `vault.db` is **wiped on every deploy/restart** — the vault empties silently | Attach a **Persistent Disk**: switch `plan: starter` in `render.yaml`, uncomment the `disk:` block and the `DB_PATH=/var/data/vault.db` env var, redeploy (~$7/mo + $0.25/GB) |
| 512 MB RAM | Very large uncompressed image documents may OOM during preview/encrypt | Send as compressed photo, or upgrade the plan |

The bot logs a loud warning at startup whenever it detects Render without a
persistent disk, so you cannot lose a vault by accident without noticing.

> Prefer containers? Render also builds the existing `Dockerfile` directly
> (New → Web Service → Docker runtime) — `PORT` binding is already handled.

## Commands

| Command | Action |
|---|---|
| send an image | seal it: encrypt → vault → channel preview |
| `/get <id>` | view-once style: photo arrives spoiler-covered, saving/forwarding blocked, auto-deleted after `VIEW_TTL_SECONDS` (60) |
| `/burn <id>` | same, then the ciphertext row is shredded forever |
| `/secret <id>` | one-time link; decrypted in the viewer's browser |
| `/list` | inventory (ids, sizes, dates) |
| `/delete <id>` | shred without viewing |
| `/publish <id>` / `/unpublish <id>` | manage the channel preview |
| `/wipe` | shred the entire vault (with confirmation) |
| `/help` | the trust model, in chat |

## Configuration

See [.env.example](.env.example). Highlights:

| Variable | Meaning |
|---|---|
| `BOT_TOKEN` | from @BotFather (required) |
| `MASTER_PASSPHRASE` | wraps all image keys via Argon2id — **lose it = lose the vault** |
| `CHANNEL_ID` | `@channel` or `-100…` for previews |
| `OWNER_ID` / `ALLOWED_USER_IDS` | who may unlock images |
| `ALLOW_CHANNEL_MEMBERS` | `1` (default): **anyone who joined the channel can use the bot** — join-to-unlock, with a friendly "join the channel" hint for strangers. `0`: owner/whitelist only |
| `SPOILER_ON_GET` | `1` (default): decrypted photos arrive under a tap-to-reveal spoiler (closest thing to view-once the Bot API offers) |
| `WATERMARK_ZK` | `1` (default): stamp a traceable per-link watermark on the zero-knowledge viewer canvas |
| `BASE_URL` | HTTPS base for one-time links (enables `/secret`) |
| `VIEW_TTL_SECONDS` / `LINK_TTL_SECONDS` | 60 s photo lifetime / 600 s link validity |

## Security notes & limitations

- **20 MB cap** — Telegram's bot API `getFile` limit; send very large images as
  documents, or compress first.
- **RAM wiping** — plaintext buffers are `bytearray`s zeroised after use, but
  Python's interpreter may create transient copies during parsing/sending.
  For strict server-side blindness rely on `/secret` links, where viewing never
  decrypts server-side at all.
- **Backups** — copy `data/vault.db` freely; it is ciphertext. Guard the
  passphrase separately (password manager).
- **Key rotation** — changing `MASTER_PASSPHRASE` re-derives the wrapping key;
  previously sealed items become unreadable (they stay encrypted forever).
- **Telegram's own retention** — the `/get` photo transits Telegram and their
  deletion semantics apply. `/secret` mode avoids exposing plaintext to your
  server, and the link dies after one fetch.
- **Host security** — the machine running this bot handles plaintext in RAM;
  harden it accordingly (disk encryption, no swap or encrypted swap:
  `swapoff -a`, patched OS, no unrelated services).

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python smoke_test.py      # crypto, preview, DB and one-time-link tests
BOT_TOKEN=1:test MASTER_PASSPHRASE=dev python -c "import bot"   # wiring check
```

Project layout:

```
bot.py       aiogram 3 bot: upload → encrypt → channel → /get /burn /secret …
crypto.py    AES-256-GCM envelope encryption, Argon2id KEK, zeroisation
db.py        async SQLite vault, atomic one-time link claims
preview.py   irreversible 26-px mosaic generator (Pillow)
webapp.py    zero-knowledge viewer: aiohttp + WebCrypto page
config.py    environment configuration (Render-aware)
render.yaml  Render.com Blueprint (free tier default, disk options inside)
```
