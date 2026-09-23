"""Irreversible mosaic previews.

The preview is the ONLY derived visual artifact of the original image and is
safe to publish in a channel because the transformation destroys information
on purpose:

    original -> downscale to ~26 px wide (BILINEAR)  [information thrown away]
            -> upscale to 480 px (NEAREST)           [mosaic blocks]
            -> Gaussian blur                         [no structure left]
            -> "ENCRYPTED" watermark band

There is nothing to de-convolve: the fine detail no longer exists in the
pixels.  The preview is re-encoded as a fresh JPEG, so EXIF and any embedded
thumbnails of the original are gone too.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFilter

OUT_W = 480
BLOCKS = 26        # mosaic resolution: image is reduced to 26 px wide first
SOFTEN = 5.0       # final Gaussian blur sigma over the mosaic


def _font(size: int):
    try:
        from PIL import ImageFont
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        except OSError:
            return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _watermark(im: Image.Image) -> None:
    d = ImageDraw.Draw(im, "RGBA")
    w, h = im.size
    band_h = max(34, h // 6)
    d.rectangle([0, h - band_h, w, h], fill=(10, 12, 16, 165))

    cx, cy = 30, h - band_h // 2
    r = 11
    # padlock: shackle arc + body
    d.arc([cx - r, cy - r - 7, cx + r, cy + r - 7],
          start=180, end=360, fill=(240, 240, 245, 220), width=4)
    d.rounded_rectangle([cx - r, cy - 5, cx + r, cy + 11],
                        radius=4, fill=(240, 240, 245, 220))

    text = "ENCRYPTED PREVIEW"
    f = _font(max(15, band_h // 2))
    try:
        tw = d.textlength(text, font=f)
        th = f.size if hasattr(f, "size") else 14
    except Exception:
        tw, th = 15 * len(text), 14
    d.text((cx + r + 14, cy - th // 2), text, font=f, fill=(235, 235, 240, 230))


def make_mosaic(image_bytes: bytes, out_w: int = OUT_W) -> bytes:
    """Produce a small irreversible mosaic JPEG from raw image bytes."""
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")

    ratio = img.height / max(img.width, 1)
    tiny = img.resize((BLOCKS, max(1, round(BLOCKS * ratio))), Image.BILINEAR)
    out_h = max(1, round(out_w * tiny.height / max(tiny.width, 1)))
    big = tiny.resize((out_w, out_h), Image.NEAREST)
    big = big.filter(ImageFilter.GaussianBlur(SOFTEN))
    _watermark(big)

    buf = io.BytesIO()
    big.save(buf, "JPEG", quality=70, optimize=True)
    return buf.getvalue()


def fallback_preview() -> bytes:
    """Gray placeholder used if the original cannot be decoded for preview."""
    big = Image.new("RGB", (OUT_W, 320), (38, 41, 48))
    _watermark(big)
    buf = io.BytesIO()
    big.save(buf, "JPEG", quality=70)
    return buf.getvalue()
