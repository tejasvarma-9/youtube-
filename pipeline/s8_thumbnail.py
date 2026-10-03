"""Stage 8: thumbnail, 1280x720.

Gemini draws the subject (no text, one normal-price request). The headline,
red underline and coin mark are drawn by code, so the lettering is always sharp
and every thumbnail has the same layout.
Writes thumbnail_art.png and thumbnail.png.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from . import config, s5_images, stubs
from .common import StageError, Video, log

W, H = 1280, 720
INK = (17, 17, 17)
RED = (214, 40, 40)
PAPER = (246, 248, 251)
GRID = (222, 229, 238)

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


def compose(art_path, headline: str, out_path) -> None:
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    for x in range(0, W, 40):
        d.line([(x, 0), (x, H)], fill=GRID, width=1)
    for y in range(0, H, 40):
        d.line([(0, y), (W, y)], fill=GRID, width=1)

    # Subject art fills the lower part of the frame, under the headline.
    art = Image.open(art_path).convert("RGB")
    box_w, box_h = 1180, 470
    art.thumbnail((box_w, box_h), Image.LANCZOS)
    img.paste(art, ((W - art.width) // 2, H - art.height - 20))

    # Headline: biggest size that fits in two lines.
    for size in range(104, 50, -4):
        font = _font(size)
        lines = _balanced(d, headline, font, W - 100)
        if lines:
            break
    else:
        lines = _wrap(d, headline, font, W - 100)[:2]
    y = 34
    for line in lines:
        bbox = d.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        x = (W - tw) // 2
        d.rectangle([x - 18, y - 6, x + tw + 18, y + (bbox[3] - bbox[1]) + 22], fill=PAPER)
        d.text((x, y - bbox[1]), line, font=font, fill=INK)
        y += (bbox[3] - bbox[1]) + 22
    last_w = d.textlength(lines[-1], font=font)
    d.rectangle([(W - last_w) // 2, y - 10, (W + last_w) // 2, y - 2], fill=RED)

    # Coin mark, bottom right.
    if config.LOGO.exists():
        logo = Image.open(config.LOGO).convert("RGBA").resize((96, 96), Image.LANCZOS)
        mask = Image.new("L", logo.size, 0)
        ImageDraw.Draw(mask).ellipse([0, 0, 95, 95], fill=255)
        img.paste(logo, (W - 96 - 22, H - 96 - 22), mask)
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
            prompt = (f"{meta['thumbnail_subject']}\n\nNo text, no letters, no logos, no faces looking at the camera. "
                      "Leave the top third of the image plain and empty. " + config.IMAGE_STYLE_SUFFIX)
            data = s5_images.generate_sync(s5_images.build_request(prompt, refs))
            if not data:
                raise StageError("Gemini returned no thumbnail image. Edit thumbnail_subject in metadata.json and re-run.")
            art.write_bytes(data)
    compose(art, meta["thumbnail_text"], video.path("thumbnail.png"))
    log("  thumbnail.png written (delete thumbnail_art.png to redraw the art)")
