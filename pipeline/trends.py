"""Trends: what is working for similar explainer channels on YouTube, and what that suggests for ours.

python -m pipeline trends             # weekly: search, measure, compare, propose (free: YouTube Data API + your Claude plan)
python -m pipeline trends --apply 2   # apply proposal 2 from the latest report to its prompt file

1. Search YouTube (Data API v3) for videos and Shorts like ours: business and money explainers, often animated
   or stick-figure, from the last few months.
2. For every channel found, read its recent uploads, so each video can be measured against ITS OWN channel:
   an "outlier" got at least 2x its channel's median views; a "flop" got at most half. Raw views mostly
   measure channel size; the ratio measures the video.
3. Download the thumbnails of the outliers and the flops, and have Claude compare them (titles, thumbnails,
   length, topic, age). Patterns must hold for the outliers AND be missing from the flops of the same
   channels; one viral video is not a pattern.
4. Claude proposes concrete edits to our prompt files, each with its evidence and how it keeps Who Pays Who
   distinct. A proposal that would make our videos match another channel's template is flagged, not proposed:
   YouTube's inauthentic-content policy targets mass-produced, templated videos.

Nothing is changed by the report itself. The topic scout, the metadata writer and the Shorts picker read its
short "notes for writers" as background (for 21 days); a prompt file changes only with --apply N, and the
old file is kept in out/_crew/_replaced_<date>/prompts/.

The YouTube Data API key is YOUTUBE_API_KEY in .env, or else GOOGLE_API_KEY (turn on "YouTube Data API v3"
for that key's Google Cloud project). The free quota is 10,000 units a day; one run uses about 1,500.
Transcripts are not available this way, so hooks and scripts are judged from titles and thumbnails only.
"""

from __future__ import annotations

import json
import re
import shutil
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import config, llm, stubs
from .common import StageError, extract_json, fill, log, prompt, replaced_dir

API = "https://www.googleapis.com/youtube/v3"
DEFAULT_QUERIES = [
    "how does it make money explained",
    "business model explained animation",
    "who really pays for free",
    "how companies make money stick figure animation",
    "why is it so expensive explained",
    "economics explained animated",
]
SEARCH_DAYS = 150
PER_QUERY = 15
UPLOADS_PER_CHANNEL = 40
OUTLIER_RATIO, FLOP_RATIO = 2.0, 0.5
MIN_OUTLIER_VIEWS = 10_000
MAX_OUTLIERS, MAX_FLOPS = 15, 12
SHORT_MAX_S = 180
NOTES_FRESH_DAYS = 21
PROPOSAL_FILES = ["prompts/script.md", "prompts/metadata.md", "prompts/shorts.md", "prompts/shorts_question.md",
                  "prompts/topic_scout.md", "voice.md"]
UNITS = {"search": 100, "videos": 1, "channels": 1, "playlistItems": 1}


def _dir(stub: bool) -> Path:
    p = config.OUT / "_crew" / ("stub" if stub else "")
    p.mkdir(parents=True, exist_ok=True)
    return p


def api_key() -> str:
    return config.env("YOUTUBE_API_KEY") or config.GOOGLE_API_KEY


# --- YouTube Data API -------------------------------------------------------------

class YouTube:
    """A thin YouTube Data API client that counts quota units. The key never appears in logs or errors."""

    def __init__(self, key: str):
        self.key, self.units = key, 0

    def get(self, endpoint: str, **params) -> dict:
        self.units += UNITS[endpoint]
        res = requests.get(f"{API}/{endpoint}", params={**params, "key": self.key}, timeout=60)
        if res.status_code == 200:
            return res.json()
        text = res.text.replace(self.key, "<key>")[:600]
        if res.status_code == 403 and ("has not been used" in text or "disabled" in text or "blocked" in text):
            raise StageError("The YouTube Data API is not turned on for your key's Google Cloud project. Turn on "
                             "\"YouTube Data API v3\" at https://console.cloud.google.com/apis/library/youtube.googleapis.com "
                             "(same project as your key), and if the key is restricted to the Gemini API, allow YouTube "
                             "Data API v3 too. Or put a separate YOUTUBE_API_KEY in .env. Don't paste the key in chat.")
        if res.status_code == 401:
            raise StageError("YouTube refused this key (401: \"API keys are not supported by this API\"). The key in .env "
                             "is most likely a Gemini/AI Studio key, which works for Gemini but not for the YouTube Data API. "
                             "Make a plain API key once: https://console.cloud.google.com/apis/credentials, Create credentials, "
                             "API key; under API restrictions pick only \"YouTube Data API v3\". Put it in .env as "
                             "YOUTUBE_API_KEY=... (it is used instead of GOOGLE_API_KEY for trends). Don't paste it in chat.")
        if res.status_code == 403 and "quota" in text.lower():
            raise StageError("Today's free YouTube Data API quota is used up. Run it again tomorrow.")
        raise StageError(f"YouTube Data API {endpoint} returned {res.status_code}: {text}")


def _seconds(iso: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def _video_rows(items: list[dict]) -> list[dict]:
    rows = []
    for it in items:
        sn, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("maxres") or thumbs.get("high") or thumbs.get("medium") or {}).get("url", "")
        secs = _seconds(cd.get("duration", ""))
        rows.append({"id": it["id"], "title": sn.get("title", ""), "channel_id": sn.get("channelId", ""),
                     "channel": sn.get("channelTitle", ""), "published": sn.get("publishedAt", "")[:10],
                     "views": int(st.get("viewCount", 0) or 0), "likes": int(st.get("likeCount", 0) or 0),
                     "comments": int(st.get("commentCount", 0) or 0), "seconds": secs, "short": 0 < secs <= SHORT_MAX_S,
                     "thumbnail_url": thumb, "url": f"https://www.youtube.com/watch?v={it['id']}"})
    return rows


def _videos(yt: YouTube, ids: list[str]) -> list[dict]:
    out = []
    for k in range(0, len(ids), 50):
        data = yt.get("videos", part="snippet,statistics,contentDetails", id=",".join(ids[k:k + 50]), maxResults=50)
        out += _video_rows(data.get("items", []))
    return out


def collect(yt: YouTube, queries: list[str], days: int) -> dict:
    """Search results plus each channel's recent uploads, all with real view counts and lengths."""
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = []
    for q in queries:
        for duration in ("medium", "short"):  # 4-20 min explainers, and under 4 min (Shorts)
            data = yt.get("search", part="snippet", q=q, type="video", maxResults=PER_QUERY, order="viewCount",
                          publishedAfter=after, videoDuration=duration, relevanceLanguage="en")
            found += [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
    hits = _videos(yt, sorted(set(found)))
    hits = [h for h in hits if h["channel"].strip().lower() != config.CHANNEL_NAME.lower()]
    channels = sorted({h["channel_id"] for h in hits if h["channel_id"]})
    info, uploads = {}, {}
    for k in range(0, len(channels), 50):
        data = yt.get("channels", part="snippet,statistics,contentDetails", id=",".join(channels[k:k + 50]), maxResults=50)
        for c in data.get("items", []):
            info[c["id"]] = {"title": c.get("snippet", {}).get("title", ""),
                             "subscribers": int(c.get("statistics", {}).get("subscriberCount", 0) or 0),
                             "uploads": c.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")}
    for cid, c in info.items():
        if not c["uploads"]:
            continue
        data = yt.get("playlistItems", part="contentDetails", playlistId=c["uploads"], maxResults=UPLOADS_PER_CHANNEL)
        ids = [it["contentDetails"]["videoId"] for it in data.get("items", []) if it.get("contentDetails", {}).get("videoId")]
        uploads[cid] = _videos(yt, ids)
    return {"queries": queries, "days": days, "hits": hits, "channels": info, "uploads": uploads}


# --- measuring -----------------------------------------------------------------

def _age_days(published: str, today: datetime) -> int:
    try:
        return max(1, (today - datetime.strptime(published, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days)
    except ValueError:
        return 1


def measure(data: dict, today: datetime | None = None) -> dict:
    """Each video against its own channel's median (long videos and Shorts measured separately).
    Videos younger than a week are left out: their views haven't settled."""
    today = today or datetime.now(timezone.utc)
    median = {}
    for cid, vids in data["uploads"].items():
        for short in (False, True):
            views = [v["views"] for v in vids if v["short"] == short and _age_days(v["published"], today) >= 7]
            if len(views) >= 5:
                median[(cid, short)] = statistics.median(views)
    seen, rows = set(), []
    for v in data["hits"] + [v for vids in data["uploads"].values() for v in vids]:
        m = median.get((v["channel_id"], v["short"]))
        if v["id"] in seen or not m or _age_days(v["published"], today) < 7:
            continue
        seen.add(v["id"])
        subs = data["channels"].get(v["channel_id"], {}).get("subscribers", 0)
        rows.append({**v, "channel_median": int(m), "ratio": round(v["views"] / m, 2) if m else 0.0,
                     "age_days": _age_days(v["published"], today), "subscribers": subs})
    out = {}
    for short in (False, True):
        mine = [r for r in rows if r["short"] == short]
        outliers = sorted((r for r in mine if r["ratio"] >= OUTLIER_RATIO and r["views"] >= MIN_OUTLIER_VIEWS),
                          key=lambda r: -r["ratio"])[:MAX_OUTLIERS]
        out_channels = {r["channel_id"] for r in outliers}
        flops = sorted((r for r in mine if r["ratio"] <= FLOP_RATIO and r["channel_id"] in out_channels),
                       key=lambda r: r["ratio"])[:MAX_FLOPS]
        out["shorts" if short else "videos"] = {"outliers": outliers, "flops": flops, "measured": len(mine)}
    return out


# --- analysis ------------------------------------------------------------------

def _thumbs(groups: dict, folder: Path, stub: bool) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for g in groups.values():
        for r in g["outliers"] + g["flops"]:
            p = folder / f"{r['id']}.jpg"
            r["thumbnail_file"] = str(p.relative_to(folder.parent))
            if p.exists():
                continue
            if stub:
                stubs.image_card(r["title"], p.with_suffix(".png"))
                p.with_suffix(".png").rename(p)
                continue
            try:
                res = requests.get(r["thumbnail_url"], timeout=30)
                if res.status_code == 200:
                    p.write_bytes(res.content)
            except requests.RequestException:
                pass
            if not p.exists():
                r["thumbnail_file"] = ""


def _table(rows: list[dict]) -> str:
    return "\n".join(
        f"- [{r['id']}] \"{r['title']}\" ({r['channel']}, {r['subscribers']:,} subs) {r['views']:,} views = "
        f"{r['ratio']}x its channel's median; {r['seconds'] // 60}:{r['seconds'] % 60:02d} long; {r['age_days']} days old; "
        f"thumbnail: {r.get('thumbnail_file') or '(none)'}" for r in rows) or "(none)"


def _ours() -> str:
    lines = []
    for m in sorted(config.OUT.glob("*/metadata.json")):
        try:
            meta = json.loads(m.read_text())
        except ValueError:
            continue
        lines.append(f"- \"{meta.get('title', '')}\" thumbnail text: {meta.get('thumbnail_text', '')}")
    res = config.OUT / "SHORTS_RESULTS.md"
    return "\n".join(lines) + ("\n\nOur Shorts results:\n" + res.read_text() if res.exists() else "")


def _prompt_files() -> str:
    parts = []
    for rel in PROPOSAL_FILES:
        p = config.ROOT / rel
        if p.exists():
            parts.append(f"<file path=\"{rel}\">\n{p.read_text()}\n</file>")
    return "\n\n".join(parts)


def check_proposals(raw: list, root: Path | None = None) -> tuple[list[dict], list[str]]:
    """A proposal can be applied only if it edits an allowed file and its "before" text is in that file exactly
    once (so --apply is a mechanical swap), and it says how it keeps the channel distinct."""
    root = root or config.ROOT
    good, dropped = [], []
    for k, p in enumerate(raw if isinstance(raw, list) else [], start=1):
        if not isinstance(p, dict):
            continue
        f, before, after = str(p.get("file", "")).strip(), str(p.get("before", "")), str(p.get("after", ""))
        label = f"Proposal {k} ({p.get('title', '')[:50]})"
        if f not in PROPOSAL_FILES:
            dropped.append(f"{label}: {f or 'no file'} is not a file proposals may edit.")
        elif not (root / f).exists() or (root / f).read_text().count(before) != 1 or not before.strip():
            dropped.append(f"{label}: its \"before\" text is not found exactly once in {f}.")
        elif not after.strip() or after == before:
            dropped.append(f"{label}: no change.")
        elif not str(p.get("keeps_us_distinct", "")).strip():
            dropped.append(f"{label}: doesn't say how it keeps Who Pays Who distinct.")
        elif p.get("template_risk"):
            dropped.append(f"{label}: flagged as copying another channel's template ({p.get('template_risk')}).")
        else:
            good.append(p)
    return good, dropped


def run(stub: bool = False, queries: list[str] | None = None, days: int = SEARCH_DAYS) -> dict:
    queries = queries or DEFAULT_QUERIES
    folder = _dir(stub)
    stamp = datetime.now().strftime("%Y-%m-%d")
    work = folder / "trends" / stamp
    work.mkdir(parents=True, exist_ok=True)
    if stub:
        data, units = stubs.youtube_data(), 0
    else:
        key = api_key()
        if not key:
            raise StageError("No YouTube Data API key: set YOUTUBE_API_KEY (or GOOGLE_API_KEY) in .env. Don't paste it in chat.")
        yt = YouTube(key)
        log(f"[trends] searching YouTube for {len(queries)} queries (about {len(queries) * 2 * 100} quota units of 10,000 a day)...")
        data = collect(yt, queries, days)
        units = yt.units
    (work / "youtube.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    groups = measure(data)
    _thumbs(groups, work / "thumbs", stub)
    n_out = sum(len(g["outliers"]) for g in groups.values())
    log(f"[trends] {len(data['hits'])} videos found, {len(data['channels'])} channels; "
        f"{n_out} outliers and {sum(len(g['flops']) for g in groups.values())} flops to compare")
    if not n_out:
        raise StageError("No outliers found (no video beat its own channel's median by 2x). Try other --queries or more --days.")
    text = fill(prompt("trends.md"), DATE=stamp, CHANNEL=config.CHANNEL_NAME,
                VIDEO_OUTLIERS=_table(groups["videos"]["outliers"]), VIDEO_FLOPS=_table(groups["videos"]["flops"]),
                SHORT_OUTLIERS=_table(groups["shorts"]["outliers"]), SHORT_FLOPS=_table(groups["shorts"]["flops"]),
                OURS=_ours() or "(none yet)", FILES=_prompt_files(), ALLOWED=", ".join(PROPOSAL_FILES))
    answer = llm.ask(text, "trends", stub=stub, read_dir=str(work))
    (work / "answer.md").write_text(answer)
    result = extract_json(answer)
    proposals, dropped = check_proposals(result.get("proposals", []))
    report = {"date": stamp, "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "stub": stub,
              "queries": queries, "quota_units": units, "measured": {k: g["measured"] for k, g in groups.items()},
              "outliers": {k: g["outliers"] for k, g in groups.items()}, "flops": {k: g["flops"] for k, g in groups.items()},
              "patterns": result.get("patterns", []), "notes_for_writers": str(result.get("notes_for_writers", "")).strip(),
              "proposals": proposals, "dropped": dropped, "applied": []}
    for name in (f"trends_{stamp}.json", "trends_latest.json"):
        (folder / name).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    (folder / f"trends_{stamp}.md").write_text(markdown(report))
    log(f"[trends] {len(report['patterns'])} patterns, {len(proposals)} proposals ({len(dropped)} dropped). "
        f"Read out/_crew/{'stub/' if stub else ''}trends_{stamp}.md; apply one with: python -m pipeline trends --apply N")
    return report


def markdown(r: dict) -> str:
    lines = [f"# Trends: {r['date']}", "", "What beat its own channel's usual views, compared with what flopped on the same channels. "
             "Nothing here has changed any file. Apply a proposal with `python -m pipeline trends --apply N`.", "",
             f"Measured {r['measured'].get('videos', 0)} videos and {r['measured'].get('shorts', 0)} Shorts; "
             f"{r['quota_units']} YouTube quota units used.", ""]
    lines += ["## Patterns", ""]
    for p in r["patterns"]:
        lines += [f"### {p.get('pattern', '')} ({p.get('confidence', '?')})", "", f"- Seen in: {', '.join(p.get('evidence', []))}",
                  f"- Against it: {p.get('counter_evidence') or 'none found'}", f"- Applies to: {p.get('applies_to', '')}", ""]
    lines += ["## Notes the topic scout, title writer and Shorts picker will read", "", r["notes_for_writers"] or "(none)", ""]
    lines += ["## Proposed changes (you decide)", ""]
    for k, p in enumerate(r["proposals"], start=1):
        lines += [f"### {k}. {p.get('title', '')}  —  `{p['file']}`" + ("  (applied)" if k in r.get("applied", []) else ""), "",
                  f"Why: {p.get('why', '')}", "", f"Evidence: {', '.join(p.get('evidence', []))}", "",
                  f"Keeps us distinct: {p.get('keeps_us_distinct', '')}", "", "Before:", "", "```", p["before"], "```", "",
                  "After:", "", "```", p["after"], "```", "", f"Apply: `python -m pipeline trends --apply {k}`", ""]
    if r["dropped"]:
        lines += ["## Proposals dropped by the checks", ""] + [f"- {d}" for d in r["dropped"]] + [""]
    for kind in ("videos", "shorts"):
        lines += [f"## {kind.title()}: outliers", ""] + [
            f"- {o['ratio']}x: [{o['title']}]({o['url']}) ({o['channel']}, {o['views']:,} views)" for o in r["outliers"][kind]] + [""]
        lines += [f"## {kind.title()}: flops on the same channels", ""] + [
            f"- {o['ratio']}x: [{o['title']}]({o['url']}) ({o['channel']}, {o['views']:,} views)" for o in r["flops"][kind]] + [""]
    return "\n".join(lines)


def latest(stub: bool = False) -> dict | None:
    p = _dir(stub) / "trends_latest.json"
    return json.loads(p.read_text()) if p.exists() else None


def writer_notes(stub: bool = False) -> str:
    """The latest report's short notes for writers, if it is fresh enough to still describe what is working."""
    r = latest(stub)
    if not r or not r.get("notes_for_writers"):
        return "(no recent trends report)"
    try:
        when = datetime.strptime(r["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return "(no recent trends report)"
    if datetime.now(timezone.utc) - when > timedelta(days=NOTES_FRESH_DAYS):
        return "(the last trends report is too old to use; run python -m pipeline trends)"
    return f"(from the trends report of {r['date']}; patterns are evidence, not rules: our voice profile and rules win)\n" \
           + r["notes_for_writers"]


def apply(n: int, stub: bool = False) -> Path:
    """Apply proposal n of the latest report to its file. The old file is kept in out/_crew/_replaced_<date>/."""
    r = latest(stub)
    if not r:
        raise StageError("No trends report yet. Run: python -m pipeline trends")
    if not 1 <= n <= len(r["proposals"]):
        raise StageError(f"The latest report ({r['date']}) has {len(r['proposals'])} proposals; there is no {n}.")
    p = r["proposals"][n - 1]
    path = config.ROOT / p["file"]
    text = path.read_text()
    if text.count(p["before"]) != 1:
        raise StageError(f"{p['file']} has changed since the report: the text proposal {n} replaces is no longer there "
                         "exactly once. Nothing was changed. Edit the file by hand, or run trends again.")
    keep = replaced_dir(_dir(stub)) / p["file"]
    keep.parent.mkdir(parents=True, exist_ok=True)
    k = 2
    while keep.exists():
        keep = keep.with_name(f"{Path(p['file']).stem}.{k}{Path(p['file']).suffix}")
        k += 1
    shutil.copy2(path, keep)
    path.write_text(text.replace(p["before"], p["after"], 1))
    r["applied"] = sorted(set(r.get("applied", []) + [n]))
    for name in (f"trends_{r['date']}.json", "trends_latest.json"):
        (_dir(stub) / name).write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n")
    (_dir(stub) / f"trends_{r['date']}.md").write_text(markdown(r))
    log(f"  applied proposal {n} to {p['file']}; the old version is in {keep.relative_to(config.ROOT) if keep.is_relative_to(config.ROOT) else keep}.")
    log(f"  Check it with: git diff {p['file']}   (commit it yourself if you keep it)")
    return keep
