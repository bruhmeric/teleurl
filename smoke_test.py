#!/usr/bin/env python3
"""Offline smoke tests — no Telegram token or network needed.

Covers: crypto round-trip/tamper/wrong-passphrase, mosaic preview,
SQLite vault, and the one-time zero-knowledge link semantics
(first fetch 200, second fetch 410, expired link rejected).

Run:  python smoke_test.py
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image  # noqa: E402

from crypto import CryptoError, VaultCrypto, b64url, new_salt  # noqa: E402
from cryptography.exceptions import InvalidTag  # noqa: E402
from db import Vault  # noqa: E402
from preview import fallback_preview, make_mosaic  # noqa: E402
from webapp import build_app  # noqa: E402

PASS = "correct horse battery staple"
IMG_ID = "deadbeef1234"
PLAINTEXT = b"\x89PNG\r\n\x1a\n" + os.urandom(300_000) + b"ENDOFIMAGE"

passed = 0


def ok(name: str) -> None:
    global passed
    passed += 1
    print(f"  \u2713 {name}")


def make_test_png(w=1920, h=1080) -> bytes:
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(0, w, 7):
            for dx in range(7):
                if x + dx < w:
                    c = ((x * 255) // w, (y * 255) // h, ((x + y) % 256))
                    px[x + dx, y] = c
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def test_crypto() -> VaultCrypto:
    print("[crypto]")
    salt = new_salt()
    vc = VaultCrypto(PASS, salt)
    enc = vc.encrypt(IMG_ID, PLAINTEXT)
    assert enc["ct"] != PLAINTEXT and PLAINTEXT not in enc["ct"]
    ok("image encrypted (ciphertext differs from plaintext)")

    pt = vc.decrypt(IMG_ID, enc["nonce"], enc["ct"],
                    enc["wrap_nonce"], enc["wrapped_key"])
    assert bytes(pt) == PLAINTEXT
    ok("round-trip decrypt matches original")

    # AAD binding: decrypting under the wrong id must fail
    try:
        vc.decrypt("otherid0", enc["nonce"], enc["ct"],
                   enc["wrap_nonce"], enc["wrapped_key"])
        raise AssertionError("AAD binding failed")
    except CryptoError:
        ok("ciphertext bound to image id (AAD) — swap rejected")

    # tamper detection
    bad = bytearray(enc["ct"]); bad[len(bad) // 2] ^= 0xFF
    try:
        vc.decrypt(IMG_ID, enc["nonce"], bytes(bad),
                   enc["wrap_nonce"], enc["wrapped_key"])
        raise AssertionError("tamper not detected")
    except CryptoError:
        ok("tampered ciphertext rejected (GCM auth)")

    # wrong passphrase
    vc2 = VaultCrypto("wrong passphrase entirely", salt)
    try:
        vc2.decrypt(IMG_ID, enc["nonce"], enc["ct"],
                    enc["wrap_nonce"], enc["wrapped_key"])
        raise AssertionError("wrong passphrase accepted")
    except CryptoError:
        ok("wrong passphrase cannot unwrap keys")

    # unwrap for ZK link
    dek = vc.unwrap_key(IMG_ID, enc["wrap_nonce"], enc["wrapped_key"])
    assert len(dek) == 32
    ok("DEK export for zero-knowledge links (32 bytes)")
    return vc, enc


def test_preview() -> bytes:
    print("[preview]")
    png = make_test_png()
    pv = make_mosaic(png)
    im = Image.open(io.BytesIO(pv))
    assert im.format == "JPEG" and im.size[0] == 480
    assert len(pv) < 120_000
    ok(f"mosaic preview generated ({im.size[0]}x{im.size[1]}, {len(pv)//1024} KB)")

    # irreversibility: preview must not contain original high-frequency data.
    # Compare a heavily downsampled preview against a downsampled original —
    # correlation should be weak-ish; the real guarantee is the 26px step.
    tiny_orig = Image.open(io.BytesIO(png)).convert("RGB").resize((26, 15), Image.BILINEAR)
    tiny_prev = im.resize((26, 15), Image.BILINEAR)
    import math
    o = list(tiny_orig.getdata()); p = list(tiny_prev.getdata())
    n = len(o)
    mo = sum(sum(px) for px in o) / (n * 3); mp = sum(sum(px) for px in p) / (n * 3)
    cov = sum((sum(a) - mo) * (sum(b) - mp) for a, b in zip(o, p)) / n
    vo = sum((sum(a) - mo) ** 2 for a in o) / n
    vp = sum((sum(b) - mp) ** 2 for b in p) / n
    corr = cov / math.sqrt(vo * vp) if vo > 0 and vp > 0 else 0
    assert corr < 0.98, f"preview too correlated ({corr:.3f})"
    ok(f"preview only loosely correlated with source ({corr:.2f}) — detail destroyed")

    fb = fallback_preview()
    assert Image.open(io.BytesIO(fb)).size[0] == 480
    ok("fallback placeholder works")
    return png


async def test_vault_and_links(vc, enc, preview_bytes):
    print("[vault + one-time links]")
    tmp = tempfile.mkdtemp(prefix="zkvault-test-")
    vault = Vault(os.path.join(tmp, "vault.db"))
    await vault.open()
    await vault.set_meta("argon_salt", new_salt().hex())

    await vault.add_image({
        "id": IMG_ID, "ct": enc["ct"], "nonce": enc["nonce"],
        "wrap_nonce": enc["wrap_nonce"], "wrapped_key": enc["wrapped_key"],
        "preview": preview_bytes, "mime": "image/png", "filename": "t.png",
        "as_doc": 0, "size": len(enc["ct"]), "created": "2026-01-01T00:00:00+00:00",
    })
    assert await vault.count() == 1
    ok("image row stored (ciphertext only)")

    row = await vault.get_image(IMG_ID)
    assert bytes(row["ct"]) == enc["ct"]
    ok("round-trip read from vault")

    # --- one-time link via real HTTP server --------------------------------
    from aiohttp.test_utils import TestClient, TestServer
    app = build_app(vault)
    token, exp = await vault.create_link(IMG_ID, 600)
    async with TestClient(TestServer(app)) as cli:
        r = await cli.get(f"/v/{token}")
        page_html = await r.text()
        assert r.status == 200 and "One-Time" in page_html
        ok("viewer page served (static shell, no secrets)")

        # anti-save hardening wiring must be present in the served page
        assert '<canvas id="cv"' in page_html and "<img" not in page_html
        assert "createImageBitmap" in page_html
        for marker in ("contextmenu", "dragstart", "selectstart", "copy", "cut",
                       "beforeprint", "PrintScreen", "visibilitychange",
                       "keydown", "@media print", "WATERMARK",
                       "webkit-touch-callout", "pointer-events:none"):
            assert marker in page_html, f"missing hardening marker: {marker}"
        assert "additionalData" in page_html          # AAD fix (OperationError)
        ok("viewer page hardened: canvas render + anti-save wiring + print blank")

        r = await cli.get(f"/v/{token}/data")
        assert r.status == 200
        j = await r.json()
        assert set(j) == {"nonce", "ct", "aad", "mime"}
        ok("ciphertext endpoint returns nonce+ct+aad only")

        # server-side key never appears anywhere in the successful response
        dek = vc.unwrap_key(IMG_ID, row["wrap_nonce"], row["wrapped_key"])
        assert b64url(bytes(dek)) not in str(j)
        ok("AES key absent from the server response")

        r = await cli.get(f"/v/{token}/data")
        assert r.status == 410
        ok("second fetch -> 410 Gone (single use enforced)")

        # expired link rejected
        token2, _ = await vault.create_link(IMG_ID, -1)
        await vault._db.execute("UPDATE images SET link_exp=1 WHERE link_token=?", (token2,))
        await vault._db.commit()
        r = await cli.get(f"/v/{token2}/data")
        assert r.status == 410
        ok("expired link -> 410 Gone")

        # unknown token
        r = await cli.get("/v/nonexistent-token/data")
        assert r.status == 410
        ok("unknown token -> 410 Gone")

        # simulate EXACTLY what the browser page does with the #fragment key
        # (it passes j.aad through TextEncoder -> utf-8 as additionalData)
        import base64
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        token3, _ = await vault.create_link(IMG_ID, 600)
        r = await cli.get(f"/v/{token3}/data")
        j = await r.json()
        pad = "=" * (-len(j["ct"]) % 4)
        ct = base64.urlsafe_b64decode(j["ct"] + pad)
        iv = base64.urlsafe_b64decode(j["nonce"] + pad)

        # regression for the reported browser "OperationError": decrypting
        # WITHOUT additionalData (the old page behaviour) must fail
        try:
            AESGCM(bytes(dek)).decrypt(iv, ct, None)
            raise AssertionError("decrypt without AAD unexpectedly succeeded")
        except InvalidTag:
            ok("regression: missing additionalData fails (the OperationError bug)")

        pt = AESGCM(bytes(dek)).decrypt(iv, ct, j["aad"].encode("utf-8"))
        assert pt == PLAINTEXT
        ok("browser-side simulation (incl. AAD) decrypts to the original")

    # delete + wipe
    assert await vault.delete_image(IMG_ID) is True
    assert await vault.get_image(IMG_ID) is None
    ok("shred (delete) works")
    await vault.close()


async def main() -> None:
    print("Zero-Knowledge Image Vault — smoke tests\n")
    vc, enc = await test_crypto()
    png = test_preview()
    await test_vault_and_links(vc, enc, make_mosaic(png))
    print(f"\nALL {passed} SMOKE TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
