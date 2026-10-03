"""Stage 5: one image per shot with Gemini 3.1 Flash Lite Image.

The 2 or 3 approved frames in assets/style-refs/ go along with every request so
all images share one look. Batch mode costs half of normal requests; results
usually arrive within minutes but Google allows up to 24 hours. Re-running the
stage resumes waiting on submitted batches and only generates missing images.
Writes images/shot_NNN.png.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import requests

from . import config, stubs
from .common import StageError, Video, log

API = "https://generativelanguage.googleapis.com"
REF_INSTRUCTION = (
    "The attached images are style references for the YouTube channel Who Pays Who. "
    "Match their drawing style exactly: line weight, flat colors, character design, lettering. "
    "Do not copy their content. Draw this new scene:\n\n"
)


def _headers(extra: dict | None = None) -> dict:
    h = {"x-goog-api-key": config.GOOGLE_API_KEY}
    h.update(extra or {})
    return h


def _check(res: requests.Response, what: str) -> dict:
    if res.status_code != 200:
        raise StageError(f"Gemini {what} returned {res.status_code}: {res.text[:500]}")
    return res.json()


def style_refs() -> list[Path]:
    return sorted(p for p in config.STYLE_REFS.glob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))[:3]


def upload_file(path: Path) -> dict:
    """Upload a file to the Gemini Files API (kept by Google for 48 hours)."""
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}[path.suffix.lower()[1:]]
    data = path.read_bytes()
    start = requests.post(
        f"{API}/upload/v1beta/files",
        headers=_headers({
            "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(data)), "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        }),
        json={"file": {"display_name": path.name}}, timeout=60,
    )
    if start.status_code != 200 or "X-Goog-Upload-URL" not in start.headers:
        raise StageError(f"Uploading {path.name} failed: {start.status_code} {start.text[:300]}")
    done = requests.post(start.headers["X-Goog-Upload-URL"], data=data, timeout=120, headers={
        "Content-Length": str(len(data)), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"})
    f = _check(done, "file upload")["file"]
    return {"file_data": {"mime_type": f.get("mimeType", mime), "file_uri": f["uri"]}}


def build_request(prompt_text: str, ref_parts: list[dict]) -> dict:
    text = (REF_INSTRUCTION if ref_parts else "") + prompt_text
    return {
        "contents": [{"role": "user", "parts": [*ref_parts, {"text": text}]}],
        "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "16:9"}},
    }


def image_from_response(resp: dict) -> bytes | None:
    for cand in resp.get("candidates", []):
        for part in (cand.get("content") or {}).get("parts", []):
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"])
    return None


def generate_sync(request: dict) -> bytes | None:
    for attempt in range(4):
        res = requests.post(f"{API}/v1beta/models/{config.IMAGE_MODEL}:generateContent",
                            headers=_headers(), json=request, timeout=180)
        if res.status_code in (429, 500, 503) and attempt < 3:
            time.sleep(2 ** (attempt + 2))
            continue
        return image_from_response(_check(res, "image request"))
    return None


def _submit_batch(name: str, keyed: list[tuple[str, dict]]) -> str:
    body = {"batch": {"display_name": name, "input_config": {"requests": {"requests": [
        {"request": req, "metadata": {"key": key}} for key, req in keyed]}}}}
    res = requests.post(f"{API}/v1beta/models/{config.IMAGE_MODEL}:batchGenerateContent",
                        headers=_headers(), json=body, timeout=120)
    return _check(res, "batch submit")["name"]


def _batch_results(op: dict, keys: list[str]) -> dict[str, dict]:
    resp = op.get("response") or {}
    out = {}
    inline = (resp.get("inlinedResponses") or {}).get("inlinedResponses")
    if inline is not None:
        for n, item in enumerate(inline):
            key = (item.get("metadata") or {}).get("key") or (keys[n] if n < len(keys) else str(n))
            out[key] = item.get("response") or {}
    elif resp.get("responsesFile"):
        res = requests.get(f"{API}/download/v1beta/{resp['responsesFile']}:download?alt=media", headers=_headers(), timeout=300)
        for line in res.text.splitlines():
            if line.strip():
                item = json.loads(line)
                out[item.get("key", "")] = item.get("response") or {}
    return out


def run(video: Video, stub: bool = False, sync: bool = False) -> None:
    shots = video.read_json("shots.json")["shots"]
    out_dir = video.path("images", ".keep").parent
    todo = [s for s in shots if not (out_dir / f"shot_{s['id']:03d}.png").exists()]
    log(f"  images: {len(shots) - len(todo)} done, {len(todo)} to generate")
    if not todo:
        return

    if stub:
        for s in todo:
            stubs.image_card(s["scene"], out_dir / f"shot_{s['id']:03d}.png", label=f"#{s['id']}")
        return
    if not config.GOOGLE_API_KEY:
        raise StageError("GOOGLE_API_KEY is empty. Put it in the .env file (see README), or run with --stub.")

    refs = style_refs()
    if not refs:
        log("  WARNING: assets/style-refs/ is empty, so images may drift in style. See README: 'Pick the style frames'.")
    ref_parts = [upload_file(p) for p in refs]

    if sync:
        failed = []
        for n, s in enumerate(todo, 1):
            img = generate_sync(build_request(s["prompt"], ref_parts))
            if img:
                (out_dir / f"shot_{s['id']:03d}.png").write_bytes(img)
            else:
                failed.append(s["id"])
            log(f"    {n}/{len(todo)}")
        _report_failures(video, failed)
        return

    state_file = out_dir / "batches.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {"batches": []}
    pending_keys = {k for b in state["batches"] if not b.get("done") for k in b["keys"]}
    fresh = [s for s in todo if f"shot_{s['id']:03d}" not in pending_keys]
    for i in range(0, len(fresh), config.IMAGE_BATCH_SIZE):
        chunk = fresh[i:i + config.IMAGE_BATCH_SIZE]
        keyed = [(f"shot_{s['id']:03d}", build_request(s["prompt"], ref_parts)) for s in chunk]
        name = _submit_batch(f"{video.slug}-{i // config.IMAGE_BATCH_SIZE + 1}", keyed)
        state["batches"].append({"name": name, "keys": [k for k, _ in keyed]})
        state_file.write_text(json.dumps(state, indent=2))
        log(f"    submitted {name} ({len(chunk)} images)")

    failed = []
    while True:
        open_batches = [b for b in state["batches"] if not b.get("done")]
        if not open_batches:
            break
        for b in open_batches:
            op = _check(requests.get(f"{API}/v1beta/{b['name']}", headers=_headers(), timeout=60), "batch status")
            st = (op.get("metadata") or {}).get("state", "")
            if st.endswith("SUCCEEDED"):
                results = _batch_results(op, b["keys"])
                for key in b["keys"]:
                    img = image_from_response(results.get(key, {}))
                    if img:
                        (out_dir / f"{key}.png").write_bytes(img)
                    else:
                        failed.append(int(key.split("_")[1]))
                b["done"] = True
            elif st.endswith(("FAILED", "CANCELLED", "EXPIRED")):
                b["done"] = True
                failed += [int(k.split("_")[1]) for k in b["keys"]]
                log(f"    {b['name']} ended as {st}")
        state_file.write_text(json.dumps(state, indent=2))
        if any(not b.get("done") for b in state["batches"]):
            log("    waiting on Gemini batch jobs (checking every 30s; Ctrl+C is safe, re-run to resume)...")
            time.sleep(30)
    _report_failures(video, failed)


def _report_failures(video: Video, failed: list[int]) -> None:
    if failed:
        raise StageError(
            f"{len(failed)} images didn't come back (shots {sorted(failed)[:15]}). "
            f"Re-run 'python -m pipeline images {video.slug}' to retry just those, or edit their scene in shots.json first."
        )


def make_style_candidates(count: int = 4) -> list[Path]:
    """Generate a few style frames for Tejas to choose from (normal-price requests)."""
    if not config.GOOGLE_API_KEY:
        raise StageError("GOOGLE_API_KEY is empty. Put it in the .env file first (see README).")
    scenes = [
        "A busy store floor with stick-figure customers queuing at a till; a large price tag reads \"$4.99\" and a sign on the wall reads \"RENT $12,000\".",
        "A stick-figure owner in a green cap stands beside a whiteboard showing a simple bar chart labeled \"COSTS\" in red and \"PROFIT\" in green.",
        "A warehouse loading dock with stick-figure workers moving boxes onto a truck; a big label on the truck reads \"SHIPPING\".",
        "A split scene: on the left a stick figure paying at a counter, on the right the same money flowing as arrows to a landlord, a supplier and a bank, each labeled.",
    ]
    dest = config.STYLE_REFS / "candidates"
    dest.mkdir(parents=True, exist_ok=True)
    made = []
    for n, scene in enumerate(scenes[:count], 1):
        img = generate_sync(build_request(f"{scene}\n\n{config.IMAGE_STYLE_SUFFIX}", []))
        if img:
            p = dest / f"candidate_{n}.png"
            p.write_bytes(img)
            made.append(p)
    return made
