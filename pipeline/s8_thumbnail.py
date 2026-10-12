"""Stage 8: thumbnail, 1280x720.

Gemini draws a before/after split scene on a dark background (no text, one normal-price request).
The headline and coin mark are drawn by code, so the lettering is always sharp
and every thumbnail has the same layout: headline on top, before/after split below.
Writes thumbnail_art.png and thumbnail.png.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from . import config, s5_images, stubs
from .common import StageError, Video, log

W, H = 1280, 720
THUMB_STYLE = (
    "Style: glossy, high-detail digital illustration with vivid saturated colors, bold outlines and glowing highlights. "
    "Split-screen composition: the left half is the 'before' (bad, costly, red and orange tones), the right half is the "
    "'after' (good, profitable, green and gold tones), divided by a bright diagonal line, with a dark navy background. "
    "Use only generic stand-ins: no real brands, logos, posters, show titles or real people. "
    "Keep the top 28 percent of the frame plain dark navy. 16:9. No text, no letters, no numbers."
)
INK = (10, 10, 10)
NAVY = (12, 22, 48)
WHITE = (255, 255, 255)
YELLOW = (255, 214, 0)

FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(size: int):
    for f in FONTS:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])


def _balanced(draw, text: str, font, max_w: int) -> list[str] | None:
    """One line if it fits, else the two-line split with the most even widths."""
    if draw.textlength(text, font=font) <= max_w:
        return [text]
    words, best = text.split(), None
    for k in range(1, len(words)):
        a, b = " ".join(words[:k]), " ".join(words[k:])
        wa, wb = draw.textlength(a, font=font), draw.textlength(b, font=font)
        if max(wa, wb) <= max_w and (best is None or max(wa, wb) < best[0]):
            best = (max(wa, wb), [a, b])
    return best[1] if best else None


def _label(d, text: str, cx: int, cy: int) -> None:
    font = _font(40)
    box = d.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    d.rounded_rectangle([cx - tw // 2 - 18, cy - th // 2 - 14, cx + tw // 2 + 18, cy + th // 2 + 14],
                        radius=10, fill=WHITE, outline=INK, width=4)
    d.text((cx - tw // 2, cy - th // 2 - box[1]), text, font=font, fill=INK)


def compose(art_path, headline: str, out_path, before: str = "", after: str = "") -> None:
    """Art fills the frame; a dark fade at the top carries a big outlined headline."""
    art = Image.open(art_path).convert("RGB")
    scale = max(W / art.width, H / art.height)
    art = art.resize((round(art.width * scale), round(art.height * scale)), Image.LANCZOS)
    left, top = (art.width - W) // 2, (art.height - H) // 2
    img = art.crop((left, top, left + W, top + H))

    fade = Image.new("L", (1, H))
    for y in range(H):
        fade.putpixel((0, y), max(0, min(235, round(235 * (1 - y / (H * 0.42))))))
    img = Image.composite(Image.new("RGB", (W, H), NAVY), img, fade.resize((W, H)))
    d = ImageDraw.Draw(img)

    # Headline: biggest size that fits across the top in at most two lines.
    max_w = W - 100
    for size in range(130, 60, -6):
        font = _font(size)
        lines = _wrap(d, headline, font, max_w)
        if len(lines) <= 2 and all(d.textlength(ln, font=font) <= max_w for ln in lines):
            break
    else:
        lines = _wrap(d, headline, font, max_w)[:2]
    stroke = max(6, size // 12)
    y = 22
    for i, line in enumerate(lines):
        bbox = d.textbbox((0, 0), line, font=font)
        fill = YELLOW if i == len(lines) - 1 else WHITE
        d.text(((W - (bbox[2] - bbox[0])) // 2, y - bbox[1]), line, font=font, fill=fill,
               stroke_width=stroke, stroke_fill=INK)
        y += (bbox[3] - bbox[1]) + round(size * 0.12)

    if before and after:
        _label(d, before.upper(), 190, H - 60)
        _label(d, after.upper(), W - 190, H - 60)

    if config.LOGO.exists():
        logo = Image.open(config.LOGO).convert("RGBA").resize((84, 84), Image.LANCZOS)
        mask = Image.new("L", logo.size, 0)
        ImageDraw.Draw(mask).ellipse([0, 0, 83, 83], fill=255)
        img.paste(logo, (W // 2 - 42, H - 84 - 18), mask)
    img.save(out_path)


def run(video: Video, stub: bool = False) -> None:
    meta = video.read_json("metadata.json")
    art = video.path("thumbnail_art.png")
    if not art.exists():
        if stub:
            stubs.image_card(meta["thumbnail_subject"], art, label="thumbnail")
        else:
            if not config.GOOGLE_API_KEY:
                raise StageError("GOOGLE_API_KEY is empty. Put it in the .env file (see README), or run with --stub.")
            refs = [s5_images.upload_file(p) for p in s5_images.style_refs()]
            prompt = (f"{meta['thumbnail_subject']}\n\n{THUMB_STYLE}")
            data = s5_images.generate_sync(s5_images.build_request(prompt, refs))
            if not data:
                raise StageError("Gemini returned no thumbnail image. Edit thumbnail_subject in metadata.json and re-run.")
            art.write_bytes(data)
    compose(art, meta["thumbnail_text"], video.path("thumbnail.png"),
            meta.get("thumbnail_before", ""), meta.get("thumbnail_after", ""))
    log("  thumbnail.png written (delete thumbnail_art.png to redraw the art)")
