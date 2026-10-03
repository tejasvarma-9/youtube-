"""Stage 4: group sentences into shots and write one image description per shot.

Each shot covers one or more whole sentences and stays on screen for at least
MIN_SHOT_SECONDS, which sets the image count and so the image cost.
Writes shots.json.
"""

from __future__ import annotations

from . import config, llm
from .common import StageError, Video, extract_json, fill, log, prompt, voice_profile


def group(sentences: list[dict], duration: float, min_s: float = None) -> list[dict]:
    min_s = min_s or config.MIN_SHOT_SECONDS
    shots, cur = [], []
    for s in sentences:
        if cur:
            length = s["start"] - cur[0]["start"]
            new_para = s["paragraph"] != cur[-1]["paragraph"]
            if length >= min_s or (new_para and length >= min_s * 0.6):
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


def run(video: Video, stub: bool = False) -> None:
    tl = video.read_json("timeline.json")
    shots = group(tl["sentences"], tl["duration"])
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

    lengths = [s["end"] - s["start"] for s in shots]
    video.write_json("shots.json", {"character_description": result.get("character_description", ""), "shots": shots})
    est = len(shots) * config.PRICE_PER_IMAGE_BATCH
    log(f"  shots: {len(shots)} images, {min(lengths):.1f}s to {max(lengths):.1f}s each, about ${est:.2f} in batch images")
