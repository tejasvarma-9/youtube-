"""Shorts: 2 to 4 vertical clips (1080x1920, under a minute) cut from a finished, fact-checked video.

python -m pipeline shorts <slug>   (needs the video's script, fact-check, voiceover, shots and pictures)

A Short never has new narration. Claude only CHOOSES sentences that already exist in the video's
script; the clip is those sentences, in order, cut from the video's own voiceover (same voice, no
new voice cost), over the video's own pictures shown in a vertical frame (the picture on top of a
blurred copy of itself), with large word-by-word captions and a short logo card at the end. Because
the words are the ones the fact-check already passed, nothing needs fact-checking again. The title
and description Claude writes may only use numbers that appear in the Short's own sentences.

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

CLIP_VERSION = "short2"   # bump when the picture layout changes, so cached clips are rebuilt
HEAD_PAD_S = 0.10         # a little room before the first word of a sentence
TAIL_PAD_S = 0.35         # and after the last
GAP_S = 0.12              # silence between kept sentences
HOLD_S = 5.0              # the same picture is re-framed (punch in or pan) at least this often
FG_TOP = 420              # top edge of the picture inside the 1080x1920 frame
HEADER_Y = 220            # logo and channel name, kept clear of the Shorts app's top bar
# Captions sit above the Shorts app's title and buttons (bottom ~400 px). libass scales its font units to
# the video height (units of 1/288), so 11 is about 73 px and MarginV 110 puts the line's bottom near y=1185, just under the picture.
CAPTION_STYLE = (
    "FontName=Arial,FontSize=11,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,"
    "BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=110"
)
BAD_HOOK_START = re.compile(r"^\s*(and|but|so|then|now|that|this|it|they|which|these|those|also)\b", re.I)
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
        if not title or len(title) > 70 or "#" in title:
            problems.append(f"{label}: the title must be 1 to 70 characters with no hashtags.")
        if not intro:
            problems.append(f"{label}: description_intro is empty.")
        allowed = _numbers(text)
        for where, value in (("title", title), ("description_intro", intro)):
            extra = sorted(_numbers(value) - allowed)
            if extra:
                problems.append(f"{label}: the {where} uses figure(s) {extra} that are not in the Short's own sentences.")
        tags = [str(t).strip() for t in sh_.get("tags", []) if str(t).strip()][:8]
        items.append({"title": title, "sentences": ids, "description_intro": intro, "tags": tags,
                      "why": str(sh_.get("why", "")).strip(), "seconds": round(total, 1), "text": text})
    return items, problems


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


def _backdrop(src):
    """The picture, blurred and darkened to fill the frame, with the logo and channel name at the top."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    from .s8_thumbnail import _font

    W, H = config.SHORT_WIDTH, config.SHORT_HEIGHT
    bg = ImageEnhance.Brightness(_cover(src, W, H).filter(ImageFilter.GaussianBlur(42))).enhance(0.55)
    d = ImageDraw.Draw(bg)
    font = _font(46)
    name = config.CHANNEL_NAME
    tw = d.textlength(name, font=font)
    size = 96
    if config.LOGO.exists():
        logo = Image.open(config.LOGO).convert("RGBA").resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
        total = size + 20 + tw
        x0 = (W - total) / 2
        bg.paste(logo, (round(x0), HEADER_Y), mask)
        d.text((x0 + size + 20, HEADER_Y + size / 2 - 28), name, font=font, fill=(255, 255, 255), stroke_width=3, stroke_fill=(16, 16, 16))
    else:
        d.text(((W - tw) / 2, HEADER_Y + 20), name, font=font, fill=(255, 255, 255), stroke_width=3, stroke_fill=(16, 16, 16))
    return bg


def build_clip(img, out, frames: int, motion: int) -> None:
    """One picture as a vertical clip: the picture across the middle with a slow push-in or pan."""
    from PIL import Image

    W, H, FPS = config.SHORT_WIDTH, config.SHORT_HEIGHT, config.FPS
    src = Image.open(img).convert("RGB")
    sw, sh_ = src.size
    fh = round(sh_ * W / sw)
    bg = _backdrop(src)
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
    if not config.LOGO.exists():
        raise StageError(f"The channel logo is missing ({config.LOGO}). Every Short ends on the logo card, so add it first.")
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
        key = hashlib.sha1(f"{CLIP_VERSION}|{st.st_size}|{st.st_mtime_ns}|{frames}|{mot % len(MOTIONS)}".encode()).hexdigest()[:10]
        clip = clips_dir / f"{idx:02d}_{key}.mp4"
        clips.append(clip)
        if not clip.exists():
            todo.append((img, clip, frames, mot))
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
    return plan if plan.get("script_sha") == sha and plan.get("voice_seconds") == round(video.read_json("timeline.json")["duration"], 1) else None


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
        plan = {"script_sha": hashlib.sha1(video.read_text("script.txt").encode()).hexdigest()[:12],
                "voice_seconds": round(tl["duration"], 1), "shorts": items}
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
        log(f"  planned {len(items)} Shorts:")
        for n, it in enumerate(items, start=1):
            log(f"    {n}. {it['title']}  ({it['seconds']:.0f}s)")
    if plan_only:
        return
    parent_title = json.loads((video.dir / "metadata.json").read_text()).get("title", "") if (video.dir / "metadata.json").exists() else video.brief().get("topic", "")
    sources_md = (video.dir / "sources.md").read_text() if (video.dir / "sources.md").exists() else ""
    results = {}
    for n, item in enumerate(plan["shorts"], start=1):
        if only and n != only:
            continue
        build_short(video, n, item, stub=stub)
        d = video.dir / "shorts" / f"short_{n}"
        meta = {"title": item["title"], "tags": item["tags"], "sentences": item["sentences"], "why": item["why"],
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
             "Each Short is made only of sentences from the approved script, in the narrator's own voice. Watch them, "
             "then upload one at a time. When you upload, set the full video as the Related video.", ""]
    for n, item in enumerate(plan["shorts"], start=1):
        d = video.dir / "shorts" / f"short_{n}"
        lines += [f"## Short {n}: {item['title']}", "", f"- Length: about {item['seconds']:.0f} seconds plus a {config.SHORT_END_CARD_S:.1f}s logo card",
                  f"- Why it works: {item['why'] or 'n/a'}", f"- File: shorts/short_{n}/short.mp4",
                  f"- Description: shorts/short_{n}/description.txt", f"- Tags: {', '.join(item['tags'])}"]
        qc = d / "qc.json"
        if qc.exists():
            r = json.loads(qc.read_text())
            lines.append(f"- Quality check: {'PASSED' if r['passed'] else 'FAILED'}")
            lines += [f"  - FAIL: {f}" for f in r["failures"]] + [f"  - note: {w}" for w in r["warnings"]]
        lines += ["", "Narration:", "", f"> {item['text']}", ""]
    video.path("shorts", "REVIEW.md").write_text("\n".join(lines))
