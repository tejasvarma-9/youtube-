"""Shorts: 2 to 4 vertical clips (1080x1920, under a minute) cut from a finished, fact-checked video.

python -m pipeline shorts <slug>   (needs the video's script, fact-check, voiceover, shots and pictures)

A Short never has new narration. Claude only CHOOSES sentences that already exist in the video's
script; the clip is those sentences, in order, cut from the video's own voiceover (same voice, no
new voice cost), over the video's own pictures shown in a vertical frame (the picture on top of a
blurred copy of itself), with large word-by-word captions and a short logo card at the end. Because
the words are the ones the fact-check already passed, nothing needs fact-checking again. The title
and description Claude writes may only use numbers that appear in the Short's own sentences.

Each Short also carries a short hook_text shown on screen above the picture for the whole clip, and
it ends on its own last line (no "watch the full video" card: Shorts that work as trailers stop being
shown). Before anything is built, a separate Claude check (prompts/shorts_review.md), which did not
write those lines, reads each Short's hook_text, title and description against its narration and the
full script. It passes them, rewrites them, or rejects the Short; a Short that does not pass is not built.

Everything is written to out/<slug>/shorts/: plan.json, REVIEW.md and one folder per Short
(short.mp4, captions.srt, metadata.json, description.txt, qc.json).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
import re
import subprocess

from . import config, llm
from .common import StageError, Video, extract_json, fill, fmt_ts, log, media_duration, prompt, run as sh, voice_profile
from .lint import _numbers
from .s6_assemble import MOTIONS, find_ffmpeg

CLIP_VERSION = "short3"   # bump when the picture layout changes, so cached clips are rebuilt
PLAN_VERSION = 2          # plans from an older version (no hook_text, no review) are chosen again
HEAD_PAD_S = 0.10         # a little room before the first word of a sentence
TAIL_PAD_S = 0.35         # and after the last
GAP_S = 0.12              # silence between kept sentences
HOLD_S = 5.0              # the same picture is re-framed (punch in or pan) at least this often
FG_TOP = 420              # top edge of the picture inside the 1080x1920 frame
HOOK_TOP, HOOK_BOTTOM = 140, 395   # the on-screen hook text sits here, below the Shorts app's top bar
HOOK_MAX_CHARS = 42
# Captions sit above the Shorts app's title and buttons (bottom ~400 px). libass scales its font units to
# the video height (units of 1/288), so 11 is about 73 px and MarginV 110 puts the line's bottom near y=1185, just under the picture.
CAPTION_STYLE = (
    "FontName=Arial,FontSize=11,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,"
    "BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=110"
)
BAD_HOOK_START = re.compile(r"^\s*(and|but|so|then|now|that|this|it|they|which|these|those|also)\b", re.I)
ODD_CHARS = re.compile(r"[#\U0001F000-\U0001FFFF\u2600-\u27BF]")
BACK_POINTER = re.compile(r"\b(as i said|as we saw|earlier|the first place|the next (?:company|two|one)|zoom out|back to the question)\b", re.I)


# --- choosing -----------------------------------------------------------------

def blocked_sentences(sentences: list[dict]) -> set[int]:
    """The welcome line, anything naming the channel, and the last two paragraphs (pointer and sign-off)."""
    name_re = re.compile(r"\b" + re.escape(config.CHANNEL_NAME) + r"\b", re.I)
    last = max(s.get("paragraph", 0) for s in sentences)
    return {s["i"] for s in sentences if name_re.search(s["text"]) or s.get("paragraph", 0) >= last - 1}


def cut_points(s: dict) -> tuple[float, float]:
    """Where in voiceover.wav a sentence is cut: from just before its first word to just after its last."""
    words = s.get("words") or []
    a = max(0.0, (words[0][0] if words else s["start"]) - HEAD_PAD_S)
    end = s["end"]
    if words:
        end = min(end, words[-1][1] + TAIL_PAD_S)
    return a, max(end, a + 0.2)


def length_of(s: dict) -> float:
    a, b = cut_points(s)
    return (b - a) + GAP_S


def validate_plan(plan: dict, sentences: list[dict], count: int, stub: bool = False) -> tuple[list[dict], list[str]]:
    """The chosen Shorts with their lengths and texts, and everything wrong with the choice."""
    by_i = {s["i"]: s for s in sentences}
    blocked = blocked_sentences(sentences)
    min_s = 3.0 if stub else config.SHORT_MIN_S
    problems, items, used = [], [], set()
    raw = plan.get("shorts") if isinstance(plan, dict) else None
    if not isinstance(raw, list) or not raw:
        return [], ["The answer has no \"shorts\" list."]
    if len(raw) != count:
        problems.append(f"Asked for {count} Shorts but got {len(raw)}.")
    for n, sh_ in enumerate(raw, start=1):
        label = f"Short {n}"
        try:
            ids = [int(x) for x in sh_["sentences"]]
        except (KeyError, TypeError, ValueError):
            problems.append(f"{label}: \"sentences\" must be a list of sentence numbers.")
            continue
        if not ids or ids != sorted(set(ids)):
            problems.append(f"{label}: sentence numbers must be in increasing order with no repeats.")
            continue
        bad = [i for i in ids if i not in by_i]
        if bad:
            problems.append(f"{label}: no such sentence number(s) {bad}.")
            continue
        if set(ids) & blocked:
            problems.append(f"{label}: uses blocked sentence(s) {sorted(set(ids) & blocked)} (welcome line, channel name or ending).")
        if set(ids) & used:
            problems.append(f"{label}: reuses sentence(s) {sorted(set(ids) & used)} from another Short.")
        used |= set(ids)
        total = sum(length_of(by_i[i]) for i in ids)
        if not min_s <= total <= config.SHORT_MAX_S:
            problems.append(f"{label}: {total:.0f} seconds long; it must be {min_s:.0f} to {config.SHORT_MAX_S:.0f}.")
        first = by_i[ids[0]]["text"]
        if BAD_HOOK_START.search(first) or BACK_POINTER.search(first):
            problems.append(f"{label}: starts on \"{first[:60]}\", which leans on what came before. Start on a sentence that stands alone.")
        for i in ids[1:]:
            if BACK_POINTER.search(by_i[i]["text"]):
                problems.append(f"{label}: sentence {i} points back at other parts of the video (\"{by_i[i]['text'][:60]}\").")
        text = " ".join(by_i[i]["text"] for i in ids)
        title = str(sh_.get("title", "")).strip()
        intro = str(sh_.get("description_intro", "")).strip()
        hook = " ".join(str(sh_.get("hook_text", "")).split())
        problems += written_problems(label, text, hook, title, intro)
        tags = [str(t).strip() for t in sh_.get("tags", []) if str(t).strip()][:8]
        items.append({"hook_text": hook, "title": title, "sentences": ids, "description_intro": intro, "tags": tags,
                      "why": str(sh_.get("why", "")).strip(), "seconds": round(total, 1), "text": text})
    return items, problems


def written_problems(label: str, text: str, hook: str, title: str, intro: str) -> list[str]:
    """Mechanical rules for the lines written for a Short (the review step judges the rest)."""
    problems = []
    if not hook or len(hook) > HOOK_MAX_CHARS or ODD_CHARS.search(hook):
        problems.append(f"{label}: hook_text must be 1 to {HOOK_MAX_CHARS} characters with no hashtags or emojis (got {len(hook)}).")
    elif hook.rstrip(".?!").lower() in text.lower():
        problems.append(f"{label}: hook_text just repeats the narration; it should set up the question instead.")
    if not title or len(title) > 70 or ODD_CHARS.search(title):
        problems.append(f"{label}: the title must be 1 to 70 characters with no hashtags or emojis.")
    if not intro:
        problems.append(f"{label}: description_intro is empty.")
    allowed = _numbers(text)
    for where, value in (("hook_text", hook), ("title", title), ("description_intro", intro)):
        extra = sorted(_numbers(value) - allowed)
        if extra:
            problems.append(f"{label}: the {where} uses figure(s) {extra} that are not in the Short's own sentences.")
    return problems


def _review_key(item: dict) -> str:
    raw = json.dumps([item["hook_text"], item["title"], item["description_intro"], item["sentences"]])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def review(video: Video, plan: dict, stub: bool = False) -> None:
    """An independent Claude check of every Short's written lines. Fills item["review"]; FIX suggestions are
    applied (if they pass the mechanical rules) and checked again once. Shorts reviewed before keep their result
    while their lines are unchanged, so a hand edit to plan.json is checked again automatically."""
    meta = video.dir / "metadata.json"
    title = json.loads(meta.read_text()).get("title", "") if meta.exists() else video.brief().get("topic", "")
    script = video.read_text("script.txt")
    for attempt in (1, 2):
        todo = [(n, it) for n, it in enumerate(plan["shorts"], start=1)
                if it.get("review", {}).get("key") != _review_key(it)]
        if not todo:
            return
        blocks = []
        for n, it in todo:
            blocks.append(f"## Short {n}\nhook_text: {it['hook_text']}\ntitle: {it['title']}\n"
                          f"description_intro: {it['description_intro']}\nnarration ({it['seconds']:.0f} seconds, "
                          f"script sentences {it['sentences']}):\n{it['text']}\n")
        text = fill(prompt("shorts_review.md"), TITLE=title, SCRIPT=script, SHORTS="\n".join(blocks))
        answer = llm.ask(text, "shorts_review", stub=stub, context={"shorts": [n for n, _ in todo]})
        video.path("shorts", "raw", f"review_answer_{attempt}.md").write_text(answer)
        got = {int(r.get("short", 0)): r for r in extract_json(answer).get("reviews", []) if isinstance(r, dict)}
        for n, it in todo:
            r = got.get(n, {"verdict": "REJECT", "problems": ["The reviewer gave no answer for this Short."]})
            verdict = str(r.get("verdict", "")).upper()
            problems = [str(p) for p in r.get("problems", [])]
            if verdict == "FIX" and attempt == 1:
                fixed = dict(it)
                for field in ("hook_text", "title", "description_intro"):
                    if str(r.get(field, "")).strip():
                        fixed[field] = " ".join(str(r[field]).split())
                rule = written_problems(f"Short {n}", it["text"], fixed["hook_text"], fixed["title"], fixed["description_intro"])
                if not rule and _review_key(fixed) != _review_key(it):
                    before = {f: it[f] for f in ("hook_text", "title", "description_intro")}
                    it.update({f: fixed[f] for f in before})
                    it["review_history"] = it.get("review_history", []) + [{"before": before, "problems": problems}]
                    log(f"  short {n}: the reviewer rewrote its lines; checking them again")
                    continue  # no "review" key for the new lines, so attempt 2 checks them
                problems += rule
            it["review"] = {"verdict": "PASS" if verdict == "PASS" else ("REJECT" if verdict == "REJECT" else "FIX"),
                            "problems": problems, "key": _review_key(it)}
    for n, it in enumerate(plan["shorts"], start=1):  # still unreviewed after two rounds
        if it.get("review", {}).get("key") != _review_key(it):
            it["review"] = {"verdict": "FIX", "problems": ["The rewritten lines were not confirmed by a second check."],
                            "key": _review_key(it)}


def choose(video: Video, count: int, stub: bool = False) -> list[dict]:
    tl = video.read_json("timeline.json")
    sentences = tl["sentences"]
    blocked = blocked_sentences(sentences)
    meta_path = video.dir / "metadata.json"
    title = json.loads(meta_path.read_text()).get("title", "") if meta_path.exists() else video.brief().get("topic", "")
    lines = "\n".join(f"[{s['i']}] ({s['start']:.1f}s, {length_of(s):.1f}s) {s['text']}" for s in sentences)
    base = fill(prompt("shorts.md"), MIN_S=f"{config.SHORT_MIN_S:.0f}", MAX_S=f"{config.SHORT_MAX_S:.0f}", VOICE=voice_profile(),
                TITLE=title, SENTENCES=lines, BLOCKED=", ".join(str(i) for i in sorted(blocked)) or "none", COUNT=str(count))
    ctx = {"sentences": [{"i": s["i"], "text": s["text"], "seconds": length_of(s)} for s in sentences],
           "blocked": sorted(blocked), "count": count}
    extra = ""
    for attempt in (1, 2):
        answer = llm.ask(base + extra, "shorts", stub=stub, context=ctx)
        video.path("shorts", "raw", f"plan_answer_{attempt}.md").write_text(answer)
        items, problems = validate_plan(extract_json(answer), sentences, count, stub=stub)
        if not problems:
            return items
        log("  the plan had problems:\n    " + "\n    ".join(problems))
        extra = ("\n\nYour previous answer had these problems. Choose again and fix every one of them:\n- "
                 + "\n- ".join(problems) + "\n")
    raise StageError("Claude's Shorts plan still breaks the rules after a second try:\n  " + "\n  ".join(problems)
                     + f"\nSee out/{video.slug}/shorts/raw/. Run the command again to get a fresh attempt.")


# --- building -----------------------------------------------------------------

def _shot_of(sentence: dict, shots: list[dict]) -> dict:
    for shot in shots:
        if sentence["i"] in (shot.get("sentences") or []):
            return shot
    for shot in shots:
        if shot["start"] <= sentence["start"] < shot["end"]:
            return shot
    return shots[-1]


def chunk_words(text: str, max_words: int = 3, max_chars: int = 18) -> list[list[int]]:
    """Caption chunks as lists of word positions: at most 3 words and about 18 characters each."""
    chunks, cur, chars = [], [], 0
    for k, w in enumerate(text.split()):
        if cur and (len(cur) >= max_words or chars + 1 + len(w) > max_chars):
            chunks.append(cur)
            cur, chars = [], 0
        cur.append(k)
        chars += len(w) + (1 if len(cur) > 1 else 0)
    if cur:
        chunks.append(cur)
    return chunks


def write_srt(parts: list[dict], path) -> None:
    """Big word-by-word captions. parts: per sentence, its text, word times, cut start, offset and length in the Short."""
    n, lines = 0, []
    for p in parts:
        words = p["text"].split()
        times = p.get("words")
        if not (times and len(times) == len(words)):
            span = max(p["length"] - GAP_S, 0.2)
            times = [[(p["cut"] + span * k / len(words)), (p["cut"] + span * (k + 1) / len(words))] for k in range(len(words))]
        chunks = chunk_words(p["text"])
        seg_end = p["offset"] + p["length"]
        for ci, ch in enumerate(chunks):
            a = p["offset"] + max(times[ch[0]][0] - p["cut"], 0.0)
            if ci + 1 < len(chunks):
                b = p["offset"] + max(times[chunks[ci + 1][0]][0] - p["cut"], 0.0)
            else:
                b = min(p["offset"] + times[ch[-1]][1] - p["cut"] + 0.15, seg_end)
            n += 1
            lines += [str(n), f"{fmt_ts(a, srt=True)} --> {fmt_ts(max(b, a + 0.12), srt=True)}", " ".join(words[k] for k in ch), ""]
    path.write_text("\n".join(lines))


def _cover(img, w: int, h: int):
    from PIL import Image
    s = max(w / img.width, h / img.height)
    big = img.resize((math.ceil(img.width * s), math.ceil(img.height * s)), Image.LANCZOS)
    x, y = (big.width - w) // 2, (big.height - h) // 2
    return big.crop((x, y, x + w, y + h))


def hook_lines(draw, hook: str):
    """The hook text at the biggest size that fits the band in at most two lines."""
    from .s8_thumbnail import _font, _wrap
    max_w = config.SHORT_WIDTH - 90
    for size in range(96, 50, -4):
        font = _font(size)
        lines = _wrap(draw, hook, font, max_w)
        height = len(lines) * size * 1.18
        if len(lines) <= 2 and all(draw.textlength(ln, font=font) <= max_w for ln in lines) and height <= HOOK_BOTTOM - HOOK_TOP:
            return font, size, lines
    return font, size, _wrap(draw, hook, font, max_w)[:2]


def _backdrop(src, hook: str = ""):
    """The picture, blurred and darkened to fill the frame, with the hook text at the top (white, last line yellow)."""
    from PIL import ImageDraw, ImageEnhance, ImageFilter
    from .s8_thumbnail import WHITE, YELLOW

    W, H = config.SHORT_WIDTH, config.SHORT_HEIGHT
    bg = ImageEnhance.Brightness(_cover(src, W, H).filter(ImageFilter.GaussianBlur(42))).enhance(0.5)
    if hook:
        d = ImageDraw.Draw(bg)
        font, size, lines = hook_lines(d, hook)
        step = round(size * 1.18)
        y = HOOK_TOP + ((HOOK_BOTTOM - HOOK_TOP) - step * len(lines)) // 2
        for k, line in enumerate(lines):
            box = d.textbbox((0, 0), line, font=font)
            fill = YELLOW if (k == len(lines) - 1 and len(lines) > 1) else WHITE
            d.text(((W - (box[2] - box[0])) // 2, y - box[1] + (step - (box[3] - box[1])) // 2), line, font=font,
                   fill=fill, stroke_width=max(5, size // 12), stroke_fill=(10, 10, 10))
            y += step
    return bg


def build_clip(img, out, frames: int, motion: int, hook: str = "") -> None:
    """One picture as a vertical clip: the picture across the middle with a slow push-in or pan."""
    from PIL import Image

    W, H, FPS = config.SHORT_WIDTH, config.SHORT_HEIGHT, config.FPS
    src = Image.open(img).convert("RGB")
    sw, sh_ = src.size
    fh = round(sh_ * W / sw)
    bg = _backdrop(src, hook)
    move = MOTIONS[motion % len(MOTIONS)]
    cmd = [find_ffmpeg()[0], "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for k in range(max(frames, 1)):
            zoom, px, py = move(k / max(frames - 1, 1))
            cw, ch = sw / zoom, sh_ / zoom
            x0, y0 = (sw - cw) * px, (sh_ - ch) * py
            frame = bg.copy()
            frame.paste(src.resize((W, fh), Image.BICUBIC, box=(x0, y0, x0 + cw, y0 + ch)), (0, FG_TOP))
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait() != 0:
            raise StageError(f"ffmpeg failed building a Short clip:\n{err[-1500:]}")
    except BrokenPipeError:
        raise StageError(f"ffmpeg stopped while building a Short clip:\n{proc.stderr.read().decode(errors='replace')[-1500:]}")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stderr.close()


def end_card_image(path, title: str) -> None:
    """The logo on its own background colour, 'Full breakdown on the channel', and the full video's title."""
    from PIL import Image, ImageDraw
    from .s8_thumbnail import _font, _wrap

    W, H = config.SHORT_WIDTH, config.SHORT_HEIGHT
    raw = Image.open(config.LOGO).convert("RGBA")
    logo = Image.new("RGB", raw.size, (255, 255, 255))
    logo.paste(raw, mask=raw.split()[3])
    bg = logo.getpixel((2, 2))
    light = (0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]) > 140
    ink = (16, 27, 45) if light else (255, 255, 255)
    card = Image.new("RGB", (W, H), bg)
    size = 560
    card.paste(logo.resize((size, size), Image.LANCZOS), ((W - size) // 2, 330))
    d = ImageDraw.Draw(card)
    y = 330 + size + 40
    name_font = _font(76)
    d.text(((W - d.textlength(config.CHANNEL_NAME, font=name_font)) / 2, y), config.CHANNEL_NAME, font=name_font, fill=ink)
    y += 130
    label_font = _font(44)
    label = "Full breakdown on the channel"
    d.text(((W - d.textlength(label, font=label_font)) / 2, y), label, font=label_font, fill=ink)
    y += 80
    t_font = _font(46)
    for line in _wrap(d, title, t_font, W - 160)[:3]:
        d.text(((W - d.textlength(line, font=t_font)) / 2, y), line, font=t_font, fill=ink)
        y += 62
    card.save(path)


def build_end_card(out, title: str) -> None:
    png = out.with_suffix(".png")
    end_card_image(png, title)
    sh([find_ffmpeg()[0], "-y", "-v", "error", "-loop", "1", "-framerate", str(config.FPS), "-i", str(png),
        "-frames:v", str(max(1, round(config.SHORT_END_CARD_S * config.FPS))), "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "18", "-pix_fmt", "yuv420p", str(out)])


def _audio(video: Video, segs: list[dict], out, loud: bool) -> None:
    ffmpeg = find_ffmpeg()[0]
    parts = []
    for k, s in enumerate(segs):
        a, b = s["cut"], s["cut"] + (s["length"] - GAP_S)
        parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.01,"
                     f"afade=t=out:st={max(b - a - 0.03, 0):.3f}:d=0.03,apad=pad_dur={GAP_S}[a{k}]")
    tail = ",loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000" if loud else ",aresample=48000"
    graph = ";".join(parts) + ";" + "".join(f"[a{k}]" for k in range(len(segs))) + f"concat=n={len(segs)}:v=0:a=1{tail}[out]"
    sh([ffmpeg, "-y", "-v", "error", "-i", str(video.dir / "voiceover.wav"), "-filter_complex", graph, "-map", "[out]", str(out)])


def build_short(video: Video, n: int, item: dict, stub: bool = False) -> dict:
    ffmpeg, can_burn = find_ffmpeg()
    if not ffmpeg:
        raise StageError("No usable ffmpeg found. On your Mac: brew install ffmpeg")
    if config.SHORT_END_CARD_S > 0 and not config.LOGO.exists():
        raise StageError(f"The channel logo is missing ({config.LOGO}). The end card needs it (or set SHORT_END_CARD_S to 0).")
    tl = video.read_json("timeline.json")
    shots = video.read_json("shots.json")["shots"]
    by_i = {s["i"]: s for s in tl["sentences"]}
    d = video.path("shorts", f"short_{n}", "x").parent
    segs, offset = [], 0.0
    for i in item["sentences"]:
        s = by_i[i]
        a, _ = cut_points(s)
        L = length_of(s)
        segs.append({"i": i, "text": s["text"], "words": s.get("words"), "cut": a, "length": L, "offset": offset,
                     "shot": _shot_of(s, shots)})
        offset += L

    voice = d / "voice.wav"
    _audio(video, segs, voice, loud=not (stub or tl.get("provider") == "stub"))
    total = media_duration(voice)
    scale = total / offset if offset else 1.0
    for s in segs:  # the cut audio can differ from the plan by a few milliseconds
        s["offset"] *= scale
        s["length"] *= scale
    write_srt(segs, d / "captions.srt")

    # One picture per run of sentences that share a shot, re-framed at least every HOLD_S seconds.
    groups = []
    for s in segs:
        if groups and groups[-1]["shot"]["id"] == s["shot"]["id"]:
            groups[-1]["end"] = s["offset"] + s["length"]
        else:
            groups.append({"shot": s["shot"], "start": s["offset"], "end": s["offset"] + s["length"]})
    jobs, motion = [], 0
    for g in groups:
        img = video.dir / "images" / f"shot_{g['shot']['id']:03d}.png"
        if not img.exists():
            raise StageError(f"Image for shot {g['shot']['id']} is missing. Run: python -m pipeline images {video.slug}")
        parts = max(1, math.ceil((g["end"] - g["start"]) / HOLD_S))
        for k in range(parts):
            t0 = g["start"] + (g["end"] - g["start"]) * k / parts
            t1 = g["start"] + (g["end"] - g["start"]) * (k + 1) / parts
            f0, f1 = round(t0 * config.FPS), round(t1 * config.FPS)
            jobs.append((img, max(f1 - f0, 1), motion))
            motion += 1
    clips_dir = d / "clips"
    clips_dir.mkdir(exist_ok=True)
    todo, clips = [], []
    for idx, (img, frames, mot) in enumerate(jobs):
        st = img.stat()
        key = hashlib.sha1(f"{CLIP_VERSION}|{st.st_size}|{st.st_mtime_ns}|{frames}|{mot % len(MOTIONS)}|{item['hook_text']}".encode()).hexdigest()[:10]
        clip = clips_dir / f"{idx:02d}_{key}.mp4"
        clips.append(clip)
        if not clip.exists():
            todo.append((img, clip, frames, mot, item["hook_text"]))
    if config.SHORT_END_CARD_S > 0:  # off by default: Shorts that send viewers elsewhere stop being shown
        ttl = video.read_json("metadata.json").get("title", "") if (video.dir / "metadata.json").exists() else video.brief().get("topic", "")
        card = clips_dir / f"end_{hashlib.sha1((CLIP_VERSION + ttl + str(config.LOGO.stat().st_mtime_ns)).encode()).hexdigest()[:10]}.mp4"
        if not card.exists():
            build_end_card(card, ttl)
        clips.append(card)
    for old in clips_dir.glob("*.mp4"):
        if old not in clips:
            old.unlink()
    for old in clips_dir.glob("*.png"):
        old.unlink()
    log(f"  short {n}: {len(segs)} sentences, {total:.0f}s of voice; building {len(todo)} clips")
    workers = max(1, min(4, (os.cpu_count() or 2) - 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in [pool.submit(build_clip, *job) for job in todo]:
            fut.result()
    (clips_dir / "list.txt").write_text("".join(f"file '{c.name}'\n" for c in clips))

    burn = can_burn
    if not burn:
        log("  WARNING: your ffmpeg can't burn in captions, so this Short has none. Shorts need captions: "
            "brew install ffmpeg-full, then run the shorts command again.")
    cmd = [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "clips/list.txt", "-i", "voice.wav"]
    if burn:
        cmd += ["-vf", f"subtitles=captions.srt:force_style='{CAPTION_STYLE}'"]
    cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-af", "apad", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", "-movflags", "+faststart", "short.mp4"]
    sh(cmd, cwd=d)
    info = {"burned_captions": burn, "end_card_seconds": config.SHORT_END_CARD_S, "voice_seconds": round(total, 2),
            "shots": sorted({s["shot"]["id"] for s in segs})}
    (d / "assembly.json").write_text(json.dumps(info, indent=2) + "\n")
    return info


def description(item: dict, parent_title: str, sources_md: str) -> str:
    parts = [item["description_intro"].strip(), "", f"Full video: {parent_title}", "",
             "Sources (from the full video):", sources_md.strip(), "", config.DISCLAIMER, "", "#shorts"]
    return "\n".join(parts).strip() + "\n"


# --- checking -----------------------------------------------------------------

def qc_short(video: Video, n: int, stub: bool = False) -> dict:
    from .s10_qc import LOUDNESS_FAIL, LOUDNESS_TARGET, PEAK_MAX_DBTP, SILENCE_MAX_S, _ff

    d = video.dir / "shorts" / f"short_{n}"
    mp4 = str(d / "short.mp4")
    failures, warnings, checks = [], [], {}
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height:format=duration", "-of", "json", mp4], capture_output=True, text=True)
    try:
        info = json.loads(probe.stdout)
        w, h = info["streams"][0]["width"], info["streams"][0]["height"]
        dur = float(info["format"]["duration"])
    except (ValueError, KeyError, IndexError):
        return {"passed": False, "failures": ["Couldn't read short.mp4 (ffprobe failed). Run the shorts command again."], "warnings": [], "checks": {}}
    checks.update(resolution=f"{w}x{h}", duration=round(dur, 1))
    if (w, h) != (config.SHORT_WIDTH, config.SHORT_HEIGHT):
        failures.append(f"The Short is {w}x{h}, not {config.SHORT_WIDTH}x{config.SHORT_HEIGHT}.")
    if dur > 60.5 + config.SHORT_END_CARD_S:
        failures.append(f"The Short is {dur:.0f} seconds; keep it under about a minute.")
    asm = json.loads((d / "assembly.json").read_text()) if (d / "assembly.json").exists() else {}
    if not asm.get("burned_captions"):
        failures.append("No captions are burned in. Most Shorts are watched on mute: install ffmpeg-full and run the shorts command again.")
    if not stub:
        out = _ff(["-i", mp4, "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"])
        m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out, re.S)
        if m:
            ln = json.loads(m.group(0))
            lufs, peak = float(ln["input_i"]), float(ln["input_tp"])
            checks.update(loudness_lufs=lufs, true_peak_dbtp=peak)
            if not LOUDNESS_FAIL[0] <= lufs <= LOUDNESS_FAIL[1]:
                failures.append(f"Loudness is {lufs:.1f} LUFS; it will sound far too {'quiet' if lufs < -14 else 'loud'}.")
            elif not LOUDNESS_TARGET[0] <= lufs <= LOUDNESS_TARGET[1]:
                warnings.append(f"Loudness is {lufs:.1f} LUFS; about -14 is ideal.")
            if peak > PEAK_MAX_DBTP:
                warnings.append(f"Audio peaks at {peak:.1f} dBTP and may clip.")
        out = _ff(["-i", mp4, "-vn", "-af", f"silencedetect=n=-45dB:d={SILENCE_MAX_S}", "-f", "null", "-"])
        for start, length in re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", out):
            s = float(start) - float(length)
            if s < dur - config.SHORT_END_CARD_S - 1.0:
                failures.append(f"A long silence at {fmt_ts(s)} ({float(length):.1f}s).")
    out = _ff(["-i", mp4, "-an", "-vf", "blackdetect=d=0.4:pix_th=0.10", "-f", "null", "-"])
    blacks = [fmt_ts(float(s)) for s in re.findall(r"black_start:([\d.]+)", out)]
    if blacks:
        failures.append("Black frames at " + ", ".join(blacks[:5]) + ".")
    # Pictures were checked in the full video's quality check; warn when a Short uses one it flagged.
    qc = video.dir / "qc.json"
    if qc.exists():
        flagged = {int(x) for t in json.loads(qc.read_text()).get("warnings", []) + json.loads(qc.read_text()).get("failures", [])
                   for x in re.findall(r"\bShot (\d+)\b", t)}
        used = set(asm.get("shots", []))
        for sid in sorted(flagged & used):
            warnings.append(f"Uses shot {sid}, which the full video's quality check flagged (see qc.md). Look at it before publishing.")
    result = {"passed": not failures, "failures": failures, "warnings": warnings, "checks": checks}
    (d / "qc.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


# --- the command ----------------------------------------------------------------

def _plan_current(video: Video, plan_path) -> dict | None:
    if not plan_path.exists():
        return None
    plan = json.loads(plan_path.read_text())
    sha = hashlib.sha1(video.read_text("script.txt").encode()).hexdigest()[:12]
    fresh = (plan.get("version") == PLAN_VERSION and plan.get("script_sha") == sha
             and plan.get("voice_seconds") == round(video.read_json("timeline.json")["duration"], 1))
    return plan if fresh else None


def run(video: Video, count: int = config.SHORTS_COUNT, only: int | None = None, plan_only: bool = False,
        replan: bool = False, stub: bool = False, force: bool = False) -> None:
    if not 2 <= count <= 4:
        raise StageError("Choose 2, 3 or 4 Shorts per video (--count).")
    fc = video.dir / "factcheck.json"
    if not force and not (fc.exists() and json.loads(fc.read_text()).get("passed")):
        raise StageError("The video's fact-check hasn't passed, and every Short is cut from its script. Fix that first (or --force).")
    for need in ("timeline.json", "shots.json", "voiceover.wav"):
        if not (video.dir / need).exists():
            raise StageError(f"{need} is missing. Run the full video pipeline first: python -m pipeline run {video.slug}")
    tl = video.read_json("timeline.json")
    plan_path = video.path("shorts", "plan.json")
    plan = None if replan else _plan_current(video, plan_path)
    if plan:
        log(f"  using the Shorts plan in out/{video.slug}/shorts/plan.json (add --replan to choose again)")
    else:
        items = choose(video, count, stub=stub)
        plan = {"version": PLAN_VERSION, "script_sha": hashlib.sha1(video.read_text("script.txt").encode()).hexdigest()[:12],
                "voice_seconds": round(tl["duration"], 1), "shorts": items}
        log(f"  planned {len(items)} Shorts:")
        for n, it in enumerate(items, start=1):
            log(f"    {n}. {it['title']}  ({it['seconds']:.0f}s)  on screen: \"{it['hook_text']}\"")
    # Re-check the written lines whenever they changed (including hand edits to plan.json).
    by_i = {s["i"]: s for s in tl["sentences"]}
    for n, it in enumerate(plan["shorts"], start=1):
        rule = written_problems(f"Short {n}", " ".join(by_i[i]["text"] for i in it["sentences"]), it.get("hook_text", ""),
                                it.get("title", ""), it.get("description_intro", ""))
        if rule:
            raise StageError("plan.json breaks the rules:\n  " + "\n  ".join(rule))
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    log("  checking each Short's on-screen text, title and description (separate Claude check)...")
    review(video, plan, stub=stub)
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    for n, it in enumerate(plan["shorts"], start=1):
        r = it["review"]
        log(f"    short {n}: {r['verdict']}" + ("" if r["verdict"] == "PASS" else "  " + "; ".join(r["problems"])))
    if plan_only:
        write_review(video, plan, json.loads((video.dir / "metadata.json").read_text()).get("title", "")
                     if (video.dir / "metadata.json").exists() else "")
        return
    parent_title = json.loads((video.dir / "metadata.json").read_text()).get("title", "") if (video.dir / "metadata.json").exists() else video.brief().get("topic", "")
    sources_md = (video.dir / "sources.md").read_text() if (video.dir / "sources.md").exists() else ""
    results = {}
    for n, item in enumerate(plan["shorts"], start=1):
        if only and n != only:
            continue
        if item["review"]["verdict"] != "PASS":
            old = video.dir / "shorts" / f"short_{n}" / "short.mp4"
            if old.exists():  # never leave an earlier build of an unapproved Short lying around to be uploaded
                old.unlink()
            log(f"  short {n}: NOT built, the check did not pass it (see shorts/REVIEW.md). Edit its lines in plan.json and "
                f"run again, or --replan.")
            continue
        build_short(video, n, item, stub=stub)
        d = video.dir / "shorts" / f"short_{n}"
        meta = {"title": item["title"], "hook_text": item["hook_text"], "tags": item["tags"], "sentences": item["sentences"], "why": item["why"],
                "parent_title": parent_title}
        (d / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        (d / "description.txt").write_text(description(item, parent_title, sources_md))
        results[n] = qc_short(video, n, stub=stub)
        log(f"  short {n} quality check: {'PASSED' if results[n]['passed'] else 'FAILED'}")
        for f in results[n]["failures"]:
            log(f"    FAIL  {f}")
    write_review(video, plan, parent_title)
    log(f"\nDone. Open out/{video.slug}/shorts/REVIEW.md")


def write_review(video: Video, plan: dict, parent_title: str) -> None:
    lines = [f"# Shorts for: {parent_title}", "",
             "Each Short is made only of sentences from the approved script, in the narrator's own voice. The on-screen text, "
             "title and description were checked by a separate Claude review. Watch them, then upload one at a time, a few "
             "days apart. When you upload, set the full video as the Related video.", ""]
    for n, item in enumerate(plan["shorts"], start=1):
        d = video.dir / "shorts" / f"short_{n}"
        r = item.get("review", {})
        lines += [f"## Short {n}: {item['title']}", "", f"- On screen: **{item['hook_text']}**",
                  f"- Text check: {r.get('verdict', 'not run')}" + (" (lines rewritten by the check)" if item.get("review_history") else "")]
        lines += [f"  - {p}" for p in r.get("problems", [])]
        lines += [f"- Length: about {item['seconds']:.0f} seconds",
                  f"- Why it works: {item['why'] or 'n/a'}", f"- File: shorts/short_{n}/short.mp4",
                  f"- Description: shorts/short_{n}/description.txt", f"- Tags: {', '.join(item['tags'])}"]
        qc = d / "qc.json"
        if r.get("verdict") != "PASS":
            lines.append("- Not built: the text check did not pass it. Do not upload anything from this folder.")
        elif qc.exists():
            r = json.loads(qc.read_text())
            lines.append(f"- Quality check: {'PASSED' if r['passed'] else 'FAILED'}")
            lines += [f"  - FAIL: {f}" for f in r["failures"]] + [f"  - note: {w}" for w in r["warnings"]]
        lines += ["", "Narration:", "", f"> {item['text']}", ""]
    video.path("shorts", "REVIEW.md").write_text("\n".join(lines))
