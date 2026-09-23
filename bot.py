"""Zero-Knowledge Image Vault - Telegram bot.

Flow
----
1. The owner (or any member of the preview channel) sends an image to the
   bot in private chat.
2. The bot downloads it straight into RAM, encrypts it with a fresh AES-256
   key (wrapped under an Argon2id-derived master key), stores ONLY ciphertext
   in SQLite and wipes plaintext from memory.
3. A heavily pixelated, irreversible mosaic preview is posted to the channel
   with "View once (60s)" and "One-time link" buttons.
4. On request:
     * View      -> decrypt in RAM, send as photo/document with
                    protect_content=True, auto-delete after VIEW_TTL seconds.
     * ZK link   -> one-time HTTPS link whose #fragment carries the AES key;
                    decryption happens in the viewer's browser (WebCrypto).
                    The server serves ciphertext only.
     * /burn     -> same as View, then the ciphertext row is shredded.

Nothing is ever written to disk in plaintext.  See README.md for the honest
trust model (what is and is not protected).
"""
from __future__ import annotations

import asyncio
import io
import logging
import secrets
import time
from datetime import datetime, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
from crypto import CryptoError, VaultCrypto, b64url, new_salt, wipe
from db import Vault
from preview import fallback_preview, make_mosaic
from webapp import build_app

log = logging.getLogger("zkvault")
router = Router()

WELCOME = (
    "🔐 <b>Zero-Knowledge Image Vault</b>\n\n"
    "Send me any image — I encrypt it with AES-256-GCM before it touches disk, "
    "post an irreversible blurred preview to the channel, and keep only ciphertext.\n\n"
    "<b>Commands</b>\n"
    "• <code>/get &lt;id&gt;</code> — view-once style: saving &amp; forwarding "
    f"blocked, gone in {config.VIEW_TTL}s\n"
    "• <code>/secret &lt;id&gt;</code> — one-time link, decrypted in your <i>browser</i>\n"
    "• <code>/burn &lt;id&gt;</code> — view once, then shred the ciphertext\n"
    "• <code>/list</code> · <code>/delete &lt;id&gt;</code> · <code>/wipe</code>\n"
    "• <code>/publish &lt;id&gt;</code> · <code>/unpublish &lt;id&gt;</code> — channel previews\n"
    "• <code>/help</code> — trust model"
)

HELP = (
    "🔐 <b>How this vault protects you</b>\n\n"
    "• <b>At rest</b>: every image is sealed with its own AES-256-GCM key. Keys are "
    "wrapped under a key derived from the master passphrase with Argon2id — the "
    "passphrase lives only in the server's <code>.env</code>. A stolen "
    "<code>vault.db</code> is just noise.\n"
    "• <b>In memory</b>: plaintext exists only briefly in RAM during encrypt/send "
    "and is zeroised afterwards. It is never written to disk, logs or temp files.\n"
    "• <b>Channel</b>: previews are 26-pixel mosaics — the detail no longer exists "
    "in them, so they cannot be reversed.\n"
    "• <b>/get</b>: the closest a bot can get to view-once — Telegram's Bot API "
    "does not let bots send true view-once media, so the photo arrives covered "
    "by a tap-to-reveal spoiler, with saving/forwarding blocked "
    f"(<code>protect_content</code>), and is deleted after {config.VIEW_TTL} "
    "seconds. Telegram's servers do relay it in transit — that is unavoidable "
    "for in-chat media.\n"
    "• <b>/secret</b>: the strongest mode. The server hands your browser ciphertext "
    "plus a key in the URL fragment (never transmitted). Decryption is 100% local "
    "via WebCrypto; the link works exactly once.\n\n"
    "⚠️ No system can stop a viewer from photographing their screen with another "
    "device. Trust the recipient, not just the pipe."
)


# --------------------------------------------------------------------------
# access control
# --------------------------------------------------------------------------

_MEMBER_OK = {"creator", "administrator", "member"}
_MEMBER_CACHE: dict[int, tuple[bool, float]] = {}
_CACHE_TTL_OK = 300      # a confirmed member stays trusted for 5 min
_CACHE_TTL_DENY = 60     # denials re-check after 1 min (user may have just joined)


async def _is_channel_member(bot: Bot, user_id: int) -> bool:
    """True if the user joined the preview channel (bot must be its admin)."""
    if not config.CHANNEL_ID:
        return False
    now = time.monotonic()
    hit = _MEMBER_CACHE.get(user_id)
    if hit:
        allowed, ts = hit
        if now - ts < (_CACHE_TTL_OK if allowed else _CACHE_TTL_DENY):
            return allowed
    try:
        member = await bot.get_chat_member(config.CHANNEL_ID, user_id)
        allowed = member.status in _MEMBER_OK
    except Exception as exc:
        log.warning("membership check failed for %s: %s", user_id, exc)
        allowed = False
    _MEMBER_CACHE[user_id] = (allowed, now)
    return allowed


def _join_hint() -> str:
    """Friendly denial that tells people how to unlock access."""
    if config.ALLOW_CHANNEL_MEMBERS and config.CHANNEL_ID:
        pretty = (config.CHANNEL_ID if config.CHANNEL_ID.startswith("@")
                  else "the vault's channel")
        return (f"🔒 This vault is for members of {pretty}.\n"
                "Join the channel, then try again 🙌")
    return "⛔ This vault is private."


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _kb(image_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"👁 View once · {config.VIEW_TTL}s",
                             callback_data=f"get:{image_id}"),
        InlineKeyboardButton(text="🕶 One-time link",
                             callback_data=f"zk:{image_id}"),
    ]])


def _confirm_wipe_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💥 Yes, shred everything", callback_data="wipeall:yes"),
        InlineKeyboardButton(text="Cancel", callback_data="wipeall:no"),
    ]])


async def _is_allowed(bot: Bot, vault: Vault, user_id: int | None) -> bool:
    if not user_id:
        return False
    if config.OWNER_ID and user_id == config.OWNER_ID:
        return True
    if user_id in config.ALLOWED_USER_IDS:
        return True
    owner = await vault.get_meta("owner_id")
    if owner and int(owner) == user_id:
        return True
    # join-to-unlock: anyone who joined the preview channel may use the bot
    if config.ALLOW_CHANNEL_MEMBERS and config.CHANNEL_ID:
        return await _is_channel_member(bot, user_id)
    return False


async def _try_claim_owner(vault: Vault, user_id: int | None) -> bool:
    """Bootstrap: with OWNER_ID unset, the first /start becomes the owner."""
    if not user_id:
        return False
    if await vault.get_meta("owner_id"):
        return False
    await vault.set_meta("owner_id", str(user_id))
    log.warning("OWNER bootstrap: user %s claimed this vault. "
                "Set OWNER_ID in .env to pin it.", user_id)
    return True


async def _expire(bot: Bot, chat_id: int, message_id: int,
                  ttl: int | None = None) -> None:
    """Delete a sent photo from Telegram after ttl seconds (with retries)."""
    await asyncio.sleep(ttl or config.VIEW_TTL)
    for delay in (0, 2, 10):
        try:
            await bot.delete_message(chat_id, message_id)
            return
        except Exception:
            if delay:
                await asyncio.sleep(delay)


async def _remove_channel_post(bot: Bot, vault: Vault, row) -> None:
    if row["channel_msg_id"] and config.CHANNEL_ID:
        try:
            await bot.delete_message(config.CHANNEL_ID, row["channel_msg_id"])
        except Exception as exc:
            log.warning("could not delete channel post: %s", exc)
        await vault.set_channel_msg(row["id"], None)


async def _post_preview(bot: Bot, vault: Vault, image_id: str, preview: bytes) -> None:
    if not config.CHANNEL_ID:
        return
    try:
        msg = await bot.send_photo(
            config.CHANNEL_ID,
            BufferedInputFile(preview, filename="preview.jpg"),
            caption=f"🔐 <code>#{image_id}</code> · sealed with AES-256-GCM — "
                    "request to unlock",
            reply_markup=_kb(image_id),
            parse_mode="HTML",
        )
        await vault.set_channel_msg(image_id, msg.message_id)
    except Exception as exc:
        log.warning("channel post failed: %s", exc)


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------

async def _deliver(bot: Bot, vault: Vault, crypto: VaultCrypto, chat_id: int,
                   image_id: str, *, burn: bool = False) -> bool | str:
    """Decrypt in RAM and send as a self-deleting photo/document."""
    row = await vault.get_image(image_id)
    if row is None:
        return f"❌ No image <code>#{image_id}</code> in the vault."

    try:
        pt = crypto.decrypt(image_id, row["nonce"], row["ct"],
                            row["wrap_nonce"], row["wrapped_key"])
    except CryptoError as exc:
        return f"❌ {exc}"

    caption = (f"🔓 #{image_id}" + (" · 🔥 burned after this view" if burn else "")
               + f"\n👁 one-time view — no saving, no forwarding · "
                 f"vanishes in {config.VIEW_TTL}s")
    try:
        data = BufferedInputFile(bytes(pt), filename=row["filename"] or f"{image_id}.jpg")
        if row["as_doc"]:
            sent = await bot.send_document(chat_id, document=data,
                                           caption=caption, protect_content=True)
        else:
            sent = await bot.send_photo(chat_id, photo=data,
                                        caption=caption, protect_content=True,
                                        has_spoiler=config.SPOILER_ON_GET)
    except Exception as exc:
        log.warning("delivery to %s failed: %s", chat_id, exc)
        return ("❌ Could not send you the file — open a private chat with me "
                "first, then try again.")
    finally:
        wipe(pt)

    asyncio.create_task(_expire(bot, chat_id, sent.message_id))

    if burn:
        await vault.delete_image(image_id)
        await _remove_channel_post(bot, vault, row)
    return True


async def _send_zk_link(bot: Bot, vault: Vault, crypto: VaultCrypto,
                        chat_id: int, image_id: str) -> bool | str:
    """Build a one-time link whose #fragment holds the AES key."""
    if not config.BASE_URL:
        return "❌ <code>BASE_URL</code> is not configured on the server."
    row = await vault.get_image(image_id)
    if row is None:
        return f"❌ No image <code>#{image_id}</code> in the vault."

    token, exp = await vault.create_link(image_id, config.LINK_TTL)
    try:
        dek = crypto.unwrap_key(image_id, row["wrap_nonce"], row["wrapped_key"])
        url = f"{config.BASE_URL}/v/{token}#{b64url(bytes(dek))}"
    finally:
        wipe(dek)

    exp_str = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%H:%M UTC")
    await bot.send_message(
        chat_id,
        f"🕶 <b>Zero-knowledge link</b> · <code>#{image_id}</code>\n"
        f"Single use · expires at {exp_str}.\n"
        "The AES key rides in the URL <code>#fragment</code> — browsers never send "
        "it to the server. Decryption happens in the viewer's browser.\n"
        "🚫 On the page: right-click, drag, saving, printing and clipboard are "
        "disabled — it blanks on app switch and after 60 s.\n\n"
        f"{url}",
        parse_mode="HTML", protect_content=True, disable_web_page_preview=True)
    return True


# --------------------------------------------------------------------------
# handlers: lifecycle & upload
# --------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(m: Message, bot: Bot, vault: Vault):
    uid = m.from_user.id if m.from_user else None
    if not await _is_allowed(bot, vault, uid):
        if not config.OWNER_ID and await _try_claim_owner(vault, uid):
            pass  # first user just claimed ownership
        else:
            return await m.reply(_join_hint())
    await m.reply(WELCOME, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(m: Message, bot: Bot, vault: Vault):
    if not await _is_allowed(bot, vault, m.from_user.id if m.from_user else None):
        return await m.reply(_join_hint())
    await m.reply(HELP, parse_mode="HTML")


@router.message(F.photo | F.document)
async def on_image(m: Message, bot: Bot, vault: Vault, crypto: VaultCrypto):
    uid = m.from_user.id if m.from_user else None
    if not await _is_allowed(bot, vault, uid):
        return await m.reply(_join_hint())

    # -- figure out what was sent ------------------------------------------
    if m.photo:
        biggest = m.photo[-1]
        file_id, mime, filename, as_doc = biggest.file_id, "image/jpeg", None, 0
        size = biggest.file_size or 0
    elif m.document and (m.document.mime_type or "").startswith("image/"):
        d = m.document
        file_id = d.file_id
        mime = d.mime_type or "application/octet-stream"
        filename, as_doc, size = d.file_name, 1, (d.file_size or 0)
    else:
        return await m.reply("🤔 That is not an image. Send a photo or an image file.")

    if size > config.MAX_UPLOAD_BYTES:
        return await m.reply(
            f"📦 Too large ({size / 1e6:.1f} MB). Telegram caps bot downloads at 20 MB — "
            "try compressing first.")

    # -- download to RAM, encrypt, wipe -------------------------------------
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    data = bytearray(buf.getvalue())
    buf.close()
    if not data:
        return await m.reply("❌ Download came back empty — try sending the image as a file.")

    image_id = secrets.token_hex(6)
    try:
        enc = crypto.encrypt(image_id, bytes(data))
    except Exception:
        wipe(data)
        raise
    try:
        preview = await asyncio.to_thread(make_mosaic, bytes(data))
    except Exception:
        log.exception("preview generation failed, using placeholder")
        preview = fallback_preview()
    finally:
        wipe(data)

    await vault.add_image({
        "id": image_id,
        "ct": enc["ct"],
        "nonce": enc["nonce"],
        "wrap_nonce": enc["wrap_nonce"],
        "wrapped_key": enc["wrapped_key"],
        "preview": preview,
        "mime": mime,
        "filename": filename,
        "as_doc": as_doc,
        "size": len(enc["ct"]),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })

    await _post_preview(bot, vault, image_id, preview)

    await m.reply_photo(
        BufferedInputFile(preview, filename="preview.jpg"),
        caption=(f"🔐 Sealed as <code>#{image_id}</code>\n"
                 f"AES-256-GCM · {len(enc['ct']) / 1e6:.2f} MB of ciphertext · "
                 "plaintext already wiped from RAM\n\n"
                 f"👁 <code>/get {image_id}</code> — view, self-deletes in {config.VIEW_TTL}s\n"
                 f"🕶 <code>/secret {image_id}</code> — zero-knowledge browser link\n"
                 f"🔥 <code>/burn {image_id}</code> — view once, then shred"),
        parse_mode="HTML",
        reply_markup=_kb(image_id),
    )


# --------------------------------------------------------------------------
# handlers: commands
# --------------------------------------------------------------------------

@router.message(Command("get"))
async def cmd_get(m: Message, command: CommandObject, bot: Bot,
                  vault: Vault, crypto: VaultCrypto):
    if not await _is_allowed(bot, vault, m.from_user.id if m.from_user else None):
        return await m.reply(_join_hint())
    if not command.args:
        return await m.reply("Usage: <code>/get &lt;id&gt;</code>", parse_mode="HTML")
    res = await _deliver(bot, vault, crypto, m.chat.id, command.args.strip())
    if res is not True:
        await m.reply(res, parse_mode="HTML")


@router.message(Command("burn"))
async def cmd_burn(m: Message, command: CommandObject, bot: Bot,
                   vault: Vault, crypto: VaultCrypto):
    if not await _is_allowed(bot, vault, m.from_user.id if m.from_user else None):
        return await m.reply(_join_hint())
    if not command.args:
        return await m.reply("Usage: <code>/burn &lt;id&gt;</code>", parse_mode="HTML")
    res = await _deliver(bot, vault, crypto, m.chat.id, command.args.strip(), burn=True)
    if res is not True:
        await m.reply(res, parse_mode="HTML")


@router.message(Command("secret"))
async def cmd_secret(m: Message, command: CommandObject, bot: Bot,
                     vault: Vault, crypto: VaultCrypto):
    if not await _is_allowed(bot, vault, m.from_user.id if m.from_user else None):
        return await m.reply(_join_hint())
    if not command.args:
        return await m.reply("Usage: <code>/secret &lt;id&gt;</code>", parse_mode="HTML")
    res = await _send_zk_link(bot, vault, crypto, m.chat.id, command.args.strip())
    if res is not True:
        await m.reply(res, parse_mode="HTML")


@router.message(Command("list"))
async def cmd_list(m: Message, bot: Bot, vault: Vault):
    if not await _is_allowed(bot, vault, m.from_user.id if m.from_user else None):
        return await m.reply(_join_hint())
    rows = await vault.list_images()
    if not rows:
        return await m.reply("🗂 The vault is empty. Send me an image to seal it.")
    lines = [f"🗂 <b>Vault · {len(rows)} item(s)</b>"]
    for r in rows:
        lines.append(
            f"• <code>#{r['id']}</code> · {r['mime'].removeprefix('image/')} · "
            f"{r['size'] / 1e6:.2f} MB · {r['created'][:16].replace('T', ' ')}")
    lines.append("\n/get to view · /secret for a browser link · /burn to view-once")
    await m.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("delete"))
async def cmd_delete(m: Message, command: CommandObject, bot: Bot, vault: Vault):
    if not await _is_allowed(bot, vault, m.from_user.id if m.from_user else None):
        return await m.reply(_join_hint())
    if not command.args:
        return await m.reply("Usage: <code>/delete &lt;id&gt;</code>", parse_mode="HTML")
    image_id = command.args.strip()
    row = await vault.get_image(image_id)
    if row is None:
        return await m.reply(f"❌ No image <code>#{image_id}</code>.")
    await _remove_channel_post(bot, vault, row)
    await vault.delete_image(image_id)
    await m.reply(f"🧹 <code>#{image_id}</code> shredded. Ciphertext is gone.")


@router.message(Command("wipe"))
async def cmd_wipe(m: Message, bot: Bot, vault: Vault):
    if not await _is_allowed(bot, vault, m.from_user.id if m.from_user else None):
        return await m.reply(_join_hint())
    n = await vault.count()
    if n == 0:
        return await m.reply("🗂 Nothing to wipe.")
    await m.reply(f"💥 Shred <b>all {n} encrypted item(s)</b>? This cannot be undone.",
                  parse_mode="HTML", reply_markup=_confirm_wipe_kb())


@router.message(Command("publish"))
async def cmd_publish(m: Message, command: CommandObject, bot: Bot, vault: Vault):
    if not await _is_allowed(bot, vault, m.from_user.id if m.from_user else None):
        return await m.reply(_join_hint())
    if not command.args:
        return await m.reply("Usage: <code>/publish &lt;id&gt;</code>", parse_mode="HTML")
    image_id = command.args.strip()
    row = await vault.get_image(image_id)
    if row is None:
        return await m.reply(f"❌ No image <code>#{image_id}</code>.")
    if row["channel_msg_id"]:
        return await m.reply(f"ℹ️ <code>#{image_id}</code> is already in the channel.")
    await _post_preview(bot, vault, image_id, row["preview"])
    await m.reply(f"📢 Preview for <code>#{image_id}</code> posted to the channel.",
                  parse_mode="HTML")


@router.message(Command("unpublish"))
async def cmd_unpublish(m: Message, command: CommandObject, bot: Bot, vault: Vault):
    if not await _is_allowed(bot, vault, m.from_user.id if m.from_user else None):
        return await m.reply(_join_hint())
    if not command.args:
        return await m.reply("Usage: <code>/unpublish &lt;id&gt;</code>", parse_mode="HTML")
    image_id = command.args.strip()
    row = await vault.get_image(image_id)
    if row is None:
        return await m.reply(f"❌ No image <code>#{image_id}</code>.")
    await _remove_channel_post(bot, vault, row)
    await m.reply(f"🧹 Channel preview for <code>#{image_id}</code> removed.",
                  parse_mode="HTML")


@router.message()
async def catch_all(m: Message, bot: Bot, vault: Vault):
    if await _is_allowed(bot, vault, m.from_user.id if m.from_user else None):
        await m.reply(
            "🤖 Send me an image to seal it, or use /help.\n"
            "List what is sealed with /list.")
    else:
        await m.reply(_join_hint())


# --------------------------------------------------------------------------
# handlers: inline buttons
# --------------------------------------------------------------------------

@router.callback_query(F.data.startswith("get:"))
async def cb_get(cq: CallbackQuery, bot: Bot, vault: Vault, crypto: VaultCrypto):
    if not await _is_allowed(bot, vault, cq.from_user.id):
        return await cq.answer(_join_hint(), show_alert=True)
    image_id = cq.data.split(":", 1)[1]
    await cq.answer(f"🔓 Decrypting #{image_id}…")
    res = await _deliver(bot, vault, crypto, cq.from_user.id, image_id)
    if res is not True:
        await bot.send_message(cq.from_user.id, res, parse_mode="HTML")


@router.callback_query(F.data.startswith("zk:"))
async def cb_zk(cq: CallbackQuery, bot: Bot, vault: Vault, crypto: VaultCrypto):
    if not await _is_allowed(bot, vault, cq.from_user.id):
        return await cq.answer(_join_hint(), show_alert=True)
    image_id = cq.data.split(":", 1)[1]
    await cq.answer("🕶 Building a one-time link…")
    res = await _send_zk_link(bot, vault, crypto, cq.from_user.id, image_id)
    if res is not True:
        await bot.send_message(cq.from_user.id, res, parse_mode="HTML")


@router.callback_query(F.data == "wipeall:yes")
async def cb_wipe_yes(cq: CallbackQuery, bot: Bot, vault: Vault):
    if not await _is_allowed(bot, vault, cq.from_user.id):
        return await cq.answer(_join_hint(), show_alert=True)
    for row in await vault.list_images(limit=1000):
        await _remove_channel_post(bot, vault, row)
    n = await vault.wipe_all()
    await cq.answer()
    await cq.message.edit_text(f"💥 Vault wiped — {n} item(s) shredded irrecoverably.")


@router.callback_query(F.data == "wipeall:no")
async def cb_wipe_no(cq: CallbackQuery):
    await cq.answer()
    await cq.message.edit_text("🧊 Wipe cancelled. Nothing was deleted.")


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------

async def main() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    config.validate()
    for issue in config.problems():
        log.warning(issue)

    vault = Vault(config.DB_PATH)
    await vault.open()

    salt_hex = await vault.get_meta("argon_salt")
    if not salt_hex:
        salt_hex = new_salt().hex()
        await vault.set_meta("argon_salt", salt_hex)
    crypto = VaultCrypto(config.MASTER_PASSPHRASE, bytes.fromhex(salt_hex))

    bot = Bot(config.BOT_TOKEN)
    dp = Dispatcher()
    dp["vault"] = vault
    dp["crypto"] = crypto
    dp.include_router(router)

    runner = None
    if config.BASE_URL:
        app = build_app(vault)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", config.WEB_PORT)
        await site.start()
        log.info("🕶 zero-knowledge viewer listening on :%d (put it behind HTTPS)",
                 config.WEB_PORT)

    owner = await vault.get_meta("owner_id")
    log.info("Vault ready: %d sealed item(s) · owner=%s",
             await vault.count(), owner or config.OWNER_ID or
             "unclaimed (first /start claims it)")

    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        if runner:
            await runner.cleanup()
        await vault.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
