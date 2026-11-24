"""Stage 4: group sentences into shots and write one image description per shot.

Each shot covers one or more whole sentences and stays on screen for at least
MIN_SHOT_SECONDS, which sets the image count and so the image cost. In the first
FAST_START_SECONDS the minimum drops to FAST_START_SHOT_SECONDS, so the opening
gets a new picture about every sentence.
Writes shots.json.
"""

from __future__ import annotations

from . import config, llm
from .common import StageError, Video, extract_json, fill, log, prompt, voice_profile


def group(sentences: list[dict], duration: float, min_s: float = None,
          fast_s: float = 0, fast_until: float = 0) -> list[dict]:
    min_s = min_s or config.MIN_SHOT_SECONDS
    shots, cur = [], []
    for s in sentences:
        if cur:
            length = s["start"] - cur[0]["start"]
            new_para = s["paragraph"] != cur[-1]["paragraph"]
            limit = fast_s if fast_s and cur[0]["start"] < fast_until else min_s
            if length >= limit or (new_para and length >= limit * 0.6):
                shots.append(cur)
                cur = []
        cur.append(s)
    if cur:
        # A short tail joins the previous shot instead of flashing by.
        if shots and duration - cur[0]["start"] < min_s * 0.5:
            shots[-1].extend(cur)
        else:
            shots.append(cur)
    out = []
    for n, g in enumerate(shots):
        start = 0.0 if n == 0 else g[0]["start"]
        end = shots[n + 1][0]["start"] if n + 1 < len(shots) else duration
        out.append({"id": n + 1, "start": round(start, 3), "end": round(end, 3),
                    "sentences": [s["i"] for s in g], "text": " ".join(s["text"] for s in g)})
    return out


def _visual_rules() -> str:
    voice = voice_profile()
    start = voice.find("## Visual style")
    end = voice.find("\n## ", start + 5)
    return voice[start:end].strip() if start >= 0 else config.IMAGE_STYLE_SUFFIX


CARD_MIN_GAP_S = 30.0   # no card this soon after another one or after the start
CARD_EVERY_S = 40.0     # and at most one per this long on average


def _attach_cards(video, shots: list[dict], result: dict) -> None:
    """Keep the cards the model proposed only if they pass validation. A bad card just stays a normal picture."""
    from . import cards
    proposed = {int(s["id"]): s.get("card") for s in result.get("shots", []) if s.get("card")}
    last_end, kept = -1e9, 0
    total = shots[-1]["end"] if shots else 0
    for s in shots:
        card = proposed.get(s["id"])
        if not card:
            continue
        why = cards.validate(card, s["text"])
        if not why and s["start"] < CARD_MIN_GAP_S:
            why = ["too early in the video"]
        if not why and s["start"] - last_end < CARD_MIN_GAP_S:
            why = ["too close to the previous card"]
        if not why and kept >= max(1, int(total // CARD_EVERY_S)):
            why = ["too many cards for this length"]
        if why:
            log(f"    card for shot {s['id']} dropped: {why[0]}")
            continue
        s["card"] = card
        s["scene"] = f"CARD (drawn in code): {card['kind']} - {s['scene']}"
        last_end, kept = s["end"], kept + 1
    if kept:
        log(f"  cards: {kept} animated cards")


def _sections(raw, shots: list[dict]) -> list[dict]:
    try:
        ids = {s["id"] for s in shots}
        secs = [{"title": str(x["title"]).strip().upper()[:24], "start_shot": int(x["start_shot"])} for x in (raw or [])]
        secs = [x for x in secs if x["title"] and x["start_shot"] in ids]
        secs.sort(key=lambda x: x["start_shot"])
        if not secs or len(secs) < 3 or len(secs) > 6:
            return []
        secs[0]["start_shot"] = shots[0]["id"]
        return secs
    except Exception:
        return []


def run(video: Video, stub: bool = False) -> None:
    tl = video.read_json("timeline.json")
    shots = group(tl["sentences"], tl["duration"],
                  fast_s=config.FAST_START_SHOT_SECONDS, fast_until=config.FAST_START_SECONDS)
    character = video.brief().get("character") or "none"
    listing = "\n\n".join(f"Shot {s['id']}: {s['text']}" for s in shots)
    text = fill(prompt("shots.md"), VISUAL_RULES=_visual_rules(), CHARACTER=character, SHOTS=listing)
    answer = llm.ask(text, "shots", stub=stub, context={"shots": shots})
    video.path("raw", "shots_answer.md").write_text(answer)
    result = extract_json(answer)

    scenes = {int(s["id"]): s["scene"].strip() for s in result.get("shots", []) if s.get("scene")}
    missing = [s["id"] for s in shots if s["id"] not in scenes]
    if missing:
        raise StageError(f"The shot list is missing scenes for shots {missing[:10]}. See raw/shots_answer.md and re-run.")
    for s in shots:
        s["scene"] = scenes[s["id"]]
        s["prompt"] = f"{s['scene']}\n\n{config.IMAGE_STYLE_SUFFIX}"
    _attach_cards(video, shots, result)

    lengths = [s["end"] - s["start"] for s in shots]
    out = {"character_description": result.get("character_description", ""), "shots": shots}
    sections = _sections(result.get("sections"), shots)
    if sections:
        out["sections"] = sections
    video.write_json("shots.json", out)
    if any(s.get("card") for s in shots) or sections:
        style = video.dir / "style.json"
        if not style.exists():
            import json as _json
            style.write_text(_json.dumps({"interactive": True, "captions": "word"}))
    est = len(shots) * config.PRICE_PER_IMAGE_BATCH
    log(f"  shots: {len(shots)} images, {min(lengths):.1f}s to {max(lengths):.1f}s each, about ${est:.2f} in batch images")
