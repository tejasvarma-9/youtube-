"""Stage 9: the review package for Tejas, and the approval record.

review/ holds everything needed to judge the video in one place. Approving
records a fingerprint of the exact files approved; the uploader (not built yet)
will refuse to upload anything that has changed since.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone

from . import config
from .common import StageError, Video, fmt_ts, log

APPROVED_FILES = ["video.mp4", "thumbnail.png", "description.txt", "metadata.json", "captions.srt"]


def _link(src, dst) -> None:
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def run(video: Video, stub: bool = False) -> None:
    meta = video.read_json("metadata.json")
    tl = video.read_json("timeline.json")
    fc = video.read_json("factcheck.json")
    shots = video.read_json("shots.json")["shots"]
    lint = video.read_json("script_lint.json")
    review = video.path("review", ".keep").parent
    for name in ["video.mp4", "thumbnail.png", "description.txt", "captions.srt", "factcheck.md", "script.txt", "sources.md"]:
        src = video.dir / name
        if not src.exists():
            raise StageError(f"{name} is missing. Run the earlier stages first.")
        _link(src, review / name)

    img_cost = len(shots) * config.PRICE_PER_IMAGE_BATCH + config.PRICE_PER_IMAGE_SYNC
    provider = tl.get("provider", "chirp")
    voice_cost = config.voice_cost(provider, tl["duration"], tl["characters"])
    voice_note = (f"Voice: about ${voice_cost:.2f} (Gemini voice)" if provider == "gemini" else
                  f"Voice: {tl['characters']:,} characters, free under {config.TTS_FREE_CHARS_PER_MONTH:,} a month "
                  f"(${voice_cost:.2f} if over)")
    verdicts = {}
    for f in fc.get("facts", []):
        verdicts[f.get("verdict", "?")] = verdicts.get(f.get("verdict", "?"), 0) + 1
    asm = video.dir / "assembly.json"
    burned = json.loads(asm.read_text()).get("burned_captions", True) if asm.exists() else True
    warnings = lint["warnings"] + meta.get("title_problems", []) + meta.get("chapter_problems", [])
    if not burned:
        warnings.append("video.mp4 has no burned-in captions (your ffmpeg can't). captions.srt is included.")

    lines = [
        f"# Review: {meta['title']}",
        "",
        f"{'**STUB RUN: placeholder audio, images and facts. Not for upload.**' if stub or tl.get('voice') == 'stub' else ''}",
        "",
        f"- **Length:** {tl['duration'] / 60:.1f} minutes, {lint['words']:,} words, {len(shots)} images",
        f"- **Fact-check:** {'passed' if fc.get('passed') else 'FAILED'} ({', '.join(f'{v} {k.lower()}' for k, v in sorted(verdicts.items()))})",
        f"- **Cost of this video:** about ${img_cost:.2f} in images. {voice_note}.",
        "",
        "## Watch these",
        "",
        "- [video.mp4](video.mp4)",
        "- [thumbnail.png](thumbnail.png)",
        "",
        "## Title",
        "",
        f"**{meta['title']}**",
        "",
        "Other options: " + " / ".join(t for t in meta.get("title_options", []) if t != meta["title"]),
        "",
        f"Thumbnail text: **{meta['thumbnail_text']}**",
        "",
        "To change the title, edit `title` in metadata.json, then re-run `python -m pipeline review " + video.slug + "`.",
        "",
        "## Description (as it will be posted)",
        "",
        "```",
        meta["description"].strip(),
        "```",
        "",
        "Tags: " + ", ".join(meta.get("tags", [])),
        "",
    ]
    if warnings:
        lines += ["## Warnings", ""] + [f"- {w}" for w in warnings] + [""]
    lines += [
        "## Before you approve",
        "",
        "- [ ] Watched the whole video. The voice reads every number correctly.",
        "- [ ] Images match the narration and show no real logos or real people.",
        "- [ ] Read factcheck.md. Every figure is sourced or said as an estimate.",
        "- [ ] The original angle is clear in the first minute.",
        "- [ ] Nothing reads as financial advice.",
        "- [ ] Title, thumbnail and description are right.",
        "",
        "Approve with:",
        "",
        f"    python -m pipeline approve {video.slug}",
        "",
        "Approving only records your sign-off on these exact files. Nothing is uploaded yet.",
    ]
    (review / "REVIEW.md").write_text("\n".join(lines) + "\n")
    chapters = ", ".join(f"{fmt_ts(c['start'])} {c['title']}" for c in meta["chapters"])
    log(f"  review package: out/{video.slug}/review/REVIEW.md")
    log(f"  chapters: {chapters}")


def fingerprint(video: Video) -> dict:
    out = {}
    for name in APPROVED_FILES:
        p = video.dir / name
        if not p.exists():
            raise StageError(f"{name} is missing; build the review package first.")
        out[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def approve(video: Video, publish_at: str = "") -> None:
    tl = video.read_json("timeline.json")
    if tl.get("voice") == "stub":
        raise StageError("This is a stub run with placeholder audio and images. It can't be approved.")
    if not video.read_json("factcheck.json").get("passed"):
        raise StageError("Fact-check hasn't passed, so this video can't be approved.")
    record = {
        "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "publish_at": publish_at,
        "files": fingerprint(video),
    }
    video.write_json("approval.json", record)
    log(f"  approved '{video.slug}'. If any of {', '.join(APPROVED_FILES)} changes, approve it again.")


def is_approved(video: Video) -> bool:
    p = video.dir / "approval.json"
    if not p.exists():
        return False
    return json.loads(p.read_text())["files"] == fingerprint(video)
