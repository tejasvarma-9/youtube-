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


def assign_backgrounds(shots: list[dict], sentences: list[dict]) -> None:
    """Give each shot a background color phrase, changing color at a paragraph break.

    A color lasts at least SECTION_MIN_SECONDS, then the next shot that starts a new
    paragraph begins the next color. Colors cycle through config.SECTION_BACKGROUNDS.
    Set SECTION_MIN_SECONDS=0 to turn this off (pictures then keep the old white look).
    """
    if config.SECTION_MIN_SECONDS <= 0:
        return
    para = {s["i"]: s["paragraph"] for s in sentences}
    colors, n, section_start, last_para = config.SECTION_BACKGROUNDS, 0, 0.0, None
    for shot in shots:
        first = shot["sentences"][0]
        starts_paragraph = last_para is not None and para.get(first) != last_para
        if shot is not shots[0] and starts_paragraph and shot["start"] - section_start >= config.SECTION_MIN_SECONDS:
            n += 1
            section_start = shot["start"]
        shot["background"] = colors[n % len(colors)]
        last_para = para.get(shot["sentences"][-1])


def _visual_rules() -> str:
    voice = voice_profile()
    start = voice.find("## Visual style")
    end = voice.find("\n## ", start + 5)
    return voice[start:end].strip() if start >= 0 else config.IMAGE_STYLE_SUFFIX


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
    assign_backgrounds(shots, tl["sentences"])
    for s in shots:
        s["prompt"] = f"{s['scene']}\n\n{config.style_suffix(s.get('background', ''))}"

    lengths = [s["end"] - s["start"] for s in shots]
    video.write_json("shots.json", {"character_description": result.get("character_description", ""), "shots": shots})
    est = len(shots) * config.PRICE_PER_IMAGE_BATCH
    log(f"  shots: {len(shots)} images, {min(lengths):.1f}s to {max(lengths):.1f}s each, about ${est:.2f} in batch images")
