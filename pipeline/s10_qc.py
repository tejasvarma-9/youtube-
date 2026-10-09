"""Quality check: the finished video, checked before Tejas watches it.

- Captions vs voice: the finished video's audio is heard again (faster-whisper, local) and each
  caption line's start is compared with when its first word is actually said.
- Missing words: runs of script words the recognizer never heard (skipped or garbled speech).
- Sound: loudness against YouTube's level, peaks, and long silences.
- Picture: 1920x1080, black frames, and whether pictures were upscaled or only resized.
- Pictures and claims: Claude looks at contact sheets of every picture and at the thumbnail, and
  checks the title and thumbnail text against the fact list.

Writes qc.json and qc.md. A failed check doesn't stop the pipeline (the review package still gets
built, with the failures at the top), but approve refuses a video that failed.
"""

from __future__ import annotations

import difflib
import json
import re

from PIL import Image, ImageDraw

from . import align, config, llm
from .common import StageError, Video, extract_json, fill, fmt_ts, log, prompt
from .s6_assemble import find_ffmpeg

DRIFT_WARN_S = 0.5      # a caption line this far from its spoken word is noticeable
DRIFT_FAIL_S = 1.0
DRIFT_FAIL_SHARE = 0.05  # or this share of lines over DRIFT_WARN_S
MISSING_RUN = 4         # this many script words in a row never heard = skipped or garbled speech
LOUDNESS_TARGET = (-17.0, -11.0)  # YouTube plays at about -14 LUFS
LOUDNESS_FAIL = (-24.0, -8.0)
PEAK_MAX_DBTP = -1.0
SILENCE_MAX_S = 2.5
SHEET_COLS, SHEET_ROWS, THUMB_W = 4, 3, 480


def _script_rules(video: Video, failures: list) -> None:
    """The welcome line and the like-and-subscribe close are required in every video, even after hand edits."""
    from .lint import _opening, _outro
    script = video.read_text("script.txt")
    failures += [f"Script: {e}" for e in _opening(script)[0] + _outro(script)]


def run(video: Video, stub: bool = False) -> dict:
    failures, warnings, checks = [], [], {}
    if stub or video.read_json("timeline.json").get("voice") == "stub":
        result = {"passed": True, "stub": True, "failures": [], "warnings": ["Stub run: quality checks skipped."], "checks": {}}
        _write(video, result)
        return result

    _technical(video, failures, warnings, checks)
    _script_rules(video, failures)
    _captions(video, failures, warnings, checks)
    _review_with_claude(video, failures, warnings, checks)
    meta = video.read_json("metadata.json")
    warnings += meta.get("title_problems", []) + meta.get("chapter_problems", [])

    result = {"passed": not failures, "failures": failures, "warnings": warnings, "checks": checks,
              "video": _video_id(video)}
    _write(video, result)
    log(f"  quality check: {'PASSED' if result['passed'] else 'FAILED'} "
        f"({len(failures)} problems, {len(warnings)} warnings). See out/{video.slug}/qc.md")
    for f in failures:
        log(f"    FAIL  {f}")
    return result


def _video_id(video: Video) -> str:
    st = video.path("video.mp4").stat()
    return f"{st.st_size}|{st.st_mtime_ns}"


def is_current(video: Video) -> bool:
    p = video.dir / "qc.json"
    return p.exists() and (video.dir / "video.mp4").exists() and json.loads(p.read_text()).get("video") == _video_id(video)


# --- sound and picture -------------------------------------------------------

def _ff(args: list[str]) -> str:
    import subprocess
    ffmpeg = find_ffmpeg()[0] or "ffmpeg"
    res = subprocess.run([ffmpeg, "-hide_banner", "-nostats", *args], capture_output=True, text=True)
    return res.stderr


def _technical(video: Video, failures: list, warnings: list, checks: dict) -> None:
    import subprocess
    mp4 = str(video.path("video.mp4"))
    tl = video.read_json("timeline.json")
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height:format=duration", "-of", "json", mp4], capture_output=True, text=True)
    try:
        info = json.loads(probe.stdout)
        w, h = info["streams"][0]["width"], info["streams"][0]["height"]
        dur = float(info["format"]["duration"])
    except (ValueError, KeyError, IndexError):
        failures.append("Couldn't read video.mp4 (ffprobe failed). Re-run the assembly.")
        return
    checks["resolution"] = f"{w}x{h}"
    checks["duration"] = round(dur, 2)
    if (w, h) != (config.WIDTH, config.HEIGHT):
        failures.append(f"Video is {w}x{h}, not {config.WIDTH}x{config.HEIGHT}.")
    asm = video.path("assembly.json")
    end_s = (json.loads(asm.read_text()).get("end_card_seconds", 0) if asm.exists() else 0) or 0
    checks["end_card_seconds"] = end_s
    if not end_s:
        failures.append(f"No logo end card. Every video ends on it: run python -m pipeline assemble {video.slug}")
    if abs(dur - tl["duration"] - end_s) > 1.0:
        failures.append(f"Video is {dur:.1f}s but the voiceover is {tl['duration']:.1f}s"
                        f"{f' plus a {end_s:.0f}s end card' if end_s else ''}; something was cut or padded.")

    out = _ff(["-i", mp4, "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out, re.S)
    if m:
        ln = json.loads(m.group(0))
        lufs, peak = float(ln["input_i"]), float(ln["input_tp"])
        checks["loudness_lufs"], checks["true_peak_dbtp"] = lufs, peak
        if not LOUDNESS_FAIL[0] <= lufs <= LOUDNESS_FAIL[1]:
            failures.append(f"Loudness is {lufs:.1f} LUFS; YouTube plays at about -14, so it will sound far too {'quiet' if lufs < -14 else 'loud'}.")
        elif not LOUDNESS_TARGET[0] <= lufs <= LOUDNESS_TARGET[1]:
            warnings.append(f"Loudness is {lufs:.1f} LUFS; about -14 is ideal for YouTube.")
        if peak > PEAK_MAX_DBTP:
            warnings.append(f"Audio peaks at {peak:.1f} dBTP and may clip on some devices (keep it under {PEAK_MAX_DBTP}).")
    else:
        warnings.append("Couldn't measure loudness.")

    out = _ff(["-i", mp4, "-vn", "-af", f"silencedetect=n=-45dB:d={SILENCE_MAX_S}", "-f", "null", "-"])
    silences = []
    for start, length in re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", out):
        s = float(start) - float(length)
        if s < dur - end_s - 1.5:  # the quiet at the end, and the silent logo end card, are fine
            silences.append(f"{fmt_ts(s)} ({float(length):.1f}s)")
    checks["long_silences"] = silences
    if silences:
        failures.append("Long silences in the voiceover at " + ", ".join(silences[:5]) + ".")

    out = _ff(["-i", mp4, "-an", "-vf", "blackdetect=d=0.5:pix_th=0.10", "-f", "null", "-"])
    blacks = [fmt_ts(float(s)) for s in re.findall(r"black_start:([\d.]+)", out)]
    checks["black_frames"] = blacks
    if blacks:
        failures.append("Black frames at " + ", ".join(blacks[:5]) + ".")

    shots = video.read_json("shots.json")["shots"]
    small = 0
    up_dir = video.dir / "images" / "upscaled"
    for s in shots:
        img = video.dir / "images" / f"shot_{s['id']:03d}.png"
        if not img.exists():
            continue
        with Image.open(img) as im:
            big = im.width >= config.WIDTH * 1.25
        if not big and not any(up_dir.glob(f"{img.stem}_*.png")):
            small += 1
    checks["pictures_only_resized"] = small
    if small:
        warnings.append(f"{small} of {len(shots)} pictures were only resized, not upscaled, so they look soft. "
                        "Check the upscaler (README: 'Sharper images').")


# --- captions vs voice ---------------------------------------------------------

def _srt_cues(text: str) -> list[dict]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        m = re.match(r"(\d+):(\d+):(\d+),(\d+) --> ", lines[1])
        if not m:
            continue
        h, mi, s, ms = map(int, m.groups())
        cues.append({"start": h * 3600 + mi * 60 + s + ms / 1000, "text": " ".join(lines[2:])})
    return cues


def caption_drift(cues: list[dict], heard: list[tuple[str, float, float]]) -> dict:
    """Compare each caption line's start with when its first word was heard.

    Returns the drift per line (only lines whose first word was clearly heard) and the runs of
    script words that were never heard.
    """
    words = [(ci, w) for ci, c in enumerate(cues) for w in c["text"].split()]
    a = [re.sub(r"[^a-z0-9]", "", w.lower()) for _, w in words]
    b = [re.sub(r"[^a-z0-9]", "", w.lower()) for w, _, _ in heard]
    hit = [None] * len(words)
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            hit[blk.a + k] = heard[blk.b + k][1]
    first = {}
    for k, (ci, _) in enumerate(words):
        first.setdefault(ci, k)
    drifts = []
    for ci, k in first.items():
        if hit[k] is not None:
            drifts.append({"line": ci, "at": cues[ci]["start"], "drift": round(cues[ci]["start"] - hit[k], 2),
                           "text": cues[ci]["text"]})
    missing, k = [], 0
    while k < len(words):
        if hit[k] is None and a[k]:
            j = k
            while j < len(words) and hit[j] is None:
                j += 1
            if j - k >= MISSING_RUN:
                missing.append({"at": cues[words[k][0]]["start"], "text": " ".join(w for _, w in words[k:j])})
            k = j
        else:
            k += 1
    matched = sum(h is not None for h in hit) / max(len(words), 1)
    return {"drifts": drifts, "missing": missing, "matched": round(matched, 3)}


def _captions(video: Video, failures: list, warnings: list, checks: dict) -> None:
    srt = video.dir / "captions.srt"
    if not srt.exists():
        failures.append("captions.srt is missing. Re-run the assembly.")
        return
    if not align.available():
        warnings.append("Captions vs voice not checked: faster-whisper isn't installed (pip install -r requirements.txt).")
        return
    log("  listening to the finished video to check the captions...")
    try:
        heard = align.transcribe(video.path("video.mp4"))
    except Exception as e:
        warnings.append(f"Captions vs voice not checked: the recognizer failed ({e}).")
        return
    cues = _srt_cues(srt.read_text())
    r = caption_drift(cues, heard)
    checks["caption_words_heard"] = r["matched"]
    late = [d for d in r["drifts"] if abs(d["drift"]) > DRIFT_WARN_S]
    checks["caption_lines_checked"] = len(r["drifts"])
    checks["caption_lines_off"] = len(late)
    worst = sorted(late, key=lambda d: -abs(d["drift"]))[:5]
    desc = "; ".join(f"{fmt_ts(d['at'])} \"{d['text'][:40]}\" {'late' if d['drift'] > 0 else 'early'} by {abs(d['drift']):.1f}s"
                     for d in worst)
    if any(abs(d["drift"]) > DRIFT_FAIL_S for d in late) or len(late) > DRIFT_FAIL_SHARE * max(len(r["drifts"]), 1):
        failures.append(f"{len(late)} caption lines are off from the voice by more than {DRIFT_WARN_S}s: {desc}. "
                        f"Fix: python -m pipeline align {video.slug}, then run --from assemble.")
    elif late:
        warnings.append(f"{len(late)} caption lines are slightly off: {desc}.")
    if r["matched"] < 0.8:
        failures.append(f"Only {r['matched']:.0%} of the caption words were heard in the voice. The voiceover may be cut or garbled.")
    for m in r["missing"][:5]:
        warnings.append(f"At {fmt_ts(m['at'])} the voice may have skipped or garbled: \"{m['text'][:80]}\". Listen there.")


# --- pictures, thumbnail and claims (Claude) ------------------------------------

def contact_sheets(video: Video) -> list:
    shots = video.read_json("shots.json")["shots"]
    per = SHEET_COLS * SHEET_ROWS
    th = THUMB_W * 9 // 16
    out = []
    qc_dir = video.path("qc", ".keep").parent
    for old in qc_dir.glob("sheet_*.png"):
        old.unlink()
    for n in range(0, len(shots), per):
        group = shots[n:n + per]
        sheet = Image.new("RGB", (SHEET_COLS * THUMB_W, SHEET_ROWS * (th + 28)), "white")
        draw = ImageDraw.Draw(sheet)
        for k, s in enumerate(group):
            x, y = (k % SHEET_COLS) * THUMB_W, (k // SHEET_COLS) * (th + 28)
            img = video.dir / "images" / f"shot_{s['id']:03d}.png"
            if img.exists():
                with Image.open(img) as im:
                    sheet.paste(im.convert("RGB").resize((THUMB_W, th)), (x, y + 28))
            draw.text((x + 6, y + 6), f"shot {s['id']}", fill="black")
        path = qc_dir / f"sheet_{n // per + 1:02d}.png"
        sheet.save(path)
        out.append(path)
    return out


def _review_with_claude(video: Video, failures: list, warnings: list, checks: dict) -> None:
    meta = video.read_json("metadata.json")
    shots = video.read_json("shots.json")["shots"]
    sheets = contact_sheets(video)
    files = [str(p.relative_to(video.dir)) for p in sheets] + ["thumbnail.png"]
    text = fill(prompt("qc.md"),
                FILES="\n".join(files),
                SCENES="\n".join(f"shot {s['id']}: {s.get('scene', '')[:200]}" for s in shots),
                TITLE=meta["title"], THUMBNAIL_TEXT=meta["thumbnail_text"],
                DESCRIPTION_INTRO=meta.get("description_intro", ""),
                FACTS=video.read_text("facts.txt"))
    try:
        answer = llm.ask(text, "qc", read_dir=str(video.dir))
        video.path("raw", "qc_answer.md").write_text(answer)
        data = extract_json(answer)
    except (StageError, ValueError) as e:
        warnings.append(f"Pictures and claims not checked by Claude ({str(e)[:200]}).")
        return
    checks["claude_review"] = data
    for c in data.get("claims", []):
        if c.get("verdict") not in ("SUPPORTED", "OK"):
            failures.append(f"{c.get('where', 'Claim')} \"{c.get('text', '')}\" {c.get('verdict', '').lower()}: {c.get('note', '')} "
                            f"Suggested: {c.get('fix', '')}")
    for i in data.get("images", []):
        msg = f"Shot {i.get('shot')}: {i.get('problem')}"
        (failures if i.get("severity") == "high" else warnings).append(msg)
    if data.get("thumbnail_problem"):
        failures.append(f"Thumbnail: {data['thumbnail_problem']}")


# --- report ---------------------------------------------------------------------

def _write(video: Video, result: dict) -> None:
    video.write_json("qc.json", result)
    lines = [f"# Quality check: {'PASSED' if result['passed'] else 'FAILED'}", ""]
    if result["failures"]:
        lines += ["## Must fix", ""] + [f"- {f}" for f in result["failures"]] + [""]
    if result["warnings"]:
        lines += ["## Worth a look", ""] + [f"- {w}" for w in result["warnings"]] + [""]
    c = result.get("checks", {})
    if c:
        lines += ["## Measured", ""]
        for k in ("resolution", "duration", "loudness_lufs", "true_peak_dbtp", "caption_lines_checked",
                  "caption_lines_off", "caption_words_heard", "pictures_only_resized"):
            if k in c:
                lines.append(f"- {k.replace('_', ' ')}: {c[k]}")
    video.path("qc.md").write_text("\n".join(lines) + "\n")
