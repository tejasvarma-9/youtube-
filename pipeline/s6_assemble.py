"""Stage 6: assemble the video with ffmpeg.

Each shot becomes a clip with a slow zoom or pan (cached, so re-runs only rebuild
changed shots). Clips are joined, the voiceover is laid under them, and captions
built from the exact sentence timings are burned in. captions.srt is kept so it
can also be uploaded as a caption track.
Writes clips/*.mp4, captions.srt and video.mp4.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import shutil

from . import config
from .common import StageError, Video, fmt_ts, log, run as sh

MOTIONS = [
    # zoom in, centered
    ("1+0.10*on/{n}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),
    # pan left to right
    ("1.10", "(iw-iw/zoom)*on/{n}", "(ih-ih/zoom)/2"),
    # zoom out, centered
    ("1.10-0.10*on/{n}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),
    # pan right to left
    ("1.10", "(iw-iw/zoom)*(1-on/{n})", "(ih-ih/zoom)/2"),
]
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


def build_clip(img, out, frames: int, motion: int) -> None:
    z, x, y = (part.format(n=max(frames - 1, 1)) for part in MOTIONS[motion % len(MOTIONS)])
    vf = (
        "scale=2880:1620:force_original_aspect_ratio=increase,crop=2880:1620,setsar=1,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={config.WIDTH}x{config.HEIGHT}:fps={config.FPS},"
        "format=yuv420p"
    )
    sh([find_ffmpeg()[0], "-y", "-v", "error", "-i", str(img), "-vf", vf, "-frames:v", str(frames),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-r", str(config.FPS), str(out)])


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
    clips = []
    for idx, s in enumerate(shots):
        img = video.dir / "images" / f"shot_{s['id']:03d}.png"
        if not img.exists():
            raise StageError(f"Image for shot {s['id']} is missing. Run: python -m pipeline images {video.slug}")
        f0, f1 = round(s["start"] * config.FPS), round(s["end"] * config.FPS)
        frames = max(f1 - f0, 1)
        key = hashlib.sha1(f"{img.stat().st_size}|{img.stat().st_mtime_ns}|{frames}|{idx % len(MOTIONS)}".encode()).hexdigest()[:10]
        clip = video.path("clips", f"shot_{s['id']:03d}_{key}.mp4")
        if not clip.exists():
            for old in clip.parent.glob(f"shot_{s['id']:03d}_*.mp4"):
                old.unlink()
            build_clip(img, clip, frames, idx)
        clips.append(clip)
        if (idx + 1) % 10 == 0 or idx + 1 == len(shots):
            log(f"    clips {idx + 1}/{len(shots)}")

    concat = video.path("clips", "list.txt")
    concat.write_text("".join(f"file '{c.name}'\n" for c in clips))
    srt = video.path("captions.srt")
    write_srt(tl["sentences"], srt)

    cmd = [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "clips/list.txt", "-i", "voiceover.wav"]
    if captions:
        cmd += ["-vf", f"subtitles=captions.srt:force_style='{CAPTION_STYLE}'"]
    cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest",
            "-movflags", "+faststart", "video.mp4"]
    log("  assembling video.mp4 ...")
    sh(cmd, cwd=video.dir)
    video.write_json("assembly.json", {"ffmpeg": ffmpeg, "burned_captions": captions})
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
