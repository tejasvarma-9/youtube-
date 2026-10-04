"""Command line: python -m pipeline <command> ...  (python -m pipeline -h for the list)."""

from __future__ import annotations

import argparse
import json
import shutil
import sys

from . import (config, s1_script, s2_factcheck, s3_voiceover, s4_shots, s5_images, s6_assemble, s7_metadata,
               s8_thumbnail, s9_review)
from .common import StageError, Video, log, slugify

STAGES = ["script", "factcheck", "voice", "shots", "images", "assemble", "metadata", "thumbnail", "review"]


def _done(v: Video, stage: str) -> bool:
    d = v.dir
    if stage == "script":
        return (d / "script.txt").exists()
    if stage == "factcheck":
        return (d / "factcheck.json").exists() and json.loads((d / "factcheck.json").read_text()).get("passed", False)
    if stage == "voice":
        return (d / "timeline.json").exists()
    if stage == "shots":
        return (d / "shots.json").exists()
    if stage == "images":
        if not (d / "shots.json").exists():
            return False
        shots = json.loads((d / "shots.json").read_text())["shots"]
        return all((d / "images" / f"shot_{s['id']:03d}.png").exists() for s in shots)
    if stage == "assemble":
        return (d / "video.mp4").exists()
    if stage == "metadata":
        return (d / "metadata.json").exists()
    if stage == "thumbnail":
        return (d / "thumbnail.png").exists()
    return False  # review is cheap; always rebuild


def run_stage(stage: str, v: Video, a) -> None:
    log(f"[{stage}] {v.slug}")
    if stage == "script":
        s1_script.run(v, stub=a.stub)
    elif stage == "factcheck":
        s2_factcheck.run(v, stub=a.stub, full=getattr(a, "full", False))
    elif stage == "voice":
        s3_voiceover.run(v, stub=a.stub, force=a.force)
    elif stage == "shots":
        s4_shots.run(v, stub=a.stub)
    elif stage == "images":
        s5_images.run(v, stub=a.stub, sync=a.sync)
    elif stage == "assemble":
        s6_assemble.run(v, stub=a.stub, captions=not a.no_captions)
    elif stage == "metadata":
        s7_metadata.run(v, stub=a.stub)
    elif stage == "thumbnail":
        s8_thumbnail.run(v, stub=a.stub)
    elif stage == "review":
        s9_review.run(v, stub=a.stub)


def cmd_new(a) -> Video:
    slug = a.slug or slugify(a.topic)
    d = config.OUT / slug
    if (d / "brief.json").exists():
        raise StageError(f"'{slug}' already exists. Pick another topic or pass --slug.")
    d.mkdir(parents=True, exist_ok=True)
    brief = {"topic": a.topic, "angle": a.angle or "", "character": a.character or "", "notes": a.notes or "",
             "words": a.words}
    (d / "brief.json").write_text(json.dumps(brief, indent=2) + "\n")
    log(f"Created out/{slug}/brief.json")
    log(f"Next: python -m pipeline run {slug}")
    return Video(slug)


def cmd_run(a, v: Video) -> None:
    start = STAGES.index(a.from_stage) if a.from_stage else 0
    for stage in STAGES[start:]:
        # Without --from, finished stages are skipped; with it, everything from there on re-runs.
        if not a.from_stage and _done(v, stage):
            log(f"[{stage}] already done, skipping")
            continue
        run_stage(stage, v, a)
    log(f"\nDone. Open out/{v.slug}/review/REVIEW.md")


def cmd_status() -> None:
    if not config.OUT.exists():
        log("No videos yet.")
        return
    for d in sorted(p for p in config.OUT.iterdir() if (p / "brief.json").exists()):
        v = Video(d.name)
        marks = " ".join(("✓" if _done(v, s) else "·") + s for s in STAGES[:-1])
        approved = "APPROVED" if (d / "approval.json").exists() else ""
        print(f"{d.name:40s} {marks} {approved}")


def cmd_doctor() -> int:
    problems, notes = s6_assemble.check_ffmpeg()
    if not shutil.which(config.CLAUDE_BIN):
        problems.append("Claude Code ('claude') isn't on your PATH; the script, fact-check, shots and metadata steps need it.")
    if not config.GOOGLE_API_KEY:
        problems.append("GOOGLE_API_KEY is empty in .env (see README step 2).")
    try:
        import PIL  # noqa: F401
    except ImportError:
        problems.append("Pillow isn't installed: pip install -r requirements.txt")
    if not s6_assemble.find_upscaler():
        notes.append("Image upscaler not installed, so videos look a little soft on big screens (README: 'Sharper images').")
    if not s5_images.style_refs():
        problems.append("assets/style-refs/ has no approved style frames yet (README: 'Pick the style frames').")
    for p in problems:
        print(f"  ✗ {p}")
    for n in notes:
        print(f"  ! {n}")
    if not problems:
        print("  ✓ Everything is set up.")
    return 1 if problems else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m pipeline", description="Who Pays Who video pipeline")
    p.add_argument("--stub", action="store_true", help="no API keys or Claude: placeholder script, audio and images")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="start a video from a topic")
    n.add_argument("topic")
    n.add_argument("--angle", help="the one original insight, calculation or comparison")
    n.add_argument("--character", help='recurring example character, e.g. "Bob, a first-time gym owner"')
    n.add_argument("--notes", help="anything else the writer should know")
    n.add_argument("--words", type=int, default=config.TARGET_WORDS)
    n.add_argument("--slug")
    n.add_argument("--run", action="store_true", help="run the whole pipeline right after")

    r = sub.add_parser("run", help="run every stage that isn't done yet")
    r.add_argument("slug")
    r.add_argument("--from", dest="from_stage", choices=STAGES, help="re-run from this stage onward")

    for s in STAGES:
        sp = sub.add_parser(s, help=f"run only the {s} stage")
        sp.add_argument("slug")
    sub.add_parser("check", help="re-check script.txt after a hand edit").add_argument("slug")
    rv = sub.add_parser("revise", help="have Claude fix what the fact-check flagged, then fact-check again")
    rv.add_argument("slug")
    rv.add_argument("--notes", help="a text file of editing notes for the writer to apply as well")
    ap = sub.add_parser("approve", help="record Tejas's approval of the exact files in the review package")
    ap.add_argument("slug")
    ap.add_argument("--publish-at", default="", help='planned publish time, e.g. "2026-10-10T15:00:00Z"')
    sub.add_parser("status", help="list videos and which stages are done")
    sub.add_parser("doctor", help="check that this Mac is set up")
    vt = sub.add_parser("voice-test", help="a short sample in several voices to choose from (about 1 cent)")
    vt.add_argument("--voices", help="comma-separated Gemini voice names, e.g. Charon,Orus")
    sub.add_parser("style-frames", help="generate 4 style frame candidates to pick from (about $0.14)")

    for sp in list(sub.choices.values()):
        sp.add_argument("--stub", action="store_true", default=argparse.SUPPRESS)
        sp.add_argument("--force", action="store_true", help="continue even though fact-check hasn't passed")
        sp.add_argument("--sync", action="store_true", help="images without batch mode: faster, twice the price")
        sp.add_argument("--no-captions", action="store_true", help="don't burn captions into the video")
        sp.add_argument("--full", action="store_true", help="fact-check everything again, not just what changed")
    a = p.parse_args(argv)

    try:
        if a.cmd == "new":
            v = cmd_new(a)
            if a.run:
                a.from_stage = None
                cmd_run(a, v)
        elif a.cmd == "run":
            cmd_run(a, Video(a.slug))
        elif a.cmd in STAGES:
            run_stage(a.cmd, Video(a.slug), a)
        elif a.cmd == "revise":
            notes = open(a.notes).read() if a.notes else ""
            s2_factcheck.revise(Video(a.slug), stub=a.stub, notes=notes)
        elif a.cmd == "check":
            s1_script.check(Video(a.slug))
        elif a.cmd == "approve":
            s9_review.approve(Video(a.slug), a.publish_at)
        elif a.cmd == "status":
            cmd_status()
        elif a.cmd == "doctor":
            return cmd_doctor()
        elif a.cmd == "voice-test":
            voices = [v.strip() for v in a.voices.split(",")] if a.voices else None
            s3_voiceover.voice_test(voices, stub=a.stub)
            log(f"Listen with: open {config.OUT / 'voice-samples'}")
            log("Pick one, then put GEMINI_TTS_VOICE=<name> in .env.")
        elif a.cmd == "style-frames":
            made = s5_images.make_style_candidates()
            log(f"Wrote {len(made)} candidates to assets/style-refs/candidates/. Move the 2 or 3 you like into assets/style-refs/.")
    except StageError as e:
        log(f"\nSTOPPED: {e}")
        return 1
    except KeyboardInterrupt:
        log("\nInterrupted. Re-run the same command to pick up where it left off.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
