"""Animated infographic cards, drawn in code so every word and number is exact.

A card is a small dict (see KINDS). The shots stage may propose one for a shot whose narration is mostly
about a comparison, one big number, parts of a whole, a path the money takes, a quote from a filing, or
a short list. validate() refuses any card that shows a number the narrator does not say in that shot, so a
card can never put a figure on screen that the fact-check did not see. The image model never draws them.

    render_frame(card, p)  one frame, p = 0..1 is how far the build-up animation has got (bars fill, boxes pop in)
    render(card, path)     the finished card as a PNG (what shots 'image' files hold, and what Shorts use)
    validate(card, narration) -> list of problems (empty means the card is fine)
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1920, 1080
BG, INK, MUTED, ACCENT = "#EAF0F6", "#14202B", "#76879A", "#E4572E"
TRACK, BLUE_L, BLUE_D, MONEY, PANEL = "#D3DEEA", "#9CC1E3", "#2F6DB5", "#2FA36B", "#FFFFFF"
SEG_COLORS = [ACCENT, BLUE_D, MONEY, BLUE_L, MUTED]
HEAVY = ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Impact.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
BOLD = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
ANIM_SECONDS = 1.6


def _font(paths, size):
    for p in paths:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def _ease(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _stage(p, i, n):
    """Progress of item i of n when the whole card is at progress p (items start one after another)."""
    return _ease(p * (n + 1.2) - i)


def _center(d, text, y, font, fill, x0=0, x1=W):
    w = d.textlength(text, font=font)
    d.text(((x0 + x1 - w) / 2, y), text, font=font, fill=fill)


def _wrap(d, text, font, width):
    lines, cur = [], ""
    for w in text.split():
        t = f"{cur} {w}".strip()
        if cur and d.textlength(t, font=font) > width:
            lines.append(cur)
            cur = w
        else:
            cur = t
    return lines + [cur] if cur else lines


def _fit(d, text, paths, size, width, floor=30):
    """The largest font size (down to floor) at which text fits in width."""
    while size > floor and d.textlength(text, font=_font(paths, size)) > width:
        size -= 4
    return _font(paths, size)


def _title(d, text, y=100, size=96, p=1.0):
    text = text.upper()
    f = _fit(d, text, HEAVY, size, 1700, 48)
    _center(d, text, y - int((1 - _ease(p * 4)) * 40), f, INK)


def _fmt(v):
    return str(int(v)) if isinstance(v, (int, float)) and float(v) == int(v) else str(v)


# ---------------------------------------------------------------- the kinds

def _counter(d, c, p):
    """One big number that counts up, with a short label under it."""
    _title(d, c["title"], p=p)
    value = c["value"]
    m = re.match(r"^(\D*?)(\d[\d,]*\.?\d*)(.*)$", value)
    if m:
        pre, num, suf = m.groups()
        dec = len(num.split(".")[1]) if "." in num else 0
        cur = float(num.replace(",", "")) * _ease(p * 1.3)
        shown = pre + (f"{cur:,.{dec}f}" if "," in num else f"{cur:.{dec}f}") + suf
        full = value
    else:
        shown = full = value
    f = _fit(d, full, HEAVY, 330, 1650, 120)
    fw = d.textlength(full, font=f)
    # the digits are drawn left-aligned inside the final width, so they don't jitter while counting
    d.text(((W - fw) / 2, 300), shown, font=f, fill=ACCENT)
    if p > 0.45:
        lab = _font(BOLD, 68)
        y = 300 + f.size + 60
        for ln in _wrap(d, c["label"], lab, 1500)[:3]:
            _center(d, ln, y, lab, INK)
            y += 88
    if c.get("sub") and p > 0.8:
        _center(d, c["sub"], 900, _font(BOLD, 52), MUTED)


def _bars(d, c, p):
    _title(d, c["title"], p=p)
    rows, mx = c["rows"], max(float(v) for _, v in c["rows"])
    label, val = _font(BOLD, 58), _font(HEAVY, 54)
    x0, x1 = 240, 1680
    n = len(rows)
    step = min(215, 640 // max(n, 1))
    for i, (name, v) in enumerate(rows):
        t = _stage(p, i, n)
        y = 300 + i * step
        d.text((x0, y), name, font=label, fill=INK)
        d.rectangle((x0, y + 78, x1, y + 78 + 84), fill=PANEL, outline=INK, width=7)
        fw = int((x1 - x0 - 14) * float(v) / mx * t)
        if fw > 0 or t > 0:
            fw = max(fw, 6 if float(v) > 0 and t > 0.2 else 0)
        if fw > 0:
            d.rectangle((x0 + 7, y + 85, x0 + 7 + fw, y + 155), fill=ACCENT if float(v) == mx else BLUE_L)
            d.line((x0 + 7 + fw, y + 78, x0 + 7 + fw, y + 162), fill=INK, width=7)
        txt = f"{_fmt(v)}{c.get('unit', '')}".strip()
        if t > 0.6:
            tw = d.textlength(txt, font=val)
            tx = min(x0 + 7 + fw + 24, x1 - 24 - tw)
            d.text((tx, y + 94), txt, font=val, fill=INK)


def _stack(d, c, p):
    """One long bar split into parts of a whole (like 22% + 14% + everyone else)."""
    _title(d, c["title"], p=p)
    segs, unit = c["segments"], c.get("unit", "")
    total = float(c.get("total") or 100)
    known = sum(float(s["value"]) for s in segs if s.get("value") is not None)
    vals = [float(s["value"]) if s.get("value") is not None else max(total - known, 0.0) for s in segs]
    x0, x1, y0, y1 = 160, 1760, 380, 560
    d.rectangle((x0, y0, x1, y1), fill=PANEL, outline=INK, width=7)
    reveal = _ease(p * 1.15)
    x = x0
    for i, (s, v) in enumerate(zip(segs, vals)):
        w = (x1 - x0) * v / total
        end = min(x + w, x0 + (x1 - x0) * reveal)
        if end > x:
            d.rectangle((x + 4, y0 + 4, end - 2 if end < x + w else end - 4, y1 - 4), fill=SEG_COLORS[i % len(SEG_COLORS)])
        if i and end > x:
            d.line((x, y0, x, y1), fill=INK, width=6)
        if reveal * (x1 - x0) + x0 > x + w * 0.5:
            cx = x + w / 2
            lvl = (i % 2) * 170
            d.line((cx, y1 + 8, cx, y1 + 40 + lvl), fill=INK, width=4)
            lf = _font(BOLD, 48)
            vf = _font(HEAVY, 66)
            lab = s["label"]
            lw = d.textlength(lab, font=lf)
            lx = max(60, min(cx - lw / 2, W - 60 - lw))
            d.text((lx, y1 + 48 + lvl), lab, font=lf, fill=INK)
            if s.get("value") is not None:
                txt = f"{_fmt(s['value'])}{unit}"
                tw = d.textlength(txt, font=vf)
                d.text((max(60, min(cx - tw / 2, W - 60 - tw)), y1 + 104 + lvl), txt, font=vf, fill=SEG_COLORS[i % len(SEG_COLORS)])
        x += w


def _arrow(d, x0, x1, y, t):
    xe = x0 + (x1 - x0) * t
    if xe - x0 < 6:
        return
    d.line((x0, y, xe, y), fill=INK, width=12)
    if t > 0.85:
        d.polygon([(x1 + 6, y), (x1 - 34, y - 26), (x1 - 34, y + 26)], fill=INK)


def _flow(d, c, p):
    """Two or three boxes joined by arrows: the path the money (or the cost) takes."""
    _title(d, c["title"], p=p)
    nodes, arrows = c["nodes"], c.get("arrows", [])
    n = len(nodes)
    bw = 460 if n == 2 else 320
    gap = (W - 2 * 130 - n * bw) / max(n - 1, 1)
    bh, cy = 220, 560
    total = 2 * n - 1
    for i, node in enumerate(nodes):
        t = _stage(p, 2 * i, total)
        x = 130 + i * (bw + gap)
        if t > 0:
            off = int((1 - t) * 60)
            d.rounded_rectangle((x, cy - bh / 2 + off, x + bw, cy + bh / 2 + off), 30, fill=PANEL, outline=INK, width=8)
            lab = node["label"].upper()
            f = _fit(d, lab, HEAVY, 64, bw - 50, 32)
            lines = _wrap(d, lab, f, bw - 50)[:2]
            yy = cy + off - len(lines) * f.size * 0.6
            for ln in lines:
                _center(d, ln, yy, f, ACCENT if i == 0 else INK, x, x + bw)
                yy += f.size * 1.2
        if i < n - 1:
            ta = _stage(p, 2 * i + 1, total)
            _arrow(d, x + bw + 14, x + bw + gap - 20, cy, ta)
            if ta > 0.5 and i < len(arrows):
                af = _font(BOLD, 44)
                lines = _wrap(d, arrows[i], af, gap - 24)
                if len(lines) > 3:
                    af = _font(BOLD, 38)
                    lines = _wrap(d, arrows[i], af, gap - 24)
                lines = lines[:5]
                step = af.size + 10
                yy = cy - 40 - len(lines) * step
                for ln in lines:
                    _center(d, ln, yy, af, INK, x + bw, x + bw + gap)
                    yy += step


def _compare(d, c, p):
    _title(d, c["title"], size=92, p=p)
    for i, col in enumerate(c["cols"]):
        t = _stage(p, i, 2)
        if t <= 0:
            continue
        x0, off = 120 + i * 880, int((1 - t) * 90)
        d.rounded_rectangle((x0, 300 + off, x0 + 800, 940 + off), 28, fill=PANEL, outline=INK, width=7)
        head = col["head"].upper()
        hf = _fit(d, head, HEAVY, 70, 720, 36)
        _center(d, head, 345 + off + (70 - hf.size) // 2, hf, ACCENT if i == 0 else BLUE_D, x0, x0 + 800)
        f, y = _font(BOLD, 50), 490 + off
        for line in col["lines"]:
            for ln in _wrap(d, line, f, 700):
                d.text((x0 + 50, y), ln, font=f, fill=INK)
                y += 66
            y += 40
    d.ellipse((880, 540, 1040, 700), fill=INK)
    _center(d, c.get("mid", "VS").upper(), 585, _font(HEAVY, 54), BG, 880, 1040)


def _list(d, c, p):
    _title(d, c["title"], p=p)
    f, n = _font(BOLD, 64), len(c["rows"])
    for i, row in enumerate(c["rows"]):
        t = _stage(p, i, n)
        if t <= 0:
            continue
        y = 330 + i * 140 + int((1 - t) * 50)
        d.rounded_rectangle((420, y + 12, 480, y + 72), 12, fill=ACCENT, outline=INK, width=5)
        d.text((530, y), row, font=f, fill=INK)


def _quote(d, c, p):
    f = _font(HEAVY, 108)
    words = c["text"].upper().split()
    lines = _wrap(d, c["text"].upper(), f, 1560)
    shown = max(1, round(len(words) * _ease(p * 1.3)))
    y = (H - len(lines) * 146) // 2 - 40
    used = 0
    for ln in lines:
        ws = ln.split()
        vis = " ".join(ws[: max(0, shown - used)])
        used += len(ws)
        if vis:
            d.text(((W - d.textlength(ln, font=f)) / 2, y), vis, font=f, fill=INK)
        y += 146
    if c.get("sub") and p > 0.7:
        _center(d, c["sub"], y + 40, _font(BOLD, 58), MUTED)


def _source(d, c, p):
    """A paper card pinned over a dark desk: a line straight from a filing, a rule or a press release."""
    layer = Image.new("RGBA", (1560, 760), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rectangle((30, 40, 1530, 730), fill="#F4F1E6", outline=INK, width=8)
    ld.rectangle((30, 40, 1530, 190), fill=ACCENT, outline=INK, width=8)
    head = c["header"].upper()
    hf = _fit(ld, head, HEAVY, 70, 1400, 36)
    ld.text(((1560 - ld.textlength(head, font=hf)) / 2, 66), head, font=hf, fill="#FFFFFF")
    mf = _font(HEAVY, 92)
    lines = _wrap(ld, c["main"], mf, 1300)
    while len(lines) > 4 and mf.size > 52:
        mf = _font(HEAVY, mf.size - 8)
        lines = _wrap(ld, c["main"], mf, 1300)
    y = 230 + max(0, (4 - len(lines))) * 40
    for ln in lines[:5]:
        ld.text(((1560 - ld.textlength(ln, font=mf)) / 2, y), ln, font=mf, fill=INK)
        y += int(mf.size * 1.22)
    ff = _font(BOLD, 40)
    ld.text((90, 660), c.get("foot_l", ""), font=ff, fill="#5A6573")
    fr = c.get("foot_r", "")
    ld.text((1470 - ld.textlength(fr, font=ff), 660), fr, font=ff, fill="#5A6573")
    for tx in (150, 1250):
        ld.rectangle((tx, 12, tx + 170, 78), fill=(236, 226, 190, 215), outline=(150, 140, 100, 255), width=3)
    t = _ease(p * 2.2)
    layer = layer.rotate(-1.4 + (1 - t) * 6, resample=Image.BICUBIC, expand=True)
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 120), mask=layer.split()[3])
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    return layer, shadow, ((W - layer.width) // 2, (H - layer.height) // 2 - 40 - int((1 - t) * 420))


KINDS = {"counter": _counter, "bars": _bars, "stack": _stack, "flow": _flow, "compare": _compare,
         "list": _list, "quote": _quote, "source": _source}
KINDS_HELP = list(KINDS)


def render_frame(card: dict, p: float = 1.0) -> Image.Image:
    kind = card["kind"]
    if kind == "source":
        img = Image.new("RGB", (W, H), "#2E3A48")
        vg = Image.radial_gradient("L").resize((W, H)).point(lambda v: int(150 * (v / 255) ** 2))
        img = Image.composite(Image.new("RGB", (W, H), "#161D26"), img, vg)
        layer, shadow, (ox, oy) = _source(ImageDraw.Draw(img), card, p)
        img.paste(shadow, (ox + 16, oy + 22), shadow)
        img.paste(layer, (ox, oy), layer)
        return img
    img = Image.new("RGB", (W, H), BG)
    KINDS[kind](ImageDraw.Draw(img), card, p)
    return img


def render(card: dict, path) -> None:
    render_frame(card, 1.0).save(path)


# ---------------------------------------------------------------- validation

_WORDS = {w: n for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_WORDS.update({w: 10 * n for n, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)})
_WORDS.update({"hundred": 100, "thousand": 1000, "million": 1e6, "billion": 1e9, "trillion": 1e12})


def numbers_in(text: str, words: bool = True) -> set[float]:
    """Every number in text, as digits ("36%", "$215.9 billion") or as a word ("two", "forty").
    words=False reads digits only; card text uses it, so a title like "Two more doors" or a label like "Q2"
    is not mistaken for a figure."""
    out = set()
    text = re.sub(r"\bQ[1-4]\b", " ", text)
    for m in re.findall(r"\d[\d,]*\.?\d*", text):
        try:
            out.add(float(m.replace(",", "").rstrip(".")))
        except ValueError:
            pass
    for w in (re.findall(r"[a-z]+", text.lower()) if words else []):
        if w in _WORDS:
            out.add(float(_WORDS[w]))
    return out


def _strings(x):
    if isinstance(x, bool) or x is None:
        return
    if isinstance(x, (int, float)):
        yield str(x)
    elif isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            if k not in ("kind", "unit", "total"):
                yield from _strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _strings(v)


def _need(c, key, kind=str, n=None):
    v = c.get(key)
    if not isinstance(v, kind) or (kind in (str, list) and not v):
        return f"'{key}' is missing"
    if n and isinstance(v, list) and not (n[0] <= len(v) <= n[1]):
        return f"'{key}' needs {n[0]} to {n[1]} items"
    return None


def validate(card, narration: str) -> list[str]:
    """Problems that make a card unsafe to show (empty list: fine). Never raises."""
    try:
        if not isinstance(card, dict) or card.get("kind") not in KINDS:
            return [f"unknown card kind (use one of {', '.join(KINDS)})"]
        k, bad = card["kind"], []
        if k in ("counter", "bars", "stack", "flow", "compare", "list"):
            bad.append(_need(card, "title"))
        if k == "counter":
            bad += [_need(card, "value"), _need(card, "label")]
        elif k == "bars":
            bad.append(_need(card, "rows", list, (2, 5)))
            rows = card.get("rows") or []
            if not all(isinstance(r, (list, tuple)) and len(r) == 2 and isinstance(r[1], (int, float)) and not isinstance(r[1], bool)
                       and isinstance(r[0], str) for r in rows):
                bad.append("each bars row must be [name, number]")
            elif max(float(r[1]) for r in rows) <= 0:
                bad.append("bars need a value above zero")
        elif k == "stack":
            bad.append(_need(card, "segments", list, (2, 5)))
            segs = card.get("segments") or []
            if not all(isinstance(s, dict) and isinstance(s.get("label"), str) and s["label"] for s in segs):
                bad.append("each stack segment needs a label")
            else:
                known = sum(float(s["value"]) for s in segs if isinstance(s.get("value"), (int, float)))
                if sum(1 for s in segs if s.get("value") is None) > 1:
                    bad.append("only one stack segment may leave its value out (the remainder)")
                if known > float(card.get("total") or 100) + 1e-9:
                    bad.append("stack values add up to more than the total")
                if any(s.get("value") is None for s in segs) and not card.get("total") and card.get("unit") != "%":
                    bad.append("a remainder segment needs unit '%' (or a total)")
        elif k == "flow":
            bad.append(_need(card, "nodes", list, (2, 3)))
            nodes = card.get("nodes") or []
            if not all(isinstance(n, dict) and isinstance(n.get("label"), str) and n["label"] for n in nodes):
                bad.append("each flow node needs a label")
            if len(card.get("arrows") or []) != max(len(nodes) - 1, 0):
                bad.append("a flow needs one arrow label between each pair of boxes")
        elif k == "compare":
            cols = card.get("cols")
            if not (isinstance(cols, list) and len(cols) == 2 and all(isinstance(c, dict) and c.get("head") and isinstance(c.get("lines"), list)
                                                                          and 1 <= len(c["lines"]) <= 3 for c in cols)):
                bad.append("compare needs 2 columns, each with a head and 1 to 3 lines")
        elif k == "list":
            bad.append(_need(card, "rows", list, (2, 5)))
        elif k == "quote":
            bad.append(_need(card, "text"))
        elif k == "source":
            bad += [_need(card, "header"), _need(card, "main")]
        bad = [b for b in bad if b]
        if bad:
            return bad
        texts = [t for t in _strings(card)]
        if any(len(t) > 110 for t in texts):
            return ["a line of text on the card is too long (keep each under 110 characters)"]
        spoken = numbers_in(narration)
        extra = sorted(n for t in texts for n in numbers_in(t, words=False) if n not in spoken and not any(abs(n - s) < 1e-9 for s in spoken))
        if extra:
            return [f"the card shows number(s) the narration of this shot does not say: {', '.join(f'{n:g}' for n in extra[:4])}"]
        return []
    except Exception as e:  # a malformed card must never stop the run: it just doesn't get used
        return [f"malformed card ({type(e).__name__}: {e})"]
