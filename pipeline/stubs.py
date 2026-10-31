"""Canned outputs so every stage can run with no API keys and no Claude CLI (--stub).

Stub facts and sources are placeholders for testing the plumbing. They are not research.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

STUB_SCRIPT = """\
You have pressed a button on a vending machine without a second thought. That machine, sitting in a busy hospital hallway, can quietly earn more per square foot than the gift shop next to it. So who actually gets paid every time you press B4? Welcome to Who Pays Who, where we follow the money behind everyday things.

Here is the reframe. A vending machine business isn't really a snack business. It's a real estate business that rents tiny pieces of floor from people who don't know what that floor is worth.

Start with the cost of getting in. A used snack machine runs roughly $1,500 to $3,000, and a new combo machine with a card reader can cost $5,000 or more. Then there's the stock, the card reader fees, and the van.

Meet Bob. Bob owns twelve machines across three office parks. Every machine he places needs a host, and every host wants a cut, usually a commission of roughly 10% to 20% of sales.

So the one-line model is simple. Sales per machine, times the number of machines, minus product cost, commissions, and Bob's time on the road.

Now the hidden costs. Card readers charge a fee on every swipe. Machines break, and a broken machine earns nothing while Bob waits for a part. Chocolate melts in summer, and nobody warned Bob about that.

Here's the customer Bob secretly loves most. It's the night-shift worker with no cafeteria open, who buys the same drink every single night.

Now the risk. Bob's worst year came when his biggest host closed its office and three machines went dark overnight. The rule is simple: never let one building carry half your income.

Step back for a second. The whole industry sells convenience, yet its profits depend on the most inconvenient job in it, driving a van to refill machines.

So, who pays whom? You pay Bob. Bob pays the building. And the building, it turns out, was the one holding the valuable thing all along: the hallway you walk past every day.

If this was useful, like the video, subscribe to Who Pays Who, and tell me in the comments which business I should break down next.
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
    if kind == "audit":
        return "===JSON===\n" + json.dumps({"checks": []}) + "\n===END==="
    if kind == "revise":
        return (f"===SCRIPT===\n{ctx['script.txt']}\n===FACTS===\n{ctx['facts.txt']}\n===SOURCES===\n"
                f"{ctx['sources.txt']}\n===CHANGES===\nstub | nothing changed\n")
    if kind == "shots":
        shots = []
        for shot in ctx.get("shots", []):
            first = shot["text"].split(".")[0][:90]
            shots.append({"id": shot["id"], "scene": f"A stick figure in an office hallway next to a vending machine, with a big sign reading \"{first.upper()}\"."})
        return "===JSON===\n" + json.dumps({"character_description": "Bob: a round-headed stick figure with a green cap and a clipboard.", "shots": shots}) + "\n===END==="
    if kind == "shorts":
        import re as _re
        bad = _re.compile(r"^\s*(and|but|so|then|now|that|this|it|they|which|these|those|also)\b", _re.I)
        blocked = set(ctx.get("blocked", []))
        free = [s for s in ctx.get("sentences", []) if s["i"] not in blocked]
        shorts, k = [], 0
        for n in range(ctx.get("count", 3)):
            while k < len(free) and bad.match(free[k]["text"]):
                k += 1
            avoid = [sorted(a) for a in ctx.get("avoid_sets", [])]
            while True:
                start, ids, secs = k, [], 0.0
                while k < len(free) and secs < 4.0:
                    ids.append(free[k]["i"])
                    secs += free[k]["seconds"]
                    k += 1
                if sorted(ids) not in avoid or k >= len(free):
                    break
                k = start + 1
                while k < len(free) and bad.match(free[k]["text"]):
                    k += 1
            shorts.append({"opening_question": f"Stub question number {n + 1}?", "hook_text": f"Stub hook {n + 1}", "title": f"Stub short {n + 1}", "sentences": ids, "description_intro": "A stub clip from the video.",
                           "tags": ["stub", "test", "shorts"], "why": "stub"})
        return "===JSON===\n" + json.dumps({"shorts": shorts}) + "\n===END==="
    if kind == "shorts_question":
        return "===JSON===\n" + json.dumps({"questions": [{"short": n, "opening_question": "Who pays in the end?"}
                                                         for n in ctx.get("shorts", [])]}) + "\n===END==="
    if kind == "shorts_review":
        return "===JSON===\n" + json.dumps({"reviews": [{"short": n, "verdict": "PASS", "problems": []}
                                                       for n in ctx.get("shorts", [])]}) + "\n===END==="
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
            "thumbnail_before": "COSTS", "thumbnail_after": "PROFIT",
            "thumbnail_subject": "A large flat-illustrated vending machine in a hallway, coins and dollar bills floating around it, small arrows labelled with costs, navy background.",
            "description_intro": "A vending machine looks like a snack business. It is really a tiny real estate deal. Here is who gets paid every time you buy a soda.",
            "tags": ["how vending machines make money", "vending machine business", "passive income myth", "business breakdown"],
            "chapters": chapters,
        }) + "\n===END==="
    if kind == "topic_scout":
        topics = []
        for name in ("How vending machines make money", "Who really pays for airport lounges?", "How parking garages make money"):
            topics.append({"topic": name, "angle": f"Stub angle for {name.lower()}.", "demand": "stub: not measured",
                           "competition": [{"title": "Stub video", "channel": "Stub channel", "views": "0", "how_we_differ": "stub"}],
                           "sources": [{"title": f"Stub primary source {k}", "url": f"https://example.com/stub-{k}", "kind": "10-K",
                                        "supports": "stub"} for k in (1, 2, 3)],
                           "news": [], "why_now": "stub"})
        return "===JSON===\n" + json.dumps({"topics": topics}) + "\n===END==="
    if kind == "trends":
        return "===JSON===\n" + json.dumps({
            "patterns": [{"pattern": "Stub pattern", "applies_to": "titles", "evidence": ["Av10", "Bv10"],
                          "counter_evidence": "Av11", "confidence": "low"}],
            "notes_for_writers": "Stub note: nothing real was measured.",
            "proposals": [{"title": "Stub tag rule", "file": "prompts/shorts.md", "before": "- tags: 3 to 6 search phrases.",
                           "after": "- tags: 3 to 6 search phrases people really type.", "why": "stub", "evidence": ["Av30"],
                           "keeps_us_distinct": "stub", "template_risk": ""},
                          {"title": "Not allowed", "file": "pipeline/cli.py", "before": "x", "after": "y", "why": "stub",
                           "evidence": [], "keeps_us_distinct": "stub", "template_risk": ""}]}) + "\n===END==="
    if kind in ("crew_images",):
        return "===JSON===\n" + json.dumps({"images": []}) + "\n===END==="
    if kind == "crew_short_qc":
        return "===JSON===\n" + json.dumps({"problems": []}) + "\n===END==="
    if kind == "overseer":
        return "===JSON===\n" + json.dumps({
            "full_video": {"verdict": "GO", "reasons": ["stub run"], "fixes": []},
            "shorts": [{"short": n, "verdict": "GO", "reasons": ["stub run"], "fixes": []} for n in ctx.get("shorts", [])],
            "rechecked_claims": [], "summary": "Stub run: nothing was really checked."}) + "\n===END==="
    raise ValueError(f"no stub for {kind}")


def youtube_data() -> dict:
    """Two made-up channels with a hit and some flops each, for --stub runs of the trends agent."""
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc)

    def vid(cid, n, views, secs, days):
        return {"id": f"{cid}v{n}", "title": f"Stub video {n} from {cid}", "channel_id": cid, "channel": f"Stub channel {cid}",
                "published": (today - timedelta(days=days)).strftime("%Y-%m-%d"), "views": views, "likes": 0, "comments": 0,
                "seconds": secs, "short": secs <= 180, "thumbnail_url": "", "url": f"https://www.youtube.com/watch?v={cid}v{n}"}

    uploads = {}
    for cid in ("A", "B"):
        vids = [vid(cid, n, 20000, 600, 20 + n) for n in range(1, 7)]
        vids += [vid(cid, 10, 150000, 640, 30), vid(cid, 11, 6000, 590, 40)]
        vids += [vid(cid, 20 + n, 4000, 45, 15 + n) for n in range(5)] + [vid(cid, 30, 60000, 50, 25), vid(cid, 31, 900, 40, 26)]
        uploads[cid] = vids
    hits = [uploads["A"][6], uploads["B"][6]]
    return {"queries": ["stub"], "days": 150, "hits": hits, "uploads": uploads,
            "channels": {cid: {"title": f"Stub channel {cid}", "subscribers": 50000, "uploads": f"UU{cid}"} for cid in ("A", "B")}}


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
