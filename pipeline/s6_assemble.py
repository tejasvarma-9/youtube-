"""Stage 6: assemble the video with ffmpeg.

Each shot becomes a clip with a slow zoom or pan (cached, so re-runs only rebuild
changed shots). Clips are joined, the voiceover is laid under them, and captions
built from the exact sentence timings are burned in. captions.srt is kept so it
can also be uploaded as a caption track.
Writes clips/*.mp4, captions.srt and video.mp4.
"""

from __future__ import annotations

import concurrent.futures
import functools
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import config
from .common import StageError, Video, fmt_ts, log, run as sh

# Slow camera moves, each (zoom, x, y) at progress t from 0 to 1. zoom >= 1 is how far in; x and y are
# where the window sits in the room the zoom leaves (0 left/top, 1 right/bottom).
MOTIONS = [
    lambda t: (1 + 0.10 * t, 0.5, 0.5),        # zoom in, centered
    lambda t: (1.10, t, 0.5),                  # pan left to right
    lambda t: (1.10 - 0.10 * t, 0.5, 0.5),     # zoom out, centered
    lambda t: (1.10, 1 - t, 0.5),              # pan right to left
]
CLIP_VERSION = "smooth1"  # bump when build_clip changes, so cached clips are rebuilt
CAPTION_STYLE = (
    "FontName=Arial,FontSize=15,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,"
    "BorderStyle=1,Outline=2.4,Shadow=0,Alignment=2,MarginV=28"
)


FFMPEG_FULL = ["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "/usr/local/opt/ffmpeg-full/bin/ffmpeg"]


def _filters(ffmpeg: str) -> str:
    try:
        return sh([ffmpeg, "-hide_banner", "-filters"]).stdout
    except (StageError, OSError):
        return ""


@functools.lru_cache(maxsize=None)
def find_ffmpeg() -> tuple[str, bool]:
    """The ffmpeg to use and whether it can burn in captions (has the 'subtitles' filter).

    Homebrew's regular ffmpeg no longer ships the caption renderer; ffmpeg-full does.
    Prefers an ffmpeg that can burn captions, wherever it is. Returns ("", False) if none works.
    """
    candidates = [os.environ.get("FFMPEG_BIN", ""), shutil.which("ffmpeg") or "", *FFMPEG_FULL]
    usable = []
    for c in dict.fromkeys(c for c in candidates if c):
        if os.path.exists(c):
            f = _filters(c)
            if re.search(r"\bzoompan\b", f):
                if re.search(r"\bsubtitles\b", f):
                    return c, True
                usable.append(c)
    return (usable[0], False) if usable else ("", False)


@functools.lru_cache(maxsize=None)
def find_upscaler() -> str:
    """The Real-ESRGAN binary, or "" if it isn't installed (pictures are then only resized)."""
    for c in (config.UPSCALER_BIN, shutil.which("realesrgan-ncnn-vulkan") or ""):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


_upscale_failed = []


def upscale(img: Path) -> Path:
    """A 2x AI-upscaled copy of a picture smaller than the video, cached next to it.

    Falls back to the original picture (resized while cutting frames, as before) if the upscaler
    is missing or fails, so a broken upscaler never stops a video.
    """
    from PIL import Image

    binary = find_upscaler()
    if not binary or _upscale_failed:
        return img
    with Image.open(img) as im:
        if im.width >= config.WIDTH * 1.25:
            return img
    st = img.stat()
    key = hashlib.sha1(f"{config.UPSCALE_MODEL}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:10]
    out = img.parent / "upscaled" / f"{img.stem}_{key}.png"
    if out.exists():
        return out
    out.parent.mkdir(exist_ok=True)
    for old in out.parent.glob(f"{img.stem}_*.png"):
        old.unlink()
    cmd = [binary, "-i", str(img), "-o", str(out), "-n", config.UPSCALE_MODEL, "-s", "2",
           "-m", str(Path(binary).parent / "models")]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        ok = res.returncode == 0 and out.exists() and out.stat().st_size > 0
        detail = (res.stderr or res.stdout)[-600:]
    except (OSError, subprocess.TimeoutExpired) as e:
        ok, detail = False, str(e)
    if not ok:
        if out.exists():
            out.unlink()
        _upscale_failed.append(detail)
        log("  WARNING: the image upscaler failed, so pictures are only resized this run. After fixing it,\n"
            f"           delete the clips folder and re-run the assembly.\n{detail}")
        return img
    return out


def build_clip(img, out, frames: int, motion: int) -> None:
    """One shot as a slowly moving clip.

    Each frame is cut from the picture at a fractional position and resized once, so the move is
    perfectly smooth. (ffmpeg's zoompan snaps the window to whole pixels, which showed as shaking.)
    """
    from PIL import Image

    W, H = config.WIDTH, config.HEIGHT
    src = Image.open(img).convert("RGB")
    sw, sh_ = src.size
    # Fill a 16:9 window from the middle of the picture.
    bw, bh = (sh_ * W / H, sh_) if sw / sh_ > W / H else (sw, sw * H / W)
    bx, by = (sw - bw) / 2, (sh_ - bh) / 2
    move = MOTIONS[motion % len(MOTIONS)]
    cmd = [find_ffmpeg()[0], "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(config.FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for k in range(frames):
            zoom, px, py = move(k / max(frames - 1, 1))
            cw, ch = bw / zoom, bh / zoom
            x0, y0 = bx + (bw - cw) * px, by + (bh - ch) * py
            frame = src.resize((W, H), Image.BICUBIC, box=(x0, y0, x0 + cw, y0 + ch))
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait() != 0:
            raise StageError(f"ffmpeg failed building a clip:\n{err[-1500:]}")
    except BrokenPipeError:
        raise StageError(f"ffmpeg stopped while building a clip:\n{proc.stderr.read().decode(errors='replace')[-1500:]}")
    finally:
        if proc.poll() is None:
            proc.kill()


def end_card_image(path) -> None:
    """The channel logo centred on its own background colour, with the channel name under it."""
    from PIL import Image, ImageDraw
    from .s8_thumbnail import _font

    W, H = config.WIDTH, config.HEIGHT
    logo = Image.open(config.LOGO).convert("RGB")
    bg = logo.getpixel((2, 2))
    card = Image.new("RGB", (W, H), bg)
    size = int(H * 0.5)
    card.paste(logo.resize((size, size), Image.LANCZOS), ((W - size) // 2, int(H * 0.12)))
    draw = ImageDraw.Draw(card)
    font = _font(84)
    name = config.CHANNEL_NAME
    tw = draw.textlength(name, font=font)
    draw.text(((W - tw) / 2, int(H * 0.12) + size + 30), name, font=font, fill=(255, 255, 255))
    card.save(path)


def build_end_card(out, seconds: float) -> None:
    """A still clip in the same format as the shot clips, so it joins them cleanly."""
    png = out.with_suffix(".png")
    end_card_image(png)
    sh([find_ffmpeg()[0], "-y", "-v", "error", "-loop", "1", "-framerate", str(config.FPS), "-i", str(png),
        "-frames:v", str(max(1, round(seconds * config.FPS))), "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "18", "-pix_fmt", "yuv420p", str(out)])


def caption_chunks(text: str, max_chars: int = 38) -> list[str]:
    words, chunks, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            chunks.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        chunks.append(cur)
    # Don't leave a lonely last word on screen.
    if len(chunks) > 1 and len(chunks[-1].split()) == 1:
        last = chunks.pop()
        chunks[-1] += " " + last
    return chunks


def write_srt(sentences: list[dict], path) -> None:
    n, lines = 0, []
    for s in sentences:
        chunks = caption_chunks(s["text"])
        words = s.get("words")
        if words and len(words) == len(s["text"].split()):
            # Each caption line starts when its first word is said and stays up until the next line.
            k = 0
            starts = []
            for c in chunks:
                starts.append(max(words[k][0], s["start"]))
                k += len(c.split())
            for i, c in enumerate(chunks):
                a = starts[i]
                b = starts[i + 1] if i + 1 < len(chunks) else s["end"]
                n += 1
                lines += [str(n), f"{fmt_ts(a, srt=True)} --> {fmt_ts(max(b, a + 0.05), srt=True)}", c, ""]
            continue
        total = sum(len(c) for c in chunks) or 1
        t = s["start"]
        span = s["end"] - s["start"]
        for c in chunks:
            d = span * len(c) / total
            n += 1
            lines += [str(n), f"{fmt_ts(t, srt=True)} --> {fmt_ts(t + d, srt=True)}", c, ""]
            t += d
    path.write_text("\n".join(lines))


def run(video: Video, stub: bool = False, captions: bool = True) -> None:
    ffmpeg, can_burn = find_ffmpeg()
    if not ffmpeg:
        raise StageError("No usable ffmpeg found. On your Mac: brew install ffmpeg")
    if captions and not can_burn:
        log("  WARNING: your ffmpeg can't burn in captions, so video.mp4 will have none. captions.srt is still written.\n"
            "           To burn them in: brew install ffmpeg-full (see README), then re-run with --from assemble.")
        captions = False
    tl = video.read_json("timeline.json")
    shots = video.read_json("shots.json")["shots"]
    # Shot times follow the timeline's sentence times, so re-timed sentences move the pictures too.
    starts = {s["i"]: s["start"] for s in tl["sentences"]}
    if all(shot.get("sentences") and shot["sentences"][0] in starts for shot in shots):
        for n, shot in enumerate(shots):
            shot["start"] = 0.0 if n == 0 else starts[shot["sentences"][0]]
        for shot, nxt in zip(shots, shots[1:] + [{"start": tl["duration"]}]):
            shot["end"] = nxt["start"]
    clips, todo = [], []
    sharp = f"up:{config.UPSCALE_MODEL}" if find_upscaler() else "plain"
    for idx, s in enumerate(shots):
        img = video.dir / "images" / f"shot_{s['id']:03d}.png"
        if not img.exists():
            raise StageError(f"Image for shot {s['id']} is missing. Run: python -m pipeline images {video.slug}")
        f0, f1 = round(s["start"] * config.FPS), round(s["end"] * config.FPS)
        frames = max(f1 - f0, 1)
        key = hashlib.sha1(f"{CLIP_VERSION}|{sharp}|{img.stat().st_size}|{img.stat().st_mtime_ns}|{frames}|{idx % len(MOTIONS)}".encode()).hexdigest()[:10]
        clip = video.path("clips", f"shot_{s['id']:03d}_{key}.mp4")
        if not clip.exists():
            for old in clip.parent.glob(f"shot_{s['id']:03d}_*.mp4"):
                old.unlink()
            todo.append((img, clip, frames, idx))
        clips.append(clip)

    # Upscale one picture at a time (it uses the graphics chip), then build several clips at once:
    # each is mostly picture resizing plus its own ffmpeg process.
    if todo and sharp != "plain":
        log(f"    upscaling {len(todo)} pictures for a sharper video")
        todo = [(upscale(img), clip, frames, idx) for img, clip, frames, idx in todo]
    if todo:
        log(f"    building {len(todo)} clips ({len(clips) - len(todo)} already made)")
    workers = max(1, min(4, (os.cpu_count() or 2) - 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(build_clip, *job) for job in todo]
        for n, fut in enumerate(concurrent.futures.as_completed(futures), start=1):
            fut.result()
            if n % 10 == 0 or n == len(todo):
                log(f"    clips {n}/{len(todo)}")

    end_s = config.END_CARD_SECONDS if config.LOGO.exists() else 0
    if end_s > 0:
        logo = config.LOGO.stat()
        key = hashlib.sha1(f"{CLIP_VERSION}|{logo.st_size}|{logo.st_mtime_ns}|{end_s}|{config.CHANNEL_NAME}".encode()).hexdigest()[:10]
        card = video.path("clips", f"end_card_{key}.mp4")
        if not card.exists():
            for old in card.parent.glob("end_card_*"):
                old.unlink()
            build_end_card(card, end_s)
        clips.append(card)

    concat = video.path("clips", "list.txt")
    concat.write_text("".join(f"file '{c.name}'\n" for c in clips))
    srt = video.path("captions.srt")
    write_srt(tl["sentences"], srt)

    cmd = [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "clips/list.txt", "-i", "voiceover.wav"]
    if captions:
        cmd += ["-vf", f"subtitles=captions.srt:force_style='{CAPTION_STYLE}'"]
    cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p",
            # Level the voice to YouTube's playback loudness (about -14 LUFS), peaks under -1.5 dB.
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11" + (",apad" if end_s > 0 else ""), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest",
            "-movflags", "+faststart", "video.mp4"]
    log("  assembling video.mp4 ...")
    sh(cmd, cwd=video.dir)
    video.write_json("assembly.json", {"ffmpeg": ffmpeg, "burned_captions": captions, "end_card_seconds": end_s})
    log(f"  video.mp4 written ({tl['duration'] / 60:.1f} minutes)")


def check_ffmpeg() -> tuple[list[str], list[str]]:
    """(problems that block the pipeline, notes that don't)."""
    ffmpeg, can_burn = find_ffmpeg()
    if not ffmpeg:
        return ["No usable ffmpeg found (it needs the 'zoompan' filter). On your Mac: brew install ffmpeg"], []
    if not can_burn:
        return [], ["Your ffmpeg can't burn captions into the video, so videos will have no on-screen captions "
                    "(captions.srt is still made). Optional fix: brew install ffmpeg-full"]
    return [], []
