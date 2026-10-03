"""Stage 6: assemble the video with ffmpeg.

Each shot becomes a clip with a slow zoom or pan (cached, so re-runs only rebuild
changed shots). Clips are joined, the voiceover is laid under them, and captions
built from the exact sentence timings are burned in. captions.srt is kept so it
can also be uploaded as a caption track.
Writes clips/*.mp4, captions.srt and video.mp4.
"""

from __future__ import annotations

import hashlib
import re

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


def build_clip(img, out, frames: int, motion: int) -> None:
    z, x, y = (part.format(n=max(frames - 1, 1)) for part in MOTIONS[motion % len(MOTIONS)])
    vf = (
        "scale=2880:1620:force_original_aspect_ratio=increase,crop=2880:1620,setsar=1,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={config.WIDTH}x{config.HEIGHT}:fps={config.FPS},"
        "format=yuv420p"
    )
    sh(["ffmpeg", "-y", "-v", "error", "-i", str(img), "-vf", vf, "-frames:v", str(frames),
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

    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "clips/list.txt", "-i", "voiceover.wav"]
    if captions:
        cmd += ["-vf", f"subtitles=captions.srt:force_style='{CAPTION_STYLE}'"]
    cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest",
            "-movflags", "+faststart", "video.mp4"]
    log("  assembling video.mp4 ...")
    sh(cmd, cwd=video.dir)
    log(f"  video.mp4 written ({tl['duration'] / 60:.1f} minutes)")


def check_ffmpeg() -> list[str]:
    problems = []
    try:
        filters = sh(["ffmpeg", "-hide_banner", "-filters"]).stdout
    except (StageError, FileNotFoundError):
        return ["ffmpeg isn't installed. On your Mac: brew install ffmpeg"]
    for name in ("zoompan", "subtitles"):
        if not re.search(rf"\b{name}\b", filters):
            problems.append(f"Your ffmpeg has no '{name}' filter. Reinstall with: brew reinstall ffmpeg")
    return problems
