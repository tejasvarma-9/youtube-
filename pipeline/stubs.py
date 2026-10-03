"""Canned outputs so every stage can run with no API keys and no Claude CLI (--stub).

Stub facts and sources are placeholders for testing the plumbing. They are not research.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

STUB_SCRIPT = """\
A vending machine in a busy hospital hallway can quietly earn more per square foot than the gift shop next to it. That sounds wrong, because a vending machine is just a metal box full of snacks. By the end of this, you'll know exactly who pays whom every time you press B4.

Here is the reframe. A vending machine business isn't really a snack business. It's a real estate business that rents tiny pieces of floor from people who don't know what that floor is worth.

Start with the cost of getting in. A used snack machine runs roughly $1,500 to $3,000, and a new combo machine with a card reader can cost $5,000 or more. Then there's the stock, the card reader fees, and the van.

Meet Bob. Bob owns twelve machines across three office parks. Every machine he places needs a host, and every host wants a cut, usually a commission of roughly 10% to 20% of sales.

So the one-line model is simple. Sales per machine, times the number of machines, minus product cost, commissions, and Bob's time on the road.

Now the hidden costs. Card readers charge a fee on every swipe. Machines break, and a broken machine earns nothing while Bob waits for a part. Chocolate melts in summer, and nobody warned Bob about that.

Here's the customer Bob secretly loves most. It's the night-shift worker with no cafeteria open, who buys the same drink every single night.

Now the risk. Bob's worst year came when his biggest host closed its office and three machines went dark overnight. The rule is simple: never let one building carry half your income.

Step back for a second. The whole industry sells convenience, yet its profits depend on the most inconvenient job in it, driving a van to refill machines.

So, who pays whom? You pay Bob. Bob pays the building. And the building, it turns out, was the one holding the valuable thing all along: the hallway you walk past every day.
"""

STUB_FACTS = """\
F1 | A used snack machine runs roughly $1,500 to $3,000 | ESTIMATE
F2 | A new combo machine with a card reader can cost $5,000 or more | S1
F3 | Host commissions are usually roughly 10% to 20% of sales | ESTIMATE"""

STUB_SOURCES = "S1 | Example Publisher, placeholder source for stub runs | https://example.com/vending-costs"


def llm_answer(kind: str, ctx: dict) -> str:
    if kind == "script":
        return f"===SCRIPT===\n{STUB_SCRIPT}\n===FACTS===\n{STUB_FACTS}\n===SOURCES===\n{STUB_SOURCES}\n"
    if kind == "factcheck":
        facts = []
        for line in ctx.get("facts", "").splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                verdict = "ESTIMATE_OK" if parts[2].upper() == "ESTIMATE" else "SUPPORTED"
                facts.append({"id": parts[0], "verdict": verdict, "note": "stub", "fix": "", "source_url": ""})
        return "===JSON===\n" + json.dumps({"facts": facts, "policy": []}) + "\n===END==="
    if kind == "shots":
        shots = []
        for shot in ctx.get("shots", []):
            first = shot["text"].split(".")[0][:90]
            shots.append({"id": shot["id"], "scene": f"A stick figure in an office hallway next to a vending machine, with a big sign reading \"{first.upper()}\"."})
        return "===JSON===\n" + json.dumps({"character_description": "Bob: a round-headed stick figure with a green cap and a clipboard.", "shots": shots}) + "\n===END==="
    if kind == "metadata":
        n = ctx.get("sentence_count", 10)
        step = max(1, n // 5)
        chapters = [{"sentence": 1, "title": "The hallway paradox"}]
        for k, title in enumerate(["Getting in", "Meet Bob", "Hidden costs", "Who pays whom"], start=1):
            if 1 + k * step <= n:
                chapters.append({"sentence": 1 + k * step, "title": title})
        return "===JSON===\n" + json.dumps({
            "title_options": ["Who Really Pays for Vending Machines?", "How Vending Machines Make Money", "The Business Behind Vending Machines"],
            "title": "Who Really Pays for Vending Machines?",
            "thumbnail_text": "THE HALLWAY IS RENT",
            "thumbnail_subject": "A large flat-illustrated vending machine in a hallway, coins and dollar bills floating around it, small arrows labelled with costs, light background.",
            "description_intro": "A vending machine looks like a snack business. It is really a tiny real estate deal. Here is who gets paid every time you buy a soda.",
            "tags": ["how vending machines make money", "vending machine business", "passive income myth", "business breakdown"],
            "chapters": chapters,
        }) + "\n===END==="
    raise ValueError(f"no stub for {kind}")


def image_card(scene: str, out_png: Path, label: str = "") -> None:
    """A placeholder frame that shows the scene text, at 16:9."""
    from PIL import Image, ImageDraw

    w, h = 1376, 768
    img = Image.new("RGB", (w, h), (238, 243, 248))
    d = ImageDraw.Draw(img)
    d.rectangle([24, 24, w - 24, h - 24], outline=(20, 20, 20), width=6)
    font = _font(30)
    small = _font(24)
    d.text((60, 50), f"STUB IMAGE {label}".strip(), fill=(200, 40, 40), font=small)
    y = 120
    for line in textwrap.wrap(scene, 70)[:14]:
        d.text((60, y), line, fill=(20, 20, 20), font=font)
        y += 42
    img.save(out_png)


def _font(size: int):
    from PIL import ImageFont

    for name in ("Arial Bold.ttf", "Arial.ttf", "DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()
