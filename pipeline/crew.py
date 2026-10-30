"""The crew: one command from a topic to a GO / NO-GO recommendation for the video and each Short.

python -m pipeline crew [--topic "..." --angle "..."] [--slug <slug>] [--pick-top] [--from-agent <name>] [--yes]
python -m pipeline shorts-results <slug> <n>

Agents, in order. Each writes out/<slug>/agents/<name>.md (to read) and <name>.json (for the overseer):

  topic_scout       NEW. 3 to 5 topics, each with demand evidence from the web, one angle and at least 3
                    primary sources. Skips topics already in out/. --pick-top takes the first; otherwise you pick.
  script_factcheck  stages script + factcheck; a failed fact-check is revised up to 3 times.
  voice_captions    stage voice, then caption timing and garbled speech are checked against what a speech
                    recognizer hears; flagged lines are re-recorded (at most 2 retries).
  images            stages shots + images, then Claude checks every picture; flagged pictures are redrawn
                    (at most 2 retries).
  packaging         stages assemble, metadata, thumbnail, qc and review; claim fixes the quality check
                    suggests for the title, thumbnail text or description are applied once.
  shorts            the Shorts command, plus: a rejected Short is chosen again (at most 2 times, never reusing
                    another Short's sentences), a Short QC (length, captions incl. the spoken question, loudness,
                    a frame sheet Claude checks), one rebuild at most, and upload.md for each Short that passed.
  overseer          NEW. A fresh Claude call that saw none of the above reads every report, re-checks the riskiest
                    claims at their sources, and writes agents/VERDICT.md.

Finished agents skip themselves on a re-run; --from-agent <name> runs that agent and every one after it again.
A re-run re-checks and fixes; it never rewrites a script or redraws pictures that exist and passed.

Hard rules: no agent approves, uploads or posts anything (the overseer only recommends). Nothing is deleted:
replaced files move to out/<slug>/_replaced_<date>/. Money is only spent after the cost estimate is shown and
confirmed (or --yes).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import (align, config, llm, s1_script, s2_factcheck, s3_voiceover, s4_shots, s5_images, s6_assemble, s7_metadata,
               s8_thumbnail, s9_review, s10_qc, s11_shorts, trends)
from .common import StageError, Video, extract_json, fill, fmt_ts, log, prompt, retire, slugify, word_count

AGENTS = ["topic_scout", "script_factcheck", "voice_captions", "images", "packaging", "shorts", "overseer"]
STOPPING = {"script_factcheck", "voice_captions", "images", "packaging"}  # these stop the crew when they fail

MAX_REVISE_ROUNDS = 3
MAX_VOICE_RETRIES = 2
MAX_VOICE_LINES_PER_RETRY = 3    # more flagged sentences than this is not a spot problem: report it, don't re-record
MAX_IMAGE_RETRIES = 2
MAX_IMAGES_PER_RETRY = 12        # likewise for pictures
MAX_REPICKS = 2
MAX_SHORT_REBUILDS = 1
MAX_CLAIM_FIX_ROUNDS = 2         # the quality check often finds a new line to flag after each fix
SCOUT_MIN, SCOUT_MAX = 3, 5
SCOUT_MIN_SOURCES = 3
WORDS_PER_MINUTE = 180           # the narrator's measured pace
CHARS_PER_SECOND = 14
SHEET_FRAMES, SHEET_COLS, FRAME_W, FRAME_H = 8, 4, 270, 480


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- reports --------------------------------------------------------------------

class Report:
    """What one agent checked, fixed and could not fix. Written to agents/<name>.md and .json."""

    def __init__(self, agent: str, slug: str):
        self.agent, self.slug = agent, slug
        self.passed = True
        self.checked: list[str] = []
        self.fixed: list[str] = []
        self.could_not_fix: list[str] = []
        self.warnings: list[str] = []
        self.retries = 0
        self.cost_usd = 0.0
        self.details: dict = {}

    def fail(self, reason: str) -> None:
        self.passed = False
        self.could_not_fix.append(reason.strip())

    def spend(self, usd: float, what: str) -> None:
        self.cost_usd += usd
        if usd > 0:
            log(f"  spending about ${usd:.2f}: {what}")

    def data(self) -> dict:
        return {"agent": self.agent, "slug": self.slug, "timestamp": _now(), "passed": self.passed,
                "checked": self.checked, "fixed": self.fixed, "could_not_fix": self.could_not_fix,
                "warnings": self.warnings, "retries_used": self.retries, "cost_usd": round(self.cost_usd, 4),
                "details": self.details}

    def markdown(self) -> str:
        lines = [f"# Agent: {self.agent}", "", f"Video: {self.slug}  ", f"Result: **{'PASSED' if self.passed else 'STOPPED'}**  ",
                 f"Retries used: {self.retries}  ", f"Spent: about ${self.cost_usd:.2f}", ""]
        for title, items in (("Could not fix", self.could_not_fix), ("Fixed", self.fixed), ("Checked", self.checked),
                             ("Worth a look", self.warnings)):
            if items:
                lines += [f"## {title}", ""] + [f"- {x}" for x in items] + [""]
        for title, body in self.details.get("sections", {}).items():
            lines += [f"## {title}", "", body.strip(), ""]
        return "\n".join(lines) + "\n"

    def write_to(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{self.agent}.json").write_text(json.dumps(self.data(), indent=2, ensure_ascii=False) + "\n")
        (folder / f"{self.agent}.md").write_text(self.markdown())

    def write(self, v: Video) -> None:
        self.write_to(v.dir / "agents")


def read_report(v: Video, name: str) -> dict | None:
    p = v.dir / "agents" / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _factcheck_passed(v: Video) -> bool:
    p = v.dir / "factcheck.json"
    return p.exists() and bool(json.loads(p.read_text()).get("passed"))


def _qc_passed(v: Video) -> bool:
    p = v.dir / "qc.json"
    return p.exists() and bool(json.loads(p.read_text()).get("passed"))


def agent_done(v: Video, name: str) -> bool:
    """A finished agent skips itself: its report passed and what it made is still there."""
    from . import cli
    r = read_report(v, name)
    if not r or not r.get("passed"):
        return False
    if name == "script_factcheck":
        return cli._done(v, "factcheck")
    if name == "voice_captions":
        return (v.dir / "timeline.json").exists()
    if name == "images":
        return cli._done(v, "images")
    if name == "packaging":  # the quality check passed on exactly this video.mp4
        return (v.dir / "video.mp4").exists() and _qc_passed(v) and r.get("details", {}).get("video") == s10_qc._video_id(v)
    if name == "shorts":
        return (v.dir / "shorts" / "plan.json").exists()
    return False  # the overseer always looks again


# --- cost ---------------------------------------------------------------------------

def _estimated_shots(seconds: float) -> int:
    fast = min(config.FAST_START_SECONDS, seconds)
    return math.ceil(fast / max(config.FAST_START_SHOT_SECONDS, 0.1) + max(0.0, seconds - fast) / config.MIN_SHOT_SECONDS)


def estimate(v: Video, a) -> list[tuple[str, float, float]]:
    """(what, dollars now, dollars more at most if every retry is used) for everything still to be paid for.
    Claude calls run on your Claude plan and cost nothing extra."""
    if a.stub:
        return []
    d = v.dir
    provider = config.TTS_PROVIDER
    if (d / "script.txt").exists():
        script = (d / "script.txt").read_text()
        words, chars = word_count(script), len(script)
    else:
        words = int(v.brief().get("words") or config.TARGET_WORDS)
        chars = words * 6
    tl = json.loads((d / "timeline.json").read_text()) if (d / "timeline.json").exists() else None
    seconds = tl["duration"] if tl else words / WORDS_PER_MINUTE * 60
    rows = []
    chunk_s = (config.GEMINI_TTS_CHUNK_CHARS if provider == "gemini" else 120) / CHARS_PER_SECOND
    voice_retry = config.voice_cost(provider, MAX_VOICE_RETRIES * MAX_VOICE_LINES_PER_RETRY * chunk_s,
                                    int(MAX_VOICE_RETRIES * MAX_VOICE_LINES_PER_RETRY * chunk_s * CHARS_PER_SECOND))
    rows.append((f"Voice ({provider}, about {seconds / 60:.0f} min)" + (" — already recorded" if tl else ""),
                 0.0 if tl else config.voice_cost(provider, seconds, chars), voice_retry))
    price = config.PRICE_PER_IMAGE_SYNC if a.sync else config.PRICE_PER_IMAGE_BATCH
    if (d / "shots.json").exists():
        shots = json.loads((d / "shots.json").read_text())["shots"]
        missing = sum(not (d / "images" / f"shot_{s['id']:03d}.png").exists() for s in shots)
        label = f"Pictures ({missing} of {len(shots)} still to draw)"
    else:
        missing = _estimated_shots(seconds)
        label = f"Pictures (about {missing}, {'normal' if a.sync else 'batch'} price)"
    rows.append((label, missing * price, MAX_IMAGE_RETRIES * MAX_IMAGES_PER_RETRY * price))
    rows.append(("Thumbnail art (1 picture)", 0.0 if (d / "thumbnail_art.png").exists() else config.PRICE_PER_IMAGE_SYNC, 0.0))
    q = config.voice_cost(provider, 5.0, 80)
    rows.append((f"Shorts: {a.count} spoken opening questions", a.count * q,
                 a.count * (MAX_REPICKS + MAX_SHORT_REBUILDS) * q))
    return rows


def confirm_cost(rows: list[tuple[str, float, float]], a) -> None:
    """Print the estimate before anything that costs money, and ask (unless --yes)."""
    if a.stub:
        log("\nCost: $0 (stub run: placeholder voice and pictures, no API calls).")
        return
    now, more = sum(r[1] for r in rows), sum(r[2] for r in rows)
    log("\nCost estimate for this run (Claude steps use your Claude plan: $0 extra):")
    for what, usd, retry in rows:
        log(f"  {what:55s} ${usd:5.2f}" + (f"   (+ up to ${retry:.2f} if retries are needed)" if retry else ""))
    log(f"  {'Total now':55s} ${now:5.2f}   (at most ${now + more:.2f} if every retry is used)")
    if now + more < 0.005:
        return
    if a.yes:
        log("  --yes: going ahead.")
        return
    if not sys.stdin.isatty():
        raise StageError("This run spends money and there is no one to ask. Re-run with --yes to go ahead.")
    if input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
        raise StageError("Stopped before spending anything.")


# --- 1. topic scout (new) ---------------------------------------------------------

def _taken() -> tuple[set[str], list[str]]:
    slugs, lines = set(), []
    if config.OUT.exists():
        for d in sorted(config.OUT.iterdir()):
            b = d / "brief.json"
            if b.exists():
                slugs.add(d.name)
                slugs.add(slugify(json.loads(b.read_text()).get("topic", "")))
                lines.append(f"- {json.loads(b.read_text()).get('topic', d.name)}")
    for n in sorted((config.ROOT / "notes").glob("*.md")):
        lines.append(f"- {n.stem.replace('-', ' ')} (research notes exist)")
    return slugs, lines


def _scout_dir(stub: bool = False) -> Path:
    """out/_crew/ holds every topic list (stub runs use out/_crew/stub/, so a test never touches the real ones)."""
    p = config.OUT / "_crew" / ("stub" if stub else "")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _recent_proposals(weeks: int = 8) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    out = []
    for f in sorted(_scout_dir().glob("scout_*.json")):
        try:
            data = json.loads(f.read_text())
            when = datetime.strptime(data["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        if when >= cutoff:
            out += [f"- {t['topic']} (proposed {data['timestamp'][:10]})" for t in data.get("details", {}).get("topics", [])]
    return out


def check_topics(data, taken: set[str]) -> tuple[list[dict], list[str]]:
    from .cli import PLACEHOLDER
    good, problems, seen = [], [], set()
    raw = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return [], ['The answer has no "topics" list.']
    for k, t in enumerate(raw, start=1):
        topic = " ".join(str(t.get("topic", "")).split()) if isinstance(t, dict) else ""
        label = f"Topic {k} ({topic[:50] or 'no name'})"
        if not topic or PLACEHOLDER.search(topic):
            problems.append(f"{label}: no real topic.")
            continue
        slug = slugify(topic)
        if slug in taken or slug in seen:
            problems.append(f"{label}: already made or proposed twice.")
            continue
        if not str(t.get("angle", "")).strip():
            problems.append(f"{label}: no angle.")
            continue
        srcs = [s for s in t.get("sources", []) if isinstance(s, dict) and str(s.get("url", "")).startswith(("http://", "https://"))]
        if len(srcs) < SCOUT_MIN_SOURCES:
            problems.append(f"{label}: only {len(srcs)} primary sources with a link; at least {SCOUT_MIN_SOURCES} are needed.")
            continue
        seen.add(slug)
        good.append({**t, "topic": topic, "slug": slug, "sources": srcs})
    return good[:SCOUT_MAX], problems


def scout(a) -> tuple[Report, list[dict]]:
    rep = Report("topic_scout", "")
    taken, made = _taken()
    text = fill(prompt("topic_scout.md"), DATE=datetime.now().strftime("%d %B %Y"), COUNT=str(SCOUT_MAX),
                EXISTING="\n".join(made) or "(none yet)", RECENT="\n".join(_recent_proposals()) or "(none)",
                TRENDS=trends.writer_notes(a.stub))
    extra, topics, problems = "", [], []
    for attempt in (1, 2):
        answer = llm.ask(text + extra, "topic_scout", web=True, stub=a.stub)
        topics, problems = check_topics(extract_json(answer), taken)
        if len(topics) >= SCOUT_MIN:
            break
        log("  the topic list had problems:\n    " + "\n    ".join(problems))
        extra = "\n\nYour previous answer had these problems. Fix every one:\n- " + "\n- ".join(problems) + "\n"
    rep.checked += [f"{len(made)} topics already made were excluded", "web search only (vidIQ has no credits until 5 Nov 2026)",
                    f"each topic needs at least {SCOUT_MIN_SOURCES} primary sources with links"]
    rep.warnings += problems
    if not topics:
        rep.fail("No usable topic: " + "; ".join(problems))
    elif len(topics) < SCOUT_MIN:
        rep.warnings.append(f"Only {len(topics)} usable topics (wanted {SCOUT_MIN} to {SCOUT_MAX}).")
    rep.details["topics"] = topics
    body = []
    for k, t in enumerate(topics, start=1):
        body += [f"### {k}. {t['topic']}", "", f"Angle: {t['angle']}", "", f"Demand: {t.get('demand', 'not measured')}", ""]
        for c in t.get("competition", []) or []:
            body.append(f"- Competing video: {c.get('title', '')} ({c.get('channel', '')}, {c.get('views', '?')} views). "
                        f"Ours differs: {c.get('how_we_differ', '')}")
        body += ["", "Primary sources:"] + [f"- {s.get('title', '')} ({s.get('kind', '')}): {s['url']} — {s.get('supports', '')}"
                                             for s in t["sources"]]
        body += ["", f"Why now: {t.get('why_now', '')}", "",
                 f"Command: `python -m pipeline crew --topic \"{t['topic']}\" --angle \"{t['angle']}\"`", ""]
    rep.details["sections"] = {"Topics": "\n".join(body) or "(none)"}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = _scout_dir(a.stub)
    rep.write_to(folder)
    for ext in ("json", "md"):  # keep every proposal list, for the 8-week rule
        (folder / f"scout_{stamp}.{ext}").write_text((folder / f"topic_scout.{ext}").read_text())
    return rep, topics


def _scout_notes(t: dict) -> str:
    lines = ["Primary sources the topic scout found (open and check each one yourself before you use it):"]
    lines += [f"- {s.get('title', '')} ({s.get('kind', '')}): {s['url']} — {s.get('supports', '')}" for s in t["sources"]]
    if t.get("demand"):
        lines += ["", f"Why viewers want this now: {t['demand']}"]
    return "\n".join(lines)


def start(a) -> Video | None:
    """Resume a video, start one from --topic, or run the topic scout and start the one picked."""
    from . import cli
    slug = a.slug or (slugify(a.topic) if a.topic else None)
    if slug and (config.OUT / slug / "brief.json").exists():
        log(f"Resuming '{slug}' (finished agents are skipped).")
        return Video(slug)
    if a.topic:
        v = cli.cmd_new(argparse.Namespace(topic=a.topic, angle=a.angle, character=a.character, notes=a.notes,
                                           words=a.words, slug=slug))
        rep = Report("topic_scout", v.slug)
        rep.checked.append("Skipped: you gave the topic (--topic).")
        rep.write(v)
        return v
    log("[topic_scout] looking for topics on the web...")
    rep, topics = scout(a)
    if not topics:
        raise StageError("The topic scout found no usable topic. See out/_crew/topic_scout.md.")
    log("\nTopics (best first):")
    for k, t in enumerate(topics, start=1):
        log(f"  {k}. {t['topic']}\n     angle: {t['angle']}\n     sources: {len(t['sources'])} primary")
    if a.pick_top:
        pick = 1
    elif sys.stdin.isatty():
        ans = input(f"Pick a topic, 1 to {len(topics)} (Enter to stop here): ").strip()
        if not ans:
            log("Stopped. The topics are in out/_crew/topic_scout.md, each with its command.")
            return None
        if not ans.isdigit() or not 1 <= int(ans) <= len(topics):
            raise StageError(f"'{ans}' is not a topic number.")
        pick = int(ans)
    else:
        log("\nNo one to ask. Start one with its command in out/_crew/topic_scout.md, or re-run with --pick-top.")
        return None
    t = topics[pick - 1]
    notes = "\n\n".join(x for x in (a.notes or "", _scout_notes(t)) if x)
    v = cli.cmd_new(argparse.Namespace(topic=t["topic"], angle=t["angle"], character=a.character, notes=notes,
                                       words=a.words, slug=a.slug or t["slug"]))
    rep.slug = v.slug
    rep.details["picked"] = pick
    rep.checked.append(f"Picked topic {pick}: {t['topic']}" + (" (--pick-top)" if a.pick_top else ""))
    rep.write(v)
    return v


# --- 2. script and fact-check ----------------------------------------------------------

def agent_script_factcheck(v: Video, a, rep: Report, forced: bool) -> None:
    if (v.dir / "script.txt").exists():
        rep.checked.append("script.txt already written; kept as it is")
    else:
        s1_script.run(v, stub=a.stub)
        rep.checked.append("wrote script.txt, facts.txt and sources.txt")
    err = ""
    if forced or not _factcheck_passed(v):
        try:
            s2_factcheck.run(v, stub=a.stub)
        except StageError as e:
            err = str(e)
    rounds = 0
    while not _factcheck_passed(v) and rounds < MAX_REVISE_ROUNDS and (v.dir / "factcheck.json").exists():
        rounds += 1
        log(f"\n[script_factcheck] fact-check failed; revision {rounds} of {MAX_REVISE_ROUNDS}")
        try:
            s2_factcheck.revise(v, stub=a.stub)
        except StageError as e:
            err = str(e)
        if (v.dir / "changes.md").exists():
            rep.fixed.append(f"Revision {rounds}: " + " / ".join(
                ln.strip() for ln in (v.dir / "changes.md").read_text().splitlines()[1:] if ln.strip())[:600])
    rep.retries = rounds
    if not (v.dir / "factcheck.json").exists():
        rep.fail(f"The fact-check could not run: {err.splitlines()[0] if err else 'unknown error'}")
        return
    fc = v.read_json("factcheck.json")
    counts = {}
    for f in fc.get("facts", []):
        counts[f.get("verdict", "?")] = counts.get(f.get("verdict", "?"), 0) + 1
    rep.checked.append(f"{len(fc.get('facts', []))} facts: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    audit = fc.get("audit") or {}
    if audit:
        rep.checked.append(f"independent audit: {len(audit.get('checks', []))} items, {len(audit.get('problems', []))} problems")
    rep.details["factcheck_passed"] = fc.get("passed", False)
    if not fc.get("passed"):
        rep.fail(f"The fact-check still fails after {rounds} revisions. What is wrong (see factcheck.md):\n"
                 + s2_factcheck.problems_text(v)[:3000]
                 + f"\nFix script.txt and facts.txt, or run: python -m pipeline revise {v.slug} --notes <file>")


# --- 3. voice and captions ----------------------------------------------------------------

def caption_check(v: Video, stub: bool, wav: Path | None = None) -> dict:
    """Caption lines (built from the timeline exactly as the video's captions are) against what a speech
    recognizer hears in the voiceover. Flags lines that are far off and runs of words never heard."""
    tl = v.read_json("timeline.json")
    if stub or tl.get("voice") == "stub":
        return {"checked": False, "why": "stub run: the placeholder voice has no words to hear"}
    if not align.available():
        return {"checked": False, "why": "faster-whisper isn't installed (pip install -r requirements.txt)"}
    srt = v.path("agents", "work", "voice_captions.srt")
    s6_assemble.write_srt(tl["sentences"], srt)
    try:
        heard = align.transcribe(wav or v.dir / "voiceover.wav")
    except Exception as e:  # a model download problem must not stop the video; it is reported instead
        return {"checked": False, "why": f"the speech recognizer failed ({e})"}
    cues = s10_qc._srt_cues(srt.read_text())
    r = s10_qc.caption_drift(cues, heard)
    late = [d for d in r["drifts"] if abs(d["drift"]) > s10_qc.DRIFT_WARN_S]
    bad = [d for d in late if abs(d["drift"]) > s10_qc.DRIFT_FAIL_S]
    share = len(late) > s10_qc.DRIFT_FAIL_SHARE * max(len(r["drifts"]), 1)
    failed = bool(bad) or share or r["matched"] < 0.8
    times = [d["at"] for d in (late if share else bad)] + [m["at"] for m in r["missing"]]
    sents = tl["sentences"]
    flagged = sorted({next((s["i"] for s in sents if s["start"] <= t < s["end"]), sents[-1]["i"]) for t in times})
    return {"checked": True, "failed": failed, "lines": len(r["drifts"]), "late": late, "bad": bad, "missing": r["missing"],
            "matched": r["matched"], "flagged": flagged}


def _describe(res: dict) -> str:
    if not res.get("checked"):
        return f"not checked: {res['why']}"
    parts = [f"{res['lines']} caption lines, {res['matched']:.0%} of words heard, {len(res['late'])} off by more than "
             f"{s10_qc.DRIFT_WARN_S}s, {len(res['missing'])} possibly garbled passages"]
    parts += [f"{fmt_ts(d['at'])} \"{d['text'][:40]}\" off by {abs(d['drift']):.1f}s" for d in res["bad"][:5]]
    parts += [f"{fmt_ts(m['at'])} maybe garbled: \"{m['text'][:60]}\"" for m in res["missing"][:5]]
    return "; ".join(parts)


def agent_voice_captions(v: Video, a, rep: Report, forced: bool) -> None:
    s3_voiceover.run(v, stub=a.stub)
    tl = v.read_json("timeline.json")
    rep.spend(config.voice_cost(tl.get("provider", ""), tl["duration"] * tl.get("new_characters", 0) / max(tl["characters"], 1),
                                tl.get("new_characters", 0)), f"voice for {tl.get('new_characters', 0):,} new characters")
    res = caption_check(v, a.stub)
    rep.checked.append("caption timing and garbled speech: " + _describe(res))
    for attempt in range(1, MAX_VOICE_RETRIES + 1):
        if not res.get("checked") or not res["flagged"]:
            break
        ids = res["flagged"]
        if len(ids) > MAX_VOICE_LINES_PER_RETRY:
            rep.warnings.append(f"{len(ids)} sentences flagged at once; that is not a spot problem, so nothing was re-recorded.")
            break
        by_i = {s["i"]: s for s in tl["sentences"]}
        moved = sum(s3_voiceover.redo(v, by_i[i]["text"], stub=a.stub) for i in ids)
        if not moved:
            rep.warnings.append("The flagged lines had no recorded part to replace.")
            break
        rep.retries = attempt
        log(f"[voice_captions] re-recording {moved} part(s) of the voiceover (retry {attempt} of {MAX_VOICE_RETRIES})")
        s3_voiceover.run(v, stub=a.stub)
        tl = v.read_json("timeline.json")
        rep.spend(config.voice_cost(tl.get("provider", ""), tl.get("new_characters", 0) / CHARS_PER_SECOND,
                                    tl.get("new_characters", 0)), f"re-recording {moved} part(s)")
        rep.fixed.append(f"Retry {attempt}: re-recorded the part(s) containing sentence(s) {ids}: "
                         + " | ".join(f"\"{by_i[i]['text'][:60]}\"" for i in ids) + " (old recordings in _replaced_<date>/audio/)")
        res = caption_check(v, a.stub)
        rep.checked.append(f"after retry {attempt}: " + _describe(res))
    rep.details["final_check"] = {k: res.get(k) for k in ("checked", "failed", "matched", "flagged")}
    if not res.get("checked"):
        rep.warnings.append(f"Caption timing and garbled speech were not checked ({res['why']}). "
                            "The full quality check in the packaging agent checks the finished video again.")
    elif res["failed"]:
        rep.fail("Captions are still off from the voice after re-recording: " + _describe(res)
                 + f". Listen there; if the voice is fine, try: python -m pipeline align {v.slug}")
    elif res["missing"]:
        rep.warnings += [f"At {fmt_ts(m['at'])} the voice may have skipped or garbled: \"{m['text'][:80]}\". Listen there."
                         for m in res["missing"][:5]]


# --- 4. pictures -------------------------------------------------------------------

def picture_check(v: Video, ids: list[int], round_no: int, stub: bool) -> dict[int, dict]:
    shots = {s["id"]: s for s in v.read_json("shots.json")["shots"]}
    out_dir = v.dir / "agents" / "work" / f"pictures_check_{round_no}"
    sheets = s10_qc.contact_sheets(v, out_dir=out_dir, ids=ids)
    files = [str(p.relative_to(v.dir)) for p in sheets]
    text = fill(prompt("crew_images.md"), FILES="\n".join(files),
                SCENES="\n".join(f"shot {i}: {shots[i].get('scene', '')[:240]}" for i in ids))
    answer = llm.ask(text, "crew_images", stub=stub, read_dir=str(v.dir))
    (out_dir / "answer.md").write_text(answer)
    found = {}
    for i in extract_json(answer).get("images", []):
        try:
            sid = int(i.get("shot"))
        except (TypeError, ValueError):
            continue
        if sid in ids:
            found[sid] = {"problem": str(i.get("problem", "")), "severity": "high" if i.get("severity") == "high" else "low"}
    return found


def agent_images(v: Video, a, rep: Report, forced: bool) -> None:
    from . import cli
    if not (v.dir / "shots.json").exists():
        s4_shots.run(v, stub=a.stub)
        rep.checked.append("wrote the shot list (shots.json)")
    shots = v.read_json("shots.json")["shots"]
    price = config.PRICE_PER_IMAGE_SYNC if a.sync else config.PRICE_PER_IMAGE_BATCH
    missing = [s for s in shots if not (v.dir / "images" / f"shot_{s['id']:03d}.png").exists()]
    if missing and not a.stub:
        rep.spend(len(missing) * price, f"drawing {len(missing)} pictures")
    s5_images.run(v, stub=a.stub, sync=a.sync)
    if not cli._done(v, "images"):
        rep.fail("Some pictures are still missing after the images stage. Run it again: python -m pipeline images " + v.slug)
        return
    ids = [s["id"] for s in shots]
    findings = picture_check(v, ids, 1, a.stub)
    rep.checked.append(f"Claude looked at all {len(ids)} pictures: {sum(f['severity'] == 'high' for f in findings.values())} "
                       f"serious problems, {sum(f['severity'] == 'low' for f in findings.values())} minor")
    for attempt in range(1, MAX_IMAGE_RETRIES + 1):
        high = sorted(i for i, f in findings.items() if f["severity"] == "high")
        if not high:
            break
        if len(high) > MAX_IMAGES_PER_RETRY:
            rep.warnings.append(f"{len(high)} pictures flagged at once; that is not a spot problem, so none were redrawn.")
            break
        data = v.read_json("shots.json")
        retire(v.dir / "shots.json")
        for s in data["shots"]:
            if s["id"] in high:
                note = f" (Redraw note: an earlier version had this problem: {findings[s['id']]['problem']}. Avoid it.)"
                s["scene"] = s["scene"].split(" (Redraw note:")[0] + note
                retire(v.dir / "images" / f"shot_{s['id']:03d}.png")
        v.write_json("shots.json", data)
        rep.retries = attempt
        rep.spend(len(high) * price, f"redrawing {len(high)} pictures (retry {attempt} of {MAX_IMAGE_RETRIES})")
        s5_images.run(v, stub=a.stub, sync=a.sync)
        rep.fixed.append(f"Retry {attempt}: redrew shot(s) {high} (" + "; ".join(f"{i}: {findings[i]['problem'][:80]}" for i in high)
                         + "). The old pictures are in _replaced_<date>/images/.")
        again = picture_check(v, high, attempt + 1, a.stub)
        for i in high:
            if i in again:
                findings[i] = again[i]
            else:
                findings.pop(i)
        rep.checked.append(f"after retry {attempt}: {sum(1 for i in high if findings.get(i, {}).get('severity') == 'high')} "
                           f"of {len(high)} redrawn pictures still have a serious problem")
    rep.details["findings"] = {str(k): f for k, f in sorted(findings.items())}
    rep.warnings += [f"Shot {i}: {f['problem']}" for i, f in sorted(findings.items()) if f["severity"] == "low"]
    still = [f"Shot {i}: {f['problem']}" for i, f in sorted(findings.items()) if f["severity"] == "high"]
    if still:
        rep.fail("These pictures still have serious problems: " + "; ".join(still)
                 + ". Edit their scene in shots.json, move the picture to _replaced_<date>/, and run: python -m pipeline crew "
                 + f"--slug {v.slug} --from-agent images")


# --- 5. packaging ----------------------------------------------------------------------

CLAIM_FIELDS = {"title": "title", "thumbnail text": "thumbnail_text", "description opening": "description_intro"}
FIX_MAX_CHARS = {"title": 100, "thumbnail_text": 40, "thumbnail_before": 24, "thumbnail_after": 24, "description_intro": 400}


def claim_field(meta: dict, claim: dict) -> str | None:
    """Which metadata field a claim is about: the field holding exactly the words quoted, else the name the
    quality check gave it ("Thumbnail text (small label)" counts as the thumbnail text)."""
    quoted = " ".join(str(claim.get("text", "")).split()).lower()
    if quoted:
        for f in FIX_MAX_CHARS:
            if " ".join(str(meta.get(f, "")).split()).lower() == quoted:
                return f
    where = str(claim.get("where", "")).strip().lower()
    for name, f in CLAIM_FIELDS.items():
        if where.startswith(name):
            return f
    return None


def usable_fix(fix: str, field: str) -> str:
    """The replacement wording itself. The quality check sometimes answers in a sentence ("Replace 'X' with 'Y' or 'Z'");
    the first quoted wording after "with" / "to" / "use" is taken then. Prose that can't be read that way is not applied."""
    fix = " ".join(str(fix).split())
    m = re.match(r"(?i)(?:replace|change|use|say|rewrite)\b.*?\b(?:with|to|as)\s+['\"\u2018\u201c]([^'\"\u2019\u201d]+)['\"\u2019\u201d]", fix)
    if m:
        fix = m.group(1).strip()
    elif re.match(r"(?i)(replace|change|use|say|rewrite|remove|drop|delete|cut|omit|avoid|reword|rephrase|consider)\b", fix):
        return ""
    return fix if 0 < len(fix) <= FIX_MAX_CHARS.get(field, 100) else ""


def agent_packaging(v: Video, a, rep: Report, forced: bool) -> None:
    s6_assemble.run(v, stub=a.stub, captions=not a.no_captions)
    s7_metadata.run(v, stub=a.stub)
    if not (v.dir / "thumbnail_art.png").exists() and not a.stub:
        rep.spend(config.PRICE_PER_IMAGE_SYNC, "thumbnail art")
    s8_thumbnail.run(v, stub=a.stub)
    qc = s10_qc.run(v, stub=a.stub)
    rep.checked.append(f"full quality check: {'PASSED' if qc['passed'] else 'FAILED'} "
                       f"({len(qc['failures'])} problems, {len(qc['warnings'])} warnings)")
    for round_no in range(1, MAX_CLAIM_FIX_ROUNDS + 1):
        if qc["passed"]:
            break
        claims = (qc.get("checks", {}).get("claude_review") or {}).get("claims", [])
        meta = v.read_json("metadata.json")
        changes = []
        for c in claims:
            field = claim_field(meta, c)
            fix = usable_fix(c.get("fix", ""), field) if field else ""
            if field and fix and c.get("verdict") not in ("SUPPORTED", "OK") and meta.get(field) != fix:
                changes.append((field, meta.get(field, ""), fix, c.get("note", "")))
                meta[field] = fix.upper() if field.startswith("thumbnail") else fix
        if not changes:
            break
        for name in ("metadata.json", "description.txt", "thumbnail.png"):
            retire(v.dir / name)
        v.write_json("metadata.json", meta)
        s7_metadata.run(v, stub=a.stub)  # keeps the edited lines, rebuilds description.txt
        s8_thumbnail.run(v, stub=a.stub)
        for field, old, new, note in changes:
            rep.fixed.append(f"Round {round_no}, {field}: \"{old}\" -> \"{new}\" (the quality check said: {note})")
        qc = s10_qc.run(v, stub=a.stub)
        rep.retries = round_no
        rep.checked.append(f"quality check after fix round {round_no}: {'PASSED' if qc['passed'] else 'FAILED'}")
    s9_review.run(v, stub=a.stub)
    rep.checked.append(f"review package written: out/{v.slug}/review/REVIEW.md")
    rep.details["video"] = s10_qc._video_id(v)
    rep.warnings += qc["warnings"]
    if not qc["passed"]:
        rep.fail("The quality check still fails: " + " | ".join(qc["failures"]) + f". See out/{v.slug}/qc.md.")


# --- 6. Shorts -----------------------------------------------------------------------

def _save_plan(v: Video, plan: dict) -> None:
    v.path("shorts", "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")


def _rule_problems(n: int, it: dict, by_i: dict) -> list[str]:
    text = " ".join(by_i[i]["text"] for i in it["sentences"])
    return s11_shorts.written_problems(f"Short {n}", text, it.get("hook_text", ""), it.get("title", ""),
                                       it.get("description_intro", ""), it.get("opening_question"))


def repick(v: Video, plan: dict, n: int, stub: bool, rep: Report) -> None:
    """Choose a rejected Short again, at most MAX_REPICKS times, telling the picker why it was rejected.
    It may not reuse sentences of the other Shorts, nor the sentences of its own rejected attempts."""
    by_i = {s["i"]: s for s in v.read_json("timeline.json")["sentences"]}
    for attempt in range(1, MAX_REPICKS + 1):
        it = plan["shorts"][n - 1]
        if it.get("review", {}).get("verdict") != "REJECT":
            return
        others = {i for k, o in enumerate(plan["shorts"], start=1) if k != n for i in o["sentences"]}
        history = it.get("repicks", []) + [{"sentences": it["sentences"], "title": it["title"],
                                             "problems": it["review"].get("problems", [])}]
        tried = {i for h in history for i in h["sentences"]}
        feedback = "\n".join(f"- {p}" for p in it["review"].get("problems", [])) or "- (no reason given)"
        log(f"[shorts] short {n} was rejected; choosing again ({attempt} of {MAX_REPICKS}), avoiding "
            f"{len(others)} sentences of the other Shorts")
        try:
            new = s11_shorts.choose(v, 1, stub=stub, exclude=others | tried, feedback=feedback)[0]
        except StageError as e:
            rep.warnings.append(f"Short {n}: choosing again failed ({str(e).splitlines()[0]})")
            return
        new["repicks"] = history
        plan["shorts"][n - 1] = new
        s11_shorts.write_questions(v, plan, [n], stub=stub)
        rule = _rule_problems(n, new, by_i)
        if rule:
            new["review"] = {"verdict": "REJECT", "problems": rule, "key": s11_shorts._review_key(new), "sentences": new["sentences"]}
        else:
            _save_plan(v, plan)
            s11_shorts.review(v, plan, stub=stub)
        _save_plan(v, plan)
        rep.retries += 1
        rep.fixed.append(f"Short {n}: chosen again (attempt {attempt}) after the reviewer rejected sentences "
                         f"{history[-1]['sentences']} for: {feedback.replace(chr(10), ' ')[:300]}. New sentences "
                         f"{new['sentences']}; the check said {new['review']['verdict']}.")


def vertical_sheet(mp4: Path, out_png: Path, duration: float) -> Path:
    """Frames from a Short, side by side with their times, for Claude to look at."""
    from PIL import Image, ImageDraw
    from .s6_assemble import find_ffmpeg
    frames_dir = out_png.parent / "qc_frames"
    frames_dir.mkdir(exist_ok=True)
    rows = math.ceil(SHEET_FRAMES / SHEET_COLS)
    sheet = Image.new("RGB", (SHEET_COLS * FRAME_W, rows * (FRAME_H + 28)), "white")
    draw = ImageDraw.Draw(sheet)
    for k in range(SHEET_FRAMES):
        t = duration * (k + 0.5) / SHEET_FRAMES
        png = frames_dir / f"frame_{k + 1:02d}.png"
        subprocess.run([find_ffmpeg()[0] or "ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1",
                        "-vf", f"scale={FRAME_W}:{FRAME_H}", str(png)], capture_output=True)
        x, y = (k % SHEET_COLS) * FRAME_W, (k // SHEET_COLS) * (FRAME_H + 28)
        if png.exists():
            with Image.open(png) as im:
                sheet.paste(im.convert("RGB"), (x, y + 28))
        draw.text((x + 6, y + 6), f"frame {fmt_ts(t)}", fill="black")
    sheet.save(out_png)
    return out_png


def hook_fits(hook: str) -> bool:
    """Whether the on-screen text fits its band at the top in at most two lines (the same test the builder uses)."""
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("RGB", (config.SHORT_WIDTH, 600)))
    font, size, lines = s11_shorts.hook_lines(d, hook)
    max_w = config.SHORT_WIDTH - 90
    shown = " ".join(" ".join(lines).split())
    return (len(lines) <= 2 and shown == " ".join(hook.split()) and all(d.textlength(ln, font=font) <= max_w for ln in lines)
            and len(lines) * size * 1.18 <= s11_shorts.HOOK_BOTTOM - s11_shorts.HOOK_TOP)


def short_qc(v: Video, n: int, item: dict, stub: bool) -> dict:
    """The Shorts QC: the existing checks (size, captions burned in, loudness, silence, black frames), plus length
    20 to 58 s, caption timing against what is heard (including the spoken question), the on-screen text fitting
    its band, and a frame sheet Claude checks for cut-off pictures and unreadable text."""
    d = v.dir / "shorts" / f"short_{n}"
    base = s11_shorts.qc_short(v, n, stub=stub)
    failures, warnings, checks = list(base["failures"]), list(base["warnings"]), dict(base["checks"])
    dur = checks.get("duration")
    lo = 3.0 if stub else config.SHORT_MIN_S
    if dur is not None and not lo <= dur <= config.SHORT_MAX_S + 0.5:
        failures.append(f"The Short is {dur:.1f}s; it must be {lo:.0f} to {config.SHORT_MAX_S:.0f} seconds.")
    if not hook_fits(item["hook_text"]):
        failures.append(f"The on-screen text \"{item['hook_text']}\" does not fit its band in two lines.")
    tl = v.read_json("timeline.json")
    if stub or tl.get("voice") == "stub":
        checks["caption_timing"] = "not checked (stub voice)"
    elif not align.available():
        warnings.append("Caption timing not checked: faster-whisper isn't installed (pip install -r requirements.txt).")
    else:
        try:
            heard = align.transcribe(d / "short.mp4")
            r = s10_qc.caption_drift(s10_qc._srt_cues((d / "captions.srt").read_text()), heard)
            late = [x for x in r["drifts"] if abs(x["drift"]) > s10_qc.DRIFT_WARN_S]
            checks.update(caption_lines_checked=len(r["drifts"]), caption_lines_off=len(late), caption_words_heard=r["matched"],
                          question_heard=bool(r["drifts"]) and r["drifts"][0]["line"] == 0)
            desc = "; ".join(f"{fmt_ts(x['at'])} \"{x['text'][:30]}\" off by {abs(x['drift']):.1f}s" for x in late[:4])
            if any(abs(x["drift"]) > s10_qc.DRIFT_FAIL_S for x in late) or len(late) > s10_qc.DRIFT_FAIL_SHARE * max(len(r["drifts"]), 1):
                failures.append(f"{len(late)} caption lines are off from the voice: {desc}.")
            elif late:
                warnings.append(f"{len(late)} caption lines are slightly off: {desc}.")
            if r["matched"] < 0.8:
                failures.append(f"Only {r['matched']:.0%} of the caption words (including the spoken question) were heard.")
            for m in r["missing"][:3]:
                warnings.append(f"At {fmt_ts(m['at'])} the voice may have skipped or garbled: \"{m['text'][:60]}\".")
        except Exception as e:
            warnings.append(f"Caption timing not checked: the recognizer failed ({e}).")
    if dur:
        vertical_sheet(d / "short.mp4", d / "qc_sheet.png", dur)
        answer = llm.ask(fill(prompt("crew_short_qc.md"), FILES="qc_sheet.png", HOOK=item["hook_text"]), "crew_short_qc",
                         stub=stub, read_dir=str(d))
        (d / "qc_sheet_answer.md").write_text(answer)
        for p in extract_json(answer).get("problems", []):
            msg = f"Frame {p.get('frame', '?')}: {p.get('problem', '')}"
            (failures if p.get("severity") == "high" else warnings).append(msg)
        checks["frame_sheet"] = "shorts/short_%d/qc_sheet.png" % n
    result = {"passed": not failures, "failures": failures, "warnings": warnings, "checks": checks, "crew_qc": True}
    (d / "qc.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


UPLOAD_SETTINGS = [
    "Audience: No, it's not made for kids",
    "Category: Education",
    "Altered or synthetic content: No",
    "Related video: the full video, {parent}",
    "Visibility: Private or Scheduled until you have watched it",
]


def write_upload(v: Video, n: int, item: dict, parent_title: str) -> Path:
    d = v.dir / "shorts" / f"short_{n}"
    desc = (d / "description.txt").read_text()
    lines = [f"# Upload: Short {n}", "",
             "**Upload this only after the full video is public, and only after you have approved the full video. "
             "Nothing here has been uploaded or posted.**", "",
             f"File: `shorts/short_{n}/short.mp4`", "", "## Title", "", item["title"], "",
             "## Description (paste as it is; it lists the full video's sources)", "", "```", desc.rstrip(), "```", "",
             "## Tags", "", ", ".join(item.get("tags", [])), "", "## Settings", ""]
    lines += [f"- {s.format(parent=parent_title or 'once it is public')}" for s in UPLOAD_SETTINGS]
    lines += ["", "## What it says", "", f"- Spoken first: {item.get('opening_question') or '(none)'}",
              f"- On screen: {item['hook_text']}", f"- Narration: {item['text']}", "",
              f"After 48 hours, record how it did: `python -m pipeline shorts-results {v.slug} {n}`", ""]
    p = d / "upload.md"
    p.write_text("\n".join(lines))
    return p


def agent_shorts(v: Video, a, rep: Report, forced: bool) -> None:
    if not _factcheck_passed(v) or not _qc_passed(v):
        rep.fail("Shorts are only made from a video whose fact-check and quality check both passed.")
        return
    tl = v.read_json("timeline.json")
    by_i = {s["i"]: s for s in tl["sentences"]}
    plan_path = v.path("shorts", "plan.json")
    plan = None if a.replan else s11_shorts._plan_current(v, plan_path)
    if plan:
        rep.checked.append("kept the existing Shorts plan (--replan chooses again)")
    else:
        if plan_path.exists():
            retire(plan_path)
        items = s11_shorts.choose(v, a.count, stub=a.stub)
        plan = {"version": s11_shorts.PLAN_VERSION, "script_sha": hashlib.sha1(v.read_text("script.txt").encode()).hexdigest()[:12],
                "voice_seconds": round(tl["duration"], 1), "shorts": items}
        rep.checked.append(f"chose {len(items)} Shorts (results of earlier Shorts were given to the picker)")
    targets = [n for n, it in enumerate(plan["shorts"], start=1) if it.get("review", {}).get("verdict") != "REJECT"]
    s11_shorts.write_questions(v, plan, targets, stub=a.stub)
    for n, it in enumerate(plan["shorts"], start=1):
        rule = _rule_problems(n, it, by_i)
        if rule:
            it["review"] = {"verdict": "REJECT", "problems": rule, "key": s11_shorts._review_key(it), "sentences": it["sentences"]}
    _save_plan(v, plan)
    s11_shorts.review(v, plan, stub=a.stub)
    _save_plan(v, plan)
    rep.checked.append("separate Claude review of every Short: " + ", ".join(
        f"{n} {it['review']['verdict']}" for n, it in enumerate(plan["shorts"], start=1)))
    for n in range(1, len(plan["shorts"]) + 1):
        if plan["shorts"][n - 1]["review"]["verdict"] == "REJECT":
            repick(v, plan, n, a.stub, rep)

    meta = v.dir / "metadata.json"
    parent_title = json.loads(meta.read_text()).get("title", "") if meta.exists() else v.brief().get("topic", "")
    sources_md = (v.dir / "sources.md").read_text() if (v.dir / "sources.md").exists() else ""
    status = {}
    for n, item in enumerate(plan["shorts"], start=1):
        d = v.dir / "shorts" / f"short_{n}"
        verdict = item["review"]["verdict"]
        if verdict != "PASS":
            for name in ("short.mp4", "upload.md"):
                if (d / name).exists():
                    retire(d / name)  # nothing that did not pass may sit where it could be uploaded
            status[n] = {"status": "REJECTED" if verdict == "REJECT" else "NOT CONFIRMED",
                         "problems": item["review"].get("problems", []), "repicks": len(item.get("repicks", []))}
            rep.warnings.append(f"Short {n} not built ({status[n]['status']}): " + "; ".join(status[n]["problems"])[:400])
            continue
        q = None
        for build in range(MAX_SHORT_REBUILDS + 1):
            if build:
                log(f"[shorts] short {n} failed its quality check; rebuilding it once")
                for old in [d / "short.mp4", d / "clips", *d.glob("question_*.wav")]:
                    retire(old)
            s11_shorts.build_short(v, n, item, stub=a.stub)
            m = {"title": item["title"], "opening_question": item.get("opening_question", ""), "hook_text": item["hook_text"],
                 "tags": item["tags"], "sentences": item["sentences"], "why": item["why"], "parent_title": parent_title}
            (d / "metadata.json").write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
            (d / "description.txt").write_text(s11_shorts.description(item, parent_title, sources_md))
            q = short_qc(v, n, item, a.stub)
            if q["passed"]:
                break
        if build:
            rep.retries += 1
            rep.fixed.append(f"Short {n}: rebuilt once after its quality check failed; now {'PASSED' if q['passed'] else 'still FAILED'}")
        status[n] = {"status": "READY" if q["passed"] else "QC FAILED", "failures": q["failures"], "warnings": q["warnings"],
                     "seconds": q["checks"].get("duration"), "repicks": len(item.get("repicks", []))}
        if q["passed"]:
            write_upload(v, n, item, parent_title)
        else:
            if (d / "upload.md").exists():
                retire(d / "upload.md")
            rep.warnings.append(f"Short {n} failed its quality check: " + "; ".join(q["failures"]))
        rep.warnings += [f"Short {n}: {w}" for w in q["warnings"]]
    _save_plan(v, plan)
    s11_shorts.write_review(v, plan, parent_title)
    rep.details["shorts"] = {str(n): s for n, s in status.items()}
    ready = [n for n, s in status.items() if s["status"] == "READY"]
    rep.checked.append(f"Shorts ready: {ready or 'none'} of {len(plan['shorts'])}; see shorts/REVIEW.md")
    rep.details["sections"] = {"Shorts": "\n".join(
        f"- Short {n}: {s['status']}" + (f" ({s['seconds']:.0f}s)" if s.get("seconds") else "")
        + (f", chosen again {s['repicks']} time(s)" if s.get("repicks") else "")
        + "".join(f"\n  - {p}" for p in s.get("problems", []) + s.get("failures", [])) for n, s in status.items())}
    if not ready:
        rep.fail("No Short is ready: " + "; ".join(f"Short {n} {s['status']}" for n, s in status.items()))


# --- 7. overseer (new) -------------------------------------------------------------------

def _read(p: Path, limit: int = 20000) -> str:
    """Text for the overseer's prompt, cut to `limit` characters. Never parse the result as JSON: use _read_json."""
    return p.read_text()[:limit] if p.exists() else "(missing)"


def _read_json(p: Path, default):
    """A whole JSON file (never a cut-off copy), or the default if it is missing or unreadable."""
    try:
        return json.loads(p.read_text()) if p.exists() else default
    except ValueError:
        return default


def agent_overseer(v: Video, a, rep: Report, forced: bool) -> dict:
    d = v.dir
    reports = []
    for name in AGENTS[:-1]:
        r = read_report(v, name)
        if r:
            reports.append(f"## {name}\n" + json.dumps({k: r[k] for k in r if k != "details"} | {"details": {
                k: x for k, x in r.get("details", {}).items() if k != "sections"}}, indent=1, ensure_ascii=False)[:6000])
    fc = _read_json(d / "factcheck.json", {})
    fc_short = {"passed": fc.get("passed"), "facts": fc.get("facts", []), "policy": fc.get("policy", []),
                "audit_problems": (fc.get("audit") or {}).get("problems", [])}
    meta = _read_json(d / "metadata.json", {})
    tl = _read_json(d / "timeline.json", {})
    stub = a.stub or tl.get("voice") == "stub"
    plan = _read_json(d / "shorts" / "plan.json", {"shorts": []})
    shorts_rep = (read_report(v, "shorts") or {}).get("details", {}).get("shorts", {})
    blocks = []
    for n, it in enumerate(plan.get("shorts", []), start=1):
        st = shorts_rep.get(str(n), {})
        qc = _read(d / "shorts" / f"short_{n}" / "qc.json", 4000)
        blocks.append(f"## Short {n}: status {st.get('status', 'unknown')}\nopening_question: {it.get('opening_question', '')}\n"
                      f"hook_text: {it.get('hook_text', '')}\ntitle: {it.get('title', '')}\ndescription_intro: "
                      f"{it.get('description_intro', '')}\nreview: {json.dumps(it.get('review', {}))}\n"
                      f"narration (script sentences {it.get('sentences')}):\n{it.get('text', '')}\nquality check: {qc}\n")
    summary = (f"slug: {v.slug}\ntitle: {meta.get('title', '')}\nthumbnail text: {meta.get('thumbnail_text', '')}\n"
               f"description opening: {meta.get('description_intro', '')}\nstub run (placeholders): {stub}")
    text = fill(prompt("overseer.md"), SUMMARY=summary, REPORTS="\n\n".join(reports), QC=_read(d / "qc.json", 8000),
                FACTCHECK=json.dumps(fc_short, indent=1, ensure_ascii=False)[:15000], SCRIPT=_read(d / "script.txt"),
                FACTS=_read(d / "facts.txt"), SOURCES=_read(d / "sources.txt"), REVIEW=_read(d / "review" / "REVIEW.md", 8000),
                SHORTS="\n".join(blocks) or "(no Shorts)")
    answer = llm.ask(text, "overseer", web=True, stub=a.stub, context={"shorts": list(range(1, len(plan.get("shorts", [])) + 1))})
    v.path("agents", "work", "overseer_answer.md").write_text(answer)
    try:
        data = extract_json(answer)
    except StageError as e:
        data = {"error": str(e)}
    verdict = enforce(v, data, plan, shorts_rep, stub)
    write_verdict(v, verdict)
    rep.checked.append(f"full video: {verdict['full_video']['verdict']}; Shorts: " + ", ".join(
        f"{s['short']} {s['verdict']}" for s in verdict["shorts"]) + f"; {len(verdict['rechecked_claims'])} claims re-checked")
    return verdict


def _verdict(raw, reasons_if_missing: str) -> dict:
    if not isinstance(raw, dict) or str(raw.get("verdict", "")).upper().replace("_", "-") not in ("GO", "NO-GO"):
        return {"verdict": "NO-GO", "reasons": [reasons_if_missing], "fixes": []}
    return {"verdict": str(raw["verdict"]).upper().replace("_", "-"), "reasons": [str(x) for x in raw.get("reasons", [])],
            "fixes": [str(x) for x in raw.get("fixes", [])]}


def _no_go(v: dict, reason: str, fix: str = "") -> None:
    if v["verdict"] == "GO":
        v["verdict"] = "NO-GO"
        v["reasons"] = []
    v["reasons"].append(reason)
    if fix:
        v["fixes"].append(fix)


def enforce(v: Video, data: dict, plan: dict, shorts_rep: dict, stub: bool) -> dict:
    """The overseer recommends, but some things are never GO, whatever it says."""
    full = _verdict(data.get("full_video"), "The overseer gave no verdict for the full video.")
    claims = [c for c in data.get("rechecked_claims", []) if isinstance(c, dict)]
    if stub:
        _no_go(full, "Stub run: placeholder script, voice and pictures. Never publish it.")
    if not _factcheck_passed(v):
        _no_go(full, "The fact-check has not passed.", f"See out/{v.slug}/factcheck.md")
    if not _qc_passed(v):
        _no_go(full, "The quality check has not passed.", f"See out/{v.slug}/qc.md")
    for c in claims:
        if str(c.get("result", "")).upper() in ("OVERSTATED", "NOT_FOUND"):
            _no_go(full, f"Re-checked claim {c.get('result')}: \"{c.get('claim', '')}\" ({c.get('source_url', '')}). {c.get('note', '')}")
    given = {}
    for s in data.get("shorts", []) or []:
        try:
            given[int(s.get("short"))] = s
        except (TypeError, ValueError, AttributeError):
            continue
    shorts = []
    for n in range(1, len(plan.get("shorts", [])) + 1):
        sv = _verdict(given.get(n), "The overseer gave no verdict for this Short.")
        st = shorts_rep.get(str(n), {}).get("status", "unknown")
        if st != "READY":
            _no_go(sv, f"Not ready: {st}.", "See shorts/REVIEW.md.")
        if full["verdict"] != "GO":
            _no_go(sv, "The full video is NO-GO, and a Short goes up only after the full video is public.")
        shorts.append({"short": n, "title": plan["shorts"][n - 1].get("title", ""), **sv})
    return {"slug": v.slug, "timestamp": _now(), "stub": stub, "full_video": full, "shorts": shorts,
            "rechecked_claims": claims, "summary": str(data.get("summary", "")), "error": data.get("error", "")}


def write_verdict(v: Video, verdict: dict) -> None:
    meta = v.dir / "metadata.json"
    title = json.loads(meta.read_text()).get("title", v.slug) if meta.exists() else v.slug
    f = verdict["full_video"]
    lines = [f"# Verdict: {title}", "", f"**Full video: {f['verdict']}**", ""]
    if verdict.get("summary"):
        lines += [verdict["summary"], ""]
    lines += [f"- {r}" for r in f["reasons"]]
    if f["fixes"]:
        lines += ["", "Fixes:"] + [f"- {x}" for x in f["fixes"]]
    for s in verdict["shorts"]:
        lines += ["", f"## Short {s['short']}: {s.get('title', '')}", "", f"**{s['verdict']}**", ""]
        lines += [f"- {r}" for r in s["reasons"]]
        if s["fixes"]:
            lines += ["", "Fixes:"] + [f"- {x}" for x in s["fixes"]]
        if s["verdict"] == "GO":
            lines.append(f"- Upload notes: shorts/short_{s['short']}/upload.md")
    if verdict["rechecked_claims"]:
        lines += ["", "## Claims the overseer re-checked at their sources", ""]
        lines += [f"- {c.get('result', '?')}: \"{c.get('claim', '')}\" {c.get('source_url', '')} {c.get('note', '')}".rstrip()
                  for c in verdict["rechecked_claims"]]
    if verdict.get("error"):
        lines += ["", f"Overseer problem: {verdict['error']}"]
    lines += ["", "---", "", "This is a recommendation. No agent approved, uploaded or posted anything.",
              f"If you agree: watch it, then `python -m pipeline approve {v.slug}`. Upload Shorts only after the full "
              "video is public, one at a time, using each Short's upload.md.", ""]
    v.write_json("agents/VERDICT.json", verdict)
    v.path("agents", "VERDICT.md").write_text("\n".join(lines))


def write_stopped_verdict(v: Video, agent: str, rep: Report) -> None:
    """When the crew stops early there is nothing for the overseer to judge: the verdict is NO-GO, with why."""
    plan_p = v.dir / "shorts" / "plan.json"
    n_shorts = len(json.loads(plan_p.read_text()).get("shorts", [])) if plan_p.exists() else 0
    reason = f"The crew stopped at the {agent} agent: " + " ".join(rep.could_not_fix)[:1500]
    fix = f"Fix that (see agents/{agent}.md), then run: python -m pipeline crew --slug {v.slug}"
    verdict = {"slug": v.slug, "timestamp": _now(), "stub": False, "stopped_at": agent,
               "full_video": {"verdict": "NO-GO", "reasons": [reason], "fixes": [fix]},
               "shorts": [{"short": n, "title": "", "verdict": "NO-GO", "reasons": ["The full video is not finished."], "fixes": []}
                          for n in range(1, n_shorts + 1)],
               "rechecked_claims": [], "summary": "", "error": ""}
    write_verdict(v, verdict)


# --- the command ----------------------------------------------------------------------

AGENT_FUNCS = {"script_factcheck": agent_script_factcheck, "voice_captions": agent_voice_captions, "images": agent_images,
               "packaging": agent_packaging, "shorts": agent_shorts, "overseer": agent_overseer}


def cmd_crew(a) -> Video | None:
    if not 2 <= a.count <= 4:
        raise StageError("Choose 2, 3 or 4 Shorts per video (--count).")
    if a.from_agent and not (a.slug or a.topic):
        raise StageError("--from-agent needs the video: add --slug <slug>.")
    v = start(a)
    if v is None:
        return None
    names = AGENTS[1:]
    first_forced = names.index(a.from_agent) if a.from_agent else None
    confirm_cost(estimate(v, a), a)
    running = False
    for idx, name in enumerate(names):
        forced = first_forced is not None and idx >= first_forced
        if name != "overseer" and not forced and not running and agent_done(v, name):
            log(f"[{name}] already done, skipping")
            continue
        running = True  # once one agent runs, every later one runs too: its input may have changed
        log(f"\n=== agent: {name} ===")
        rep = Report(name, v.slug)
        try:
            AGENT_FUNCS[name](v, a, rep, forced)
        except StageError as e:
            rep.fail(str(e))
        except Exception as e:  # a bug in an agent must still leave a report and a NO-GO, not a bare traceback
            import traceback
            log(traceback.format_exc())
            rep.fail(f"The {name} agent hit an unexpected error ({type(e).__name__}: {e}). This is a bug in the crew, not in your video.")
        rep.write(v)
        if not rep.passed and name in STOPPING:
            write_stopped_verdict(v, name, rep)
            raise StageError(f"The {name} agent stopped:\n  " + "\n  ".join(rep.could_not_fix)[:3000]
                             + f"\nReport: out/{v.slug}/agents/{name}.md. Verdict: NO-GO (out/{v.slug}/agents/VERDICT.md).\n"
                             f"After fixing it, run: python -m pipeline crew --slug {v.slug}")
        if not rep.passed:
            log(f"  {name}: " + " ".join(rep.could_not_fix)[:500])
    verdict = json.loads((v.dir / "agents" / "VERDICT.json").read_text())
    log(f"\nVerdict: full video {verdict['full_video']['verdict']}; " + ", ".join(
        f"Short {s['short']} {s['verdict']}" for s in verdict["shorts"]))
    log(f"Read out/{v.slug}/agents/VERDICT.md. Nothing was approved, uploaded or posted.")
    return v


# --- shorts-results -----------------------------------------------------------------------

def _seconds(text) -> float:
    t = str(text).strip()
    if ":" in t:
        m, s = t.split(":", 1)
        return int(m) * 60 + float(s)
    return float(t)


def _ask(label: str, given, tty: bool, default=None):
    if given not in (None, ""):
        return given
    if not tty:
        if default is not None:
            return default
        raise StageError(f"Missing {label}. Pass it as a flag (see: python -m pipeline shorts-results -h).")
    ans = input(f"{label}{f' [{default}]' if default is not None else ''}: ").strip()
    return ans or default


def cmd_shorts_results(a) -> dict:
    """Record how a Short did after 48 hours; the picker reads every results.json next time it chooses Shorts."""
    v = Video(a.slug)
    n = int(a.n)
    d = v.dir / "shorts"
    plan = json.loads((d / "plan.json").read_text()) if (d / "plan.json").exists() else {"shorts": []}
    item = plan["shorts"][n - 1] if 0 < n <= len(plan["shorts"]) else {}
    mp4 = d / f"short_{n}" / "short.mp4"
    from .common import media_duration
    length_default = f"{media_duration(mp4):.0f}" if mp4.exists() else None
    tty = sys.stdin.isatty()
    views = int(_ask("Views", a.views, tty))
    avg = _seconds(_ask("Average view duration (m:ss)", a.avg, tty))
    length = _seconds(_ask("Length of the Short (m:ss)", a.length, tty, length_default))
    swiped = float(str(_ask("% swiped away", a.swiped, tty)).rstrip("%"))
    subs = int(_ask("Subscribers gained", a.subs, tty, "0"))
    path = d / "results.json"
    data = json.loads(path.read_text()) if path.exists() else {"slug": v.slug, "shorts": {}}
    old = data["shorts"].get(str(n))
    rec = {"title": item.get("title") or (old or {}).get("title", ""), "hook_text": item.get("hook_text") or (old or {}).get("hook_text", ""),
           "opening_question": item.get("opening_question") or (old or {}).get("opening_question", ""),
           "views": views, "avg_view_s": avg, "length_s": length, "retention": round(avg / length, 3) if length else None,
           "swiped_away_pct": swiped, "subscribers": subs, "recorded_at": a.when or _now()[:10],
           "history": ((old or {}).get("history", []) + [{k: x for k, x in old.items() if k != "history"}]) if old else []}
    data["shorts"][str(n)] = rec
    data["updated"] = _now()
    ranked = sorted(data["shorts"].items(), key=lambda kv: -(kv[1].get("retention") or 0))
    data["comparison"] = [{"short": int(k), "retention": r["retention"], "swiped_away_pct": r["swiped_away_pct"],
                           "views": r["views"], "hook_text": r["hook_text"], "opening_question": r["opening_question"]} for k, r in ranked]
    d.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    write_results_tables()
    log(f"  Short {n}: watched {(rec['retention'] or 0):.0%} of its length on average, {swiped:.0f}% swiped away, {views} views.")
    log(f"  Saved out/{v.slug}/shorts/results.json. Comparison of every Short: out/SHORTS_RESULTS.md")
    return data


def write_results_tables() -> None:
    """out/SHORTS_RESULTS.md (every Short on the channel) and out/<slug>/shorts/RESULTS.md, best held first."""
    rows = []
    for f in sorted(config.OUT.glob("*/shorts/results.json")):
        for n, r in json.loads(f.read_text()).get("shorts", {}).items():
            rows.append((f.parent.parent.name, n, r))
    rows.sort(key=lambda x: -(x[2].get("retention") or 0))
    head = ["# How the Shorts did (best held first)", "",
            "Held is the share of its length the average viewer watched. The Shorts picker reads these results.", "",
            "| Video | Short | Held | Swiped away | Views | Subs | Spoken first | On screen |", "|---|---|---|---|---|---|---|---|"]

    def row(s, n, r):
        return (f"| {s} | {n} | {(r.get('retention') or 0):.0%} ({r.get('avg_view_s', 0):.0f}s of {r.get('length_s', 0):.0f}s) | "
                f"{r.get('swiped_away_pct', '?')}% | {r.get('views', '?')} | {r.get('subscribers', '?')} | "
                f"{(r.get('opening_question') or '').replace('|', '/')} | {(r.get('hook_text') or r.get('title') or '').replace('|', '/')} |")

    (config.OUT / "SHORTS_RESULTS.md").write_text("\n".join(head + [row(*x) for x in rows]) + "\n")
    for slug in sorted({x[0] for x in rows}):
        (config.OUT / slug / "shorts" / "RESULTS.md").write_text("\n".join(head + [row(*x) for x in rows if x[0] == slug]) + "\n")
