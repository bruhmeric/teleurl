"""Async SQLite vault.  Stores ONLY ciphertext + wrapped keys + tiny previews.

Nothing that goes into this database is decryptable without the
MASTER_PASSPHRASE (see crypto.py).  One-time web links are claimed atomically
so a link can never be fetched twice.
"""
from __future__ import annotations

import secrets
import time
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS images (
    id             TEXT PRIMARY KEY,   -- short random id shown to the user
    ct             BLOB NOT NULL,      -- AES-256-GCM ciphertext of the image
    nonce          BLOB NOT NULL,      -- 12-byte GCM nonce (image layer)
    wrap_nonce     BLOB NOT NULL,      -- 12-byte GCM nonce (key wrap layer)
    wrapped_key    BLOB NOT NULL,      -- DEK wrapped under the Argon2id KEK
    preview        BLOB NOT NULL,      -- irreversible mosaic JPEG (tiny)
    mime           TEXT NOT NULL,
    filename       TEXT,
    as_doc         INTEGER NOT NULL DEFAULT 0,  -- deliver as document (no recompression)
    size           INTEGER NOT NULL,
    created        TEXT NOT NULL,      -- UTC ISO timestamp
    channel_msg_id INTEGER,            -- blurred-preview post in the channel
    link_token     TEXT,               -- current one-time web link token
    link_used      INTEGER NOT NULL DEFAULT 0,
    link_exp       INTEGER             -- unix seconds
);
"""


class Vault:
    def __init__(self, path: str):
        self._path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # -- meta ----------------------------------------------------------------
    async def get_meta(self, key: str) -> Optional[str]:
        cur = await self._db.execute("SELECT value FROM meta WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await self._db.commit()

    # -- images --------------------------------------------------------------
    async def add_image(self, row: dict[str, Any]) -> None:
        await self._db.execute(
            """INSERT INTO images
               (id, ct, nonce, wrap_nonce, wrapped_key, preview, mime, filename,
                as_doc, size, created)
               VALUES (:id, :ct, :nonce, :wrap_nonce, :wrapped_key, :preview,
                       :mime, :filename, :as_doc, :size, :created)""",
            row)
        await self._db.commit()

    async def get_image(self, image_id: str) -> Optional[aiosqlite.Row]:
        cur = await self._db.execute("SELECT * FROM images WHERE id=?", (image_id,))
        return await cur.fetchone()

    async def list_images(self, limit: int = 50) -> list[aiosqlite.Row]:
        cur = await self._db.execute(
            "SELECT id, mime, size, created, channel_msg_id FROM images "
            "ORDER BY created DESC LIMIT ?", (limit,))
        return list(await cur.fetchall())

    async def count(self) -> int:
        cur = await self._db.execute("SELECT COUNT(*) AS n FROM images")
        row = await cur.fetchone()
        return row["n"]

    async def delete_image(self, image_id: str) -> bool:
        cur = await self._db.execute("DELETE FROM images WHERE id=?", (image_id,))
        await self._db.commit()
        return cur.rowcount == 1

    async def wipe_all(self) -> int:
        cur = await self._db.execute("DELETE FROM images")
        await self._db.commit()
        return cur.rowcount

    async def set_channel_msg(self, image_id: str, msg_id: Optional[int]) -> None:
        await self._db.execute(
            "UPDATE images SET channel_msg_id=? WHERE id=?", (msg_id, image_id))
        await self._db.commit()

    # -- one-time links --------------------------------------------------------
    async def create_link(self, image_id: str, ttl_seconds: int) -> tuple[str, int]:
        """(Re)issue a one-time link token for an image."""
        token = secrets.token_urlsafe(18)
        exp = int(time.time()) + max(30, ttl_seconds)
        await self._db.execute(
            "UPDATE images SET link_token=?, link_used=0, link_exp=? WHERE id=?",
            (token, exp, image_id))
        await self._db.commit()
        return token, exp

    async def claim_link(self, token: str) -> Optional[aiosqlite.Row]:
        """Atomically consume a link token.  Returns the row once, else None.

        The UPDATE ... WHERE guard makes double-fetches impossible even under
        concurrent requests: only the first caller flips link_used 0 -> 1.
        """
        cur = await self._db.execute(
            "UPDATE images SET link_used=1 "
            "WHERE link_token=? AND link_used=0 AND link_exp>?",
            (token, int(time.time())))
        if cur.rowcount != 1:
            await self._db.commit()
            return None
        cur = await self._db.execute(
            "SELECT * FROM images WHERE link_token=?", (token,))
        row = await cur.fetchone()
        await self._db.commit()
        return row
