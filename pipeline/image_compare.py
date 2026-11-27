"""Side-by-side picture test: the Gemini images a video already has vs the same shots drawn by OpenAI.

    python -m pipeline.image_compare <slug>              # 6 shots, OpenAI low + medium quality
    python -m pipeline.image_compare <slug> --shots 8 --qualities low
    python -m pipeline.image_compare <slug> --yes        # skip the cost question

It never touches out/<slug>/images/. Everything goes to out/_image_compare/<slug>/, and
index.html there shows each shot as Gemini | OpenAI low | OpenAI medium. It uses the exact same
prompt text the real image stage sends (scene + section background + style suffix). The folder
assets/style-refs/ may hold no approved frames; in that case the Gemini images were made without
references too, so the test is like for like. If frames are there, they are NOT sent to OpenAI,
and the page says so.

Needs OPENAI_API_KEY in .env (kept on your Mac, never in chat). Prices below are estimates from
public price trackers, checked 2026-11-27; the real bill is on your OpenAI usage page.
"""

from __future__ import annotations

import argparse
import base64
import html
import shutil
import sys
import time
from pathlib import Path

import requests

from . import config
from .common import StageError, Video, log
from . import s5_images
from .s5_images import _prompt, style_refs

API = "https://api.openai.com/v1/images/generations"
MODEL = config.env("COMPARE_OPENAI_MODEL", "gpt-image-2.5-flare")
SIZE = config.env("COMPARE_OPENAI_SIZE", "1536x864")  # 16:9, multiple of 16 on both edges
# Estimated dollars per image at about 1 megapixel (third-party trackers, not OpenAI's own table).
EST_PRICE = {"low": 0.006, "medium": 0.0132, "high": 0.053}
HARD_CAP_USD = 0.50  # the test refuses to start above this even with --yes
GEMINI_BATCH_PRICE = config.PRICE_PER_IMAGE_BATCH


def pick_shots(shots: list[dict], n: int) -> list[dict]:
    """n generated (non-card) shots spread evenly from start to end of the video."""
    pool = [s for s in shots if not s.get("card")]
    if n <= 0 or not pool:
        return []
    if n >= len(pool):
        return pool
    step = (len(pool) - 1) / (n - 1) if n > 1 else 0
    seen, out = set(), []
    for i in range(n):
        s = pool[round(i * step)]
        if s["id"] not in seen:
            seen.add(s["id"])
            out.append(s)
    return out


def estimate(n_shots: int, qualities: list[str]) -> float:
    return round(sum(EST_PRICE[q] for q in qualities) * n_shots, 4)


def request_body(prompt: str, quality: str) -> dict:
    return {"model": MODEL, "prompt": prompt, "size": SIZE, "quality": quality, "n": 1}


def generate(prompt: str, quality: str, key: str) -> bytes:
    return s5_images.openai_generate(prompt, key, quality=quality, model=MODEL, size=SIZE)


def build_page(slug: str, rows: list[dict], qualities: list[str], refs_note: str) -> str:
    cols = ["Gemini (current)"] + [f"OpenAI {q}" for q in qualities]
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = []
    for r in rows:
        cells = [f'<td><img src="{html.escape(r["gemini"])}"></td>' if r.get("gemini")
                 else "<td>(no Gemini image found)</td>"]
        for q in qualities:
            f = r.get(q)
            cells.append(f'<td><img src="{html.escape(f)}"></td>' if f else "<td>failed</td>")
        scene = html.escape(r["scene"][:220])
        body.append(f'<tr><td colspan="{len(cols)}"><b>Shot {r["id"]}</b>: {scene}</td></tr><tr>{"".join(cells)}</tr>')
    return (
        "<!doctype html><meta charset='utf-8'><title>Image test</title>"
        "<style>body{font:14px system-ui;margin:20px}table{border-collapse:collapse;width:100%}"
        "td,th{padding:4px;vertical-align:top}img{width:100%;border:1px solid #ccc}</style>"
        f"<h2>{html.escape(slug)}: Gemini vs OpenAI ({html.escape(MODEL)}, {SIZE})</h2>"
        f"<p>{html.escape(refs_note)}</p><table><tr>{head}</tr>{''.join(body)}</table>"
    )


def run(slug: str, n_shots: int = 6, qualities: list[str] | None = None, yes: bool = False) -> Path:
    qualities = qualities or ["low", "medium"]
    bad = [q for q in qualities if q not in EST_PRICE]
    if bad:
        raise StageError(f"Unknown quality {bad}. Use low, medium or high.")
    video = Video(slug)
    shots = pick_shots(video.read_json("shots.json")["shots"], n_shots)
    if not shots:
        raise StageError("No generated shots found in shots.json.")
    est = estimate(len(shots), qualities)
    log(f"  test: {len(shots)} shots x {qualities} with {MODEL} at {SIZE}")
    log(f"  estimated cost: about ${est:.2f} (hard cap ${HARD_CAP_USD:.2f}; your per-video cap is $2)")
    log(f"  for scale: the same {len(shots)} shots on Gemini cost about ${len(shots) * GEMINI_BATCH_PRICE:.2f} in batch")
    if est > HARD_CAP_USD:
        raise StageError(f"Estimate ${est:.2f} is above the ${HARD_CAP_USD:.2f} cap. Use fewer shots or qualities.")
    key = config.env("OPENAI_API_KEY")
    if not key:
        raise StageError("OPENAI_API_KEY is empty. Add the line OPENAI_API_KEY=... to .env on your Mac (never paste it in chat).")
    if not yes and input("  Spend this? [y/N] ").strip().lower() != "y":
        raise StageError("Stopped. Nothing was spent.")

    out_dir = config.OUT / "_image_compare" / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in shots:
        row = {"id": s["id"], "scene": s["scene"]}
        src = video.path("images", ".keep").parent / f"shot_{s['id']:03d}.png"
        if src.exists():
            dst = out_dir / f"shot_{s['id']:03d}_gemini.png"
            shutil.copyfile(src, dst)
            row["gemini"] = dst.name
        for q in qualities:
            dst = out_dir / f"shot_{s['id']:03d}_openai_{q}.png"
            if not dst.exists():
                try:
                    dst.write_bytes(generate(_prompt(s), q, key))
                except StageError as e:
                    log(f"    shot {s['id']} {q}: {e}")
                    continue
            row[q] = dst.name
            log(f"    shot {s['id']} {q}: done")
        rows.append(row)

    refs = style_refs()
    note = ("No approved style frames were sent to either model; the look comes from the prompt text alone."
            if not refs else f"{len(refs)} style frame(s) exist in assets/style-refs/ and went to Gemini but NOT to OpenAI, so OpenAI is at a disadvantage here.")
    page = out_dir / "index.html"
    page.write_text(build_page(slug, rows, qualities, note), encoding="utf-8")
    log(f"  open {page}")
    return page


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("slug")
    p.add_argument("--shots", type=int, default=6)
    p.add_argument("--qualities", default="low,medium")
    p.add_argument("--yes", action="store_true", help="skip the cost question")
    a = p.parse_args(argv)
    try:
        run(a.slug, a.shots, [q.strip() for q in a.qualities.split(",") if q.strip()], a.yes)
    except StageError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
