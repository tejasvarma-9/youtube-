"""Shorts: 2 to 4 vertical clips (1080x1920, under a minute) cut from a finished, fact-checked video.

python -m pipeline shorts <slug>   (needs the video's script, fact-check, voiceover, shots and pictures)

A Short never has new narration. Claude only CHOOSES sentences that already exist in the video's
script; the clip is those sentences, in order, cut from the video's own voiceover (same voice, no
new voice cost), over the video's own pictures shown in a vertical frame (the picture on top of a
blurred copy of itself), with large word-by-word captions and a short logo card at the end. Because
the words are the ones the fact-check already passed, nothing needs fact-checking again. The title
and description Claude writes may only use numbers that appear in the Short's own sentences.

Each Short also carries a short hook_text shown on screen above the picture for the whole clip, and
it ends on its own last line (no "watch the full video" card: Shorts that work as trailers stop being
shown). Before anything is built, a separate Claude check (prompts/shorts_review.md), which did not
write those lines, reads each Short's hook_text, title and description against its narration and the
full script. It passes them, rewrites them, or rejects the Short; a Short that does not pass is not built.

Everything is written to out/<slug>/shorts/: plan.json, REVIEW.md and one folder per Short
(short.mp4, captions.srt, metadata.json, description.txt, qc.json).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
import re
import subprocess

from . import config, llm, trends
from .common import (StageError, Video, extract_json, fill, fmt_ts, log, media_duration, prompt, retire, run as sh,
                     split_sentences, voice_profile)
from .lint import _numbers
from .s6_assemble import MOTIONS, find_ffmpeg

CLIP_VERSION = "short4"   # bump when the picture layout changes, so cached clips are rebuilt
PLAN_VERSION = 2          # plans from an older version (no hook_text, no review) are chosen again
# Shorts only breathe: a small centred push-in or pull-out. The full video's pans and 10% zoom cut figures drawn near
# an edge, and a Short shows the whole 16:9 picture across a narrow frame, so nothing may be cropped here.
SHORT_MOTIONS = [
    lambda t: (1 + 0.04 * t, 0.5, 0.5),
    lambda t: (1.04 - 0.04 * t, 0.5, 0.5),
]
HEAD_PAD_S = 0.10         # a little room before the first word of a sentence
TAIL_PAD_S = 0.35         # and after the last
GAP_S = 0.12              # silence between kept sentences
HOLD_S = 5.0              # the same picture is re-framed (punch in or pan) at least this often
FG_TOP = 420              # top edge of the picture inside the 1080x1920 frame
HOOK_TOP, HOOK_BOTTOM = 140, 395   # the on-screen hook text sits here, below the Shorts app's top bar
HOOK_MAX_CHARS = 42
QUESTION_MAX_WORDS = 14
QUESTION_GAP_S = 0.30     # pause between the spoken opening question and the first sentence
QUESTION_FIELDS = ("opening_question", "hook_text", "title", "description_intro")
QUESTION_S_PER_WORD = 0.38   # about how long the narrator takes per word of the opening question
SAME_THING_OVERLAP = 0.6     # share of content words two lines may share before they "say the same thing"
# Captions sit above the Shorts app's title and buttons (bottom ~400 px). libass scales its font units to
# the video height (units of 1/288), so 11 is about 73 px and MarginV 110 puts the line's bottom near y=1185, just under the picture.
CAPTION_STYLE = (
    "FontName=Arial,FontSize=11,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,"
    "BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=110"
)
BAD_HOOK_START = re.compile(r"^\s*(and|but|so|then|now|that|this|it|they|which|these|those|also)\b", re.I)
ODD_CHARS = re.compile(r"[#\U0001F000-\U0001FFFF\u2600-\u27BF]")
BACK_POINTER = re.compile(r"\b(as i said|as we saw|earlier|the first place|the next (?:company|two|one)|zoom out|back to the question)\b", re.I)


# --- choosing -----------------------------------------------------------------

def blocked_sentences(sentences: list[dict]) -> set[int]:
    """The welcome line, anything naming the channel, and the last two paragraphs (pointer and sign-off)."""
    name_re = re.compile(r"\b" + re.escape(config.CHANNEL_NAME) + r"\b", re.I)
    last = max(s.get("paragraph", 0) for s in sentences)
    return {s["i"] for s in sentences if name_re.search(s["text"]) or s.get("paragraph", 0) >= last - 1}


def cut_points(s: dict) -> tuple[float, float]:
    """Where in voiceover.wav a sentence is cut: from just before its first word to just after its last."""
    words = s.get("words") or []
    a = max(0.0, (words[0][0] if words else s["start"]) - HEAD_PAD_S)
    end = s["end"]
    if words:
        end = min(end, words[-1][1] + TAIL_PAD_S)
    return a, max(end, a + 0.2)


def length_of(s: dict) -> float:
    a, b = cut_points(s)
    return (b - a) + GAP_S


def question_seconds(question: str | None) -> float:
    """About how long the spoken opening question takes, with its pause (for the length limit before it is recorded)."""
    return len(question.split()) * QUESTION_S_PER_WORD + QUESTION_GAP_S if question else 0.0


def validate_plan(plan: dict, sentences: list[dict], count: int, stub: bool = False,
                  exclude=(), avoid_sets=()) -> tuple[list[dict], list[str]]:
    """The chosen Shorts with their lengths and texts, and everything wrong with the choice.
    exclude: sentence numbers other Shorts already use (when one Short is chosen again)."""
    by_i = {s["i"]: s for s in sentences}
    blocked = blocked_sentences(sentences)
    exclude = set(exclude)
    min_s = 3.0 if stub else config.SHORT_MIN_S
    problems, items, used = [], [], set()
    raw = plan.get("shorts") if isinstance(plan, dict) else None
    if not isinstance(raw, list) or not raw:
        return [], ["The answer has no \"shorts\" list."]
    if len(raw) != count:
        problems.append(f"Asked for {count} Shorts but got {len(raw)}.")
    for n, sh_ in enumerate(raw, start=1):
        label = f"Short {n}"
        try:
            ids = [int(x) for x in sh_["sentences"]]
        except (KeyError, TypeError, ValueError):
            problems.append(f"{label}: \"sentences\" must be a list of sentence numbers.")
            continue
        if not ids or ids != sorted(set(ids)):
            problems.append(f"{label}: sentence numbers must be in increasing order with no repeats.")
            continue
        bad = [i for i in ids if i not in by_i]
        if bad:
            problems.append(f"{label}: no such sentence number(s) {bad}.")
            continue
        if set(ids) & blocked:
            problems.append(f"{label}: uses blocked sentence(s) {sorted(set(ids) & blocked)} (welcome line, channel name or ending).")
        if set(ids) & exclude:
            problems.append(f"{label}: uses sentence(s) {sorted(set(ids) & exclude)}, which another Short already uses or which were rejected.")
        if any(sorted(ids) == sorted(a) for a in avoid_sets):
            problems.append(f"{label}: these exact sentences {ids} were already rejected. Change the choice (you may keep "
                            "some of them, for example the claim, and add the sentence that fixes the problem).")
        if set(ids) & used:
            problems.append(f"{label}: reuses sentence(s) {sorted(set(ids) & used)} from another Short.")
        used |= set(ids)
        total = sum(length_of(by_i[i]) for i in ids)
        q_s = question_seconds(sh_.get("opening_question")) if isinstance(sh_.get("opening_question"), str) else 0.0
        if not min_s <= total + q_s <= config.SHORT_MAX_S:
            problems.append(f"{label}: {total + q_s:.0f} seconds long" + (f" (with about {q_s:.0f}s for the spoken question)" if q_s else "")
                            + f"; it must be {min_s:.0f} to {config.SHORT_MAX_S:.0f}.")
        first = by_i[ids[0]]["text"]
        if BAD_HOOK_START.search(first) or BACK_POINTER.search(first):
            problems.append(f"{label}: starts on \"{first[:60]}\", which leans on what came before. Start on a sentence that stands alone.")
        for i in ids[1:]:
            if BACK_POINTER.search(by_i[i]["text"]):
                problems.append(f"{label}: sentence {i} points back at other parts of the video (\"{by_i[i]['text'][:60]}\").")
        text = " ".join(by_i[i]["text"] for i in ids)
        title = str(sh_.get("title", "")).strip()
        intro = str(sh_.get("description_intro", "")).strip()
        hook = " ".join(str(sh_.get("hook_text", "")).split())
        question = " ".join(str(sh_["opening_question"]).split()) if "opening_question" in sh_ else None
        problems += written_problems(label, text, hook, title, intro, question)
        tags = [str(t).strip() for t in sh_.get("tags", []) if str(t).strip()][:8]
        items.append({**({"opening_question": question} if question is not None else {}), "hook_text": hook, "title": title, "sentences": ids, "description_intro": intro, "tags": tags,
                      "why": str(sh_.get("why", "")).strip(), "seconds": round(total, 1), "text": text})
    return items, problems


_STOP = set("a an and are as at be but by can do does did for from has have how if in into is it its of on or so that "
            "the their them then there these they this those to was were what when where which who whom why will with "
            "would you your about just only than".split())


def _content_words(text: str) -> set[str]:
    words = (w.strip(".,'-") for w in re.findall(r"[a-z0-9$%][a-z0-9$%.,'-]*", text.lower()))
    return {w for w in words if w not in _STOP and len(w) > 2}


def near_duplicate(a: str, b: str) -> bool:
    """Two lines that say the same thing in nearly the same words."""
    x, y = _content_words(a), _content_words(b)
    if len(x & y) < 2:
        return False
    return len(x & y) / len(x | y) >= SAME_THING_OVERLAP or len(x & y) >= min(len(x), len(y))


def written_problems(label: str, text: str, hook: str, title: str, intro: str, question: str | None = None) -> list[str]:
    """Mechanical rules for the lines written for a Short (the review step judges the rest).
    question=None means the Short has no spoken opening question (yet); "" means it is missing."""
    problems = []
    # The question, the on-screen text and the first narrated line each do a different job; none repeats another.
    first = (split_sentences(text) or [""])[0]
    trio = [("opening_question", question or ""), ("hook_text", hook), ("first narrated sentence", first)]
    for k, (na, a) in enumerate(trio):
        for nb, b in trio[k + 1:]:
            if a and b and near_duplicate(a, b):
                problems.append(f"{label}: the {na} and the {nb} say the same thing (\"{a[:50]}\" / \"{b[:50]}\"). "
                                "Each must do a different job: ask, set up the contrast, start the story.")
    if question is not None:
        words = question.split()
        if not words or len(words) > QUESTION_MAX_WORDS or not question.endswith("?") or ODD_CHARS.search(question):
            problems.append(f"{label}: opening_question must be one question of at most {QUESTION_MAX_WORDS} words ending with \"?\".")
        elif question.rstrip("?").lower() in text.lower():
            problems.append(f"{label}: opening_question just repeats the narration.")
        extra = sorted(_numbers(question) - _numbers(text))
        if extra:
            problems.append(f"{label}: the opening_question uses figure(s) {extra} that are not in the Short's own sentences.")
    if not hook or len(hook) > HOOK_MAX_CHARS or ODD_CHARS.search(hook):
        problems.append(f"{label}: hook_text must be 1 to {HOOK_MAX_CHARS} characters with no hashtags or emojis (got {len(hook)}).")
    elif hook.rstrip(".?!").lower() in text.lower():
        problems.append(f"{label}: hook_text just repeats the narration; it should set up the question instead.")
    if not title or len(title) > 70 or ODD_CHARS.search(title):
        problems.append(f"{label}: the title must be 1 to 70 characters with no hashtags or emojis.")
    if not intro:
        problems.append(f"{label}: description_intro is empty.")
    allowed = _numbers(text)
    for where, value in (("hook_text", hook), ("title", title), ("description_intro", intro)):
        extra = sorted(_numbers(value) - allowed)
        if extra:
            problems.append(f"{label}: the {where} uses figure(s) {extra} that are not in the Short's own sentences.")
    return problems


def _review_key(item: dict) -> str:
    fields = [item["hook_text"], item["title"], item["description_intro"], item["sentences"]]
    if item.get("opening_question"):  # plans from before spoken questions keep their keys
        fields.insert(0, item["opening_question"])
    raw = json.dumps(fields)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def review(video: Video, plan: dict, stub: bool = False) -> None:
    """An independent Claude check of every Short's written lines. Fills item["review"]; FIX suggestions are
    applied (if they pass the mechanical rules) and checked again once. Shorts reviewed before keep their result
    while their lines are unchanged, so a hand edit to plan.json is checked again automatically."""
    meta = video.dir / "metadata.json"
    title = json.loads(meta.read_text()).get("title", "") if meta.exists() else video.brief().get("topic", "")
    script = video.read_text("script.txt")
    for attempt in (1, 2):
        # A rejection is about the narration itself, and the reviewer does not always give the same answer twice, so
        # a rejected Short stays rejected until its sentences change (or --replan), rather than being asked again.
        todo = [(n, it) for n, it in enumerate(plan["shorts"], start=1)
                if it.get("review", {}).get("key") != _review_key(it)
                and not (it.get("review", {}).get("verdict") == "REJECT" and it["review"].get("sentences", it["sentences"]) == it["sentences"])]
        if not todo:
            return
        blocks = []
        for n, it in todo:
            blocks.append(f"## Short {n}\nopening_question: {it.get('opening_question') or '(none)'}\n"
                          f"hook_text: {it['hook_text']}\ntitle: {it['title']}\n"
                          f"description_intro: {it['description_intro']}\nnarration ({it['seconds']:.0f} seconds, "
                          f"script sentences {it['sentences']}):\n{it['text']}\n")
        text = fill(prompt("shorts_review.md"), TITLE=title, SCRIPT=script, SHORTS="\n".join(blocks))
        answer = llm.ask(text, "shorts_review", stub=stub, context={"shorts": [n for n, _ in todo]})
        video.path("shorts", "raw", f"review_answer_{attempt}.md").write_text(answer)
        got = {int(r.get("short", 0)): r for r in extract_json(answer).get("reviews", []) if isinstance(r, dict)}
        for n, it in todo:
            r = got.get(n, {"verdict": "REJECT", "problems": ["The reviewer gave no answer for this Short."]})
            verdict = str(r.get("verdict", "")).upper()
            problems = [str(p) for p in r.get("problems", [])]
            if verdict == "FIX" and attempt == 1:
                fixed = dict(it)
                for field in QUESTION_FIELDS:
                    if str(r.get(field, "")).strip() and (field != "opening_question" or "opening_question" in it):
                        fixed[field] = " ".join(str(r[field]).split())
                rule = written_problems(f"Short {n}", it["text"], fixed["hook_text"], fixed["title"], fixed["description_intro"],
                                        fixed.get("opening_question"))
                if not rule and _review_key(fixed) != _review_key(it):
                    before = {f: it.get(f) for f in QUESTION_FIELDS}
                    it.update({f: fixed[f] for f in before if f in fixed})
                    it["review_history"] = it.get("review_history", []) + [{"before": before, "problems": problems}]
                    log(f"  short {n}: the reviewer rewrote its lines; checking them again")
                    continue  # no "review" key for the new lines, so attempt 2 checks them
                problems += rule
            it["review"] = {"verdict": "PASS" if verdict == "PASS" else ("REJECT" if verdict == "REJECT" else "FIX"),
                            "problems": problems, "key": _review_key(it), "sentences": it["sentences"]}
    for n, it in enumerate(plan["shorts"], start=1):  # still unreviewed after two rounds
        if it.get("review", {}).get("key") != _review_key(it) and it.get("review", {}).get("verdict") != "REJECT":
            it["review"] = {"verdict": "FIX", "problems": ["The rewritten lines were not confirmed by a second check."],
                            "key": _review_key(it)}


def write_questions(video: Video, plan: dict, targets: list[int], stub: bool = False) -> None:
    """A spoken opening question for each target Short that has none yet (plans made before questions existed)."""
    todo = [(n, it) for n, it in enumerate(plan["shorts"], start=1) if n in targets and not it.get("opening_question")]
    if not todo:
        return
    meta = video.dir / "metadata.json"
    title = json.loads(meta.read_text()).get("title", "") if meta.exists() else video.brief().get("topic", "")
    blocks = [f"## Short {n}\non-screen text: {it['hook_text']}\ntitle: {it['title']}\nnarration:\n{it['text']}\n" for n, it in todo]
    answer = llm.ask(fill(prompt("shorts_question.md"), VOICE=voice_profile(), TITLE=title, SHORTS="\n".join(blocks)),
                     "shorts_question", stub=stub, context={"shorts": [n for n, _ in todo]})
    video.path("shorts", "raw", "question_answer.md").write_text(answer)
    got = {int(q.get("short", 0)): " ".join(str(q.get("opening_question", "")).split())
           for q in extract_json(answer).get("questions", []) if isinstance(q, dict)}
    for n, it in todo:
        q = got.get(n, "")
        rule = written_problems(f"Short {n}", it["text"], it["hook_text"], it["title"], it["description_intro"], q)
        if rule:
            raise StageError("Claude's opening question breaks the rules:\n  " + "\n  ".join(rule) + "\nRun the command again.")
        it["opening_question"] = q
        log(f"  short {n}: opening question \"{q}\"")


def past_results_text(limit: int = 12) -> str:
    """How earlier Shorts on the channel did (from every out/<slug>/shorts/results.json), best held first,
    so the picker can repeat the openings that kept people watching and avoid the ones they swiped past."""
    rows = []
    for f in sorted(config.OUT.glob("*/shorts/results.json")):
        try:
            data = json.loads(f.read_text())
        except ValueError:
            continue
        for n, r in (data.get("shorts") or {}).items():
            if isinstance(r, dict) and r.get("retention") is not None:
                rows.append((f.parent.parent.name, n, r))
    if not rows:
        return "(no results recorded yet)"
    rows.sort(key=lambda x: -(x[2].get("retention") or 0))
    out = []
    for slug, n, r in rows[:limit]:
        out.append(f"- {slug} Short {n}: held {r['retention']:.0%} of its length on average "
                   f"({r.get('avg_view_s', 0):.0f}s of {r.get('length_s', 0):.0f}s), {r.get('swiped_away_pct', '?')}% swiped away, "
                   f"{r.get('views', '?')} views, {r.get('subscribers', '?')} subscribers. "
                   f"Spoken first: \"{r.get('opening_question') or '(none)'}\" On screen: \"{r.get('hook_text', '')}\" "
                   f"Title: \"{r.get('title', '')}\"")
    return "\n".join(out)


def choose(video: Video, count: int, stub: bool = False, exclude=(), feedback: str = "", avoid_sets=()) -> list[dict]:
    """Ask Claude for `count` Shorts. exclude: sentence numbers it may not use (other Shorts' sentences, or ones
    already rejected); feedback: the reviewer's reasons for rejecting the last choice, passed back to the picker."""
    tl = video.read_json("timeline.json")
    sentences = tl["sentences"]
    exclude = set(exclude)
    blocked = blocked_sentences(sentences) | exclude
    meta_path = video.dir / "metadata.json"
    title = json.loads(meta_path.read_text()).get("title", "") if meta_path.exists() else video.brief().get("topic", "")
    lines = "\n".join(f"[{s['i']}] ({s['start']:.1f}s, {length_of(s):.1f}s) {s['text']}" for s in sentences)
    base = fill(prompt("shorts.md"), MIN_S=f"{config.SHORT_MIN_S:.0f}", MAX_S=f"{config.SHORT_MAX_S:.0f}", VOICE=voice_profile(),
                TITLE=title, SENTENCES=lines, BLOCKED=", ".join(str(i) for i in sorted(blocked)) or "none", COUNT=str(count),
                PAST_RESULTS=past_results_text(), TRENDS=trends.writer_notes(stub))
    if feedback:
        base += ("\n\nA reviewer who did not choose them REJECTED an earlier choice for this Short, for these reasons. "
                 "Choose sentences that fix every one of these problems. You may keep some of the rejected sentences "
                 "(often the claim itself) and add the ones that fix it, but not the exact same set:\n" + feedback.strip() + "\n")
        if avoid_sets:
            base += "Rejected sets (do not choose exactly one of these again): " + "; ".join(str(list(a)) for a in avoid_sets) + "\n"
    ctx = {"sentences": [{"i": s["i"], "text": s["text"], "seconds": length_of(s)} for s in sentences],
           "blocked": sorted(blocked), "count": count, "avoid_sets": [list(a) for a in avoid_sets]}
    extra = ""
    for attempt in (1, 2):
        answer = llm.ask(base + extra, "shorts", stub=stub, context=ctx)
        video.path("shorts", "raw", f"plan_answer_{attempt}.md").write_text(answer)
        items, problems = validate_plan(extract_json(answer), sentences, count, stub=stub, exclude=exclude, avoid_sets=avoid_sets)
        if not problems:
            return items
        log("  the plan had problems:\n    " + "\n    ".join(problems))
        extra = ("\n\nYour previous answer had these problems. Choose again and fix every one of them:\n- "
                 + "\n- ".join(problems) + "\n")
    raise StageError("Claude's Shorts plan still breaks the rules after a second try:\n  " + "\n  ".join(problems)
                     + f"\nSee out/{video.slug}/shorts/raw/. Run the command again to get a fresh attempt.")


# --- building -----------------------------------------------------------------

def _shot_of(sentence: dict, shots: list[dict]) -> dict:
    for shot in shots:
        if sentence["i"] in (shot.get("sentences") or []):
            return shot
    for shot in shots:
        if shot["start"] <= sentence["start"] < shot["end"]:
            return shot
    return shots[-1]


def chunk_words(text: str, max_words: int = 3, max_chars: int = 18) -> list[list[int]]:
    """Caption chunks as lists of word positions: at most 3 words and about 18 characters each."""
    chunks, cur, chars = [], [], 0
    for k, w in enumerate(text.split()):
        if cur and (len(cur) >= max_words or chars + 1 + len(w) > max_chars):
            chunks.append(cur)
            cur, chars = [], 0
        cur.append(k)
        chars += len(w) + (1 if len(cur) > 1 else 0)
    if cur:
        chunks.append(cur)
    return chunks


def write_srt(parts: list[dict], path) -> None:
    """Big word-by-word captions. parts: per sentence, its text, word times, cut start, offset and length in the Short."""
    n, lines = 0, []
    for p in parts:
        words = p["text"].split()
        times = p.get("words")
        if not (times and len(times) == len(words)):
            span = max(p["length"] - GAP_S, 0.2)
            times = [[(p["cut"] + span * k / len(words)), (p["cut"] + span * (k + 1) / len(words))] for k in range(len(words))]
        chunks = chunk_words(p["text"])
        seg_end = p["offset"] + p["length"]
        for ci, ch in enumerate(chunks):
            a = p["offset"] + max(times[ch[0]][0] - p["cut"], 0.0)
            if ci + 1 < len(chunks):
                b = p["offset"] + max(times[chunks[ci + 1][0]][0] - p["cut"], 0.0)
            else:
                b = min(p["offset"] + times[ch[-1]][1] - p["cut"] + 0.15, seg_end)
            n += 1
            lines += [str(n), f"{fmt_ts(a, srt=True)} --> {fmt_ts(max(b, a + 0.12), srt=True)}", " ".join(words[k] for k in ch), ""]
    path.write_text("\n".join(lines))


def _cover(img, w: int, h: int):
    from PIL import Image
    s = max(w / img.width, h / img.height)
    big = img.resize((math.ceil(img.width * s), math.ceil(img.height * s)), Image.LANCZOS)
    x, y = (big.width - w) // 2, (big.height - h) // 2
    return big.crop((x, y, x + w, y + h))


def hook_lines(draw, hook: str):
    """The hook text at the biggest size that fits the band in at most two lines."""
    from .s8_thumbnail import _font, _wrap
    max_w = config.SHORT_WIDTH - 90
    for size in range(96, 50, -4):
        font = _font(size)
        lines = _wrap(draw, hook, font, max_w)
        height = len(lines) * size * 1.18
        if len(lines) <= 2 and all(draw.textlength(ln, font=font) <= max_w for ln in lines) and height <= HOOK_BOTTOM - HOOK_TOP:
            return font, size, lines
    return font, size, _wrap(draw, hook, font, max_w)[:2]


def _backdrop(src, hook: str = ""):
    """The picture, blurred and darkened to fill the frame, with the hook text at the top (white, last line yellow)."""
    from PIL import ImageDraw, ImageEnhance, ImageFilter
    from .s8_thumbnail import WHITE, YELLOW

    W, H = config.SHORT_WIDTH, config.SHORT_HEIGHT
    bg = ImageEnhance.Brightness(_cover(src, W, H).filter(ImageFilter.GaussianBlur(42))).enhance(0.5)
    if hook:
        d = ImageDraw.Draw(bg)
        font, size, lines = hook_lines(d, hook)
        step = round(size * 1.18)
        y = HOOK_TOP + ((HOOK_BOTTOM - HOOK_TOP) - step * len(lines)) // 2
        for k, line in enumerate(lines):
            box = d.textbbox((0, 0), line, font=font)
            fill = YELLOW if (k == len(lines) - 1 and len(lines) > 1) else WHITE
            d.text(((W - (box[2] - box[0])) // 2, y - box[1] + (step - (box[3] - box[1])) // 2), line, font=font,
                   fill=fill, stroke_width=max(5, size // 12), stroke_fill=(10, 10, 10))
            y += step
    return bg


def build_clip(img, out, frames: int, motion: int, hook: str = "") -> None:
    """One picture as a vertical clip: the picture across the middle with a slow push-in or pan."""
    from PIL import Image

    W, H, FPS = config.SHORT_WIDTH, config.SHORT_HEIGHT, config.FPS
    src = Image.open(img).convert("RGB")
    sw, sh_ = src.size
    fh = round(sh_ * W / sw)
    bg = _backdrop(src, hook)
    move = SHORT_MOTIONS[motion % len(SHORT_MOTIONS)]
    cmd = [find_ffmpeg()[0], "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for k in range(max(frames, 1)):
            zoom, px, py = move(k / max(frames - 1, 1))
            cw, ch = sw / zoom, sh_ / zoom
            x0, y0 = (sw - cw) * px, (sh_ - ch) * py
            frame = bg.copy()
            frame.paste(src.resize((W, fh), Image.BICUBIC, box=(x0, y0, x0 + cw, y0 + ch)), (0, FG_TOP))
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait() != 0:
            raise StageError(f"ffmpeg failed building a Short clip:\n{err[-1500:]}")
    except BrokenPipeError:
        raise StageError(f"ffmpeg stopped while building a Short clip:\n{proc.stderr.read().decode(errors='replace')[-1500:]}")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stderr.close()


def end_card_image(path, title: str) -> None:
    """The logo on its own background colour, 'Full breakdown on the channel', and the full video's title."""
    from PIL import Image, ImageDraw
    from .s8_thumbnail import _font, _wrap

    W, H = config.SHORT_WIDTH, config.SHORT_HEIGHT
    raw = Image.open(config.LOGO).convert("RGBA")
    logo = Image.new("RGB", raw.size, (255, 255, 255))
    logo.paste(raw, mask=raw.split()[3])
    bg = logo.getpixel((2, 2))
    light = (0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]) > 140
    ink = (16, 27, 45) if light else (255, 255, 255)
    card = Image.new("RGB", (W, H), bg)
    size = 560
    card.paste(logo.resize((size, size), Image.LANCZOS), ((W - size) // 2, 330))
    d = ImageDraw.Draw(card)
    y = 330 + size + 40
    name_font = _font(76)
    d.text(((W - d.textlength(config.CHANNEL_NAME, font=name_font)) / 2, y), config.CHANNEL_NAME, font=name_font, fill=ink)
    y += 130
    label_font = _font(44)
    label = "Full breakdown on the channel"
    d.text(((W - d.textlength(label, font=label_font)) / 2, y), label, font=label_font, fill=ink)
    y += 80
    t_font = _font(46)
    for line in _wrap(d, title, t_font, W - 160)[:3]:
        d.text(((W - d.textlength(line, font=t_font)) / 2, y), line, font=t_font, fill=ink)
        y += 62
    card.save(path)


def build_end_card(out, title: str) -> None:
    png = out.with_suffix(".png")
    end_card_image(png, title)
    sh([find_ffmpeg()[0], "-y", "-v", "error", "-loop", "1", "-framerate", str(config.FPS), "-i", str(png),
        "-frames:v", str(max(1, round(config.SHORT_END_CARD_S * config.FPS))), "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "18", "-pix_fmt", "yuv420p", str(out)])


def _lufs(path, start: float | None = None, length: float | None = None) -> float | None:
    from .s10_qc import _ff
    args = (["-ss", f"{start:.3f}", "-t", f"{length:.3f}"] if start is not None else []) + ["-i", str(path)]
    out = _ff(args + ["-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out, re.S)
    try:
        v = float(json.loads(m.group(0))["input_i"]) if m else None
    except ValueError:
        return None
    return v if v is not None and math.isfinite(v) and v > -70 else None


def record_question(video: Video, question: str, d, stub: bool = False):
    """The opening question in the video's own narrator voice, trimmed and matched in loudness to the narration
    that follows. Cached by wording, so it is recorded (about a cent) only once per question."""
    from . import s3_voiceover as v3
    tl = video.read_json("timeline.json")
    provider, voice = tl.get("provider", config.TTS_PROVIDER), tl.get("voice", config.GEMINI_TTS_VOICE)
    key = hashlib.sha1(f"{provider}|{voice}|{config.GEMINI_TTS_STYLE}|{question}".encode()).hexdigest()[:10]
    raw = d / f"question_{key}.wav"
    if not raw.exists():
        for old in d.glob("question_*.wav"):
            retire(old)
        if stub or provider == "stub":
            pcm = v3._stub_pcm(question)
        elif provider == "gemini":
            if not config.GOOGLE_API_KEY:
                raise StageError("GOOGLE_API_KEY is empty, and the opening question has to be recorded. Put it in .env (README).")
            log(f"  recording the opening question in the {voice} voice (about a cent)")
            pcm = v3._gemini(question, voice=voice)
        else:
            pcm = v3._synthesize(question)
        v3._write_wav(raw, v3._strip_wav_header(pcm))
    return raw


def question_word_times(path, question: str, length: float, stub: bool = False) -> list | None:
    """When each word of the spoken opening question is said, heard by the speech recognizer. The question is a fresh
    recording with no timings of its own; spreading its words evenly put its captions up to a second off."""
    from . import align
    if stub or not align.available() or not path.exists():
        return None
    try:
        heard = align.transcribe(path)
    except Exception as e:  # a recognizer problem must not stop the Short; even spreading is the fallback
        log(f"  WARNING: couldn't time the opening question's captions ({e}); they are spread evenly.")
        return None
    times = align.match([{"text": question}], heard, length)
    return times[0] if times and len(times[0]) == len(question.split()) else None


def _audio(video: Video, segs: list[dict], out, loud: bool, question=None) -> float:
    """Cut and join the narration (after the opening question, if any). Returns the question's length with its pause."""
    ffmpeg = find_ffmpeg()[0]
    parts, inputs, q_len = [], ["-i", str(video.dir / "voiceover.wav")], 0.0
    if question is not None:
        qa = question.with_name("question_trimmed.wav")
        trim = "" if not loud else ("silenceremove=start_periods=1:start_threshold=-45dB,areverse,"
                                    "silenceremove=start_periods=1:start_threshold=-45dB,areverse,")
        gain = 0.0
        if loud:
            first = segs[0]
            target, have = _lufs(video.dir / "voiceover.wav", first["cut"], max(first["length"], 3.0)), _lufs(question)
            if target is not None and have is not None:
                gain = max(-12.0, min(12.0, target - have))
        sh([ffmpeg, "-y", "-v", "error", "-i", str(question), "-af",
            f"{trim}volume={gain:.2f}dB,aresample=24000,afade=t=in:d=0.01,apad=pad_dur={QUESTION_GAP_S}",
            "-ac", "1", str(qa)])
        q_len = media_duration(qa)
        inputs += ["-i", str(qa)]
        parts.append("[1:a]aresample=24000,asetpts=PTS-STARTPTS[q]")
    for k, s in enumerate(segs):
        a, b = s["cut"], s["cut"] + (s["length"] - GAP_S)
        parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.01,"
                     f"afade=t=out:st={max(b - a - 0.03, 0):.3f}:d=0.03,apad=pad_dur={GAP_S}[a{k}]")
    tail = ",loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000" if loud else ",aresample=48000"
    labels = ("[q]" if question is not None else "") + "".join(f"[a{k}]" for k in range(len(segs)))
    n = len(segs) + (1 if question is not None else 0)
    graph = ";".join(parts) + ";" + labels + f"concat=n={n}:v=0:a=1{tail}[out]"
    sh([ffmpeg, "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "[out]", str(out)])
    return q_len


def build_short(video: Video, n: int, item: dict, stub: bool = False) -> dict:
    ffmpeg, can_burn = find_ffmpeg()
    if not ffmpeg:
        raise StageError("No usable ffmpeg found. On your Mac: brew install ffmpeg")
    if config.SHORT_END_CARD_S > 0 and not config.LOGO.exists():
        raise StageError(f"The channel logo is missing ({config.LOGO}). The end card needs it (or set SHORT_END_CARD_S to 0).")
    tl = video.read_json("timeline.json")
    shots = video.read_json("shots.json")["shots"]
    by_i = {s["i"]: s for s in tl["sentences"]}
    d = video.path("shorts", f"short_{n}", "x").parent
    segs, offset = [], 0.0
    for i in item["sentences"]:
        s = by_i[i]
        a, _ = cut_points(s)
        L = length_of(s)
        segs.append({"i": i, "text": s["text"], "words": s.get("words"), "cut": a, "length": L, "offset": offset,
                     "shot": _shot_of(s, shots)})
        offset += L

    voice = d / "voice.wav"
    loud = not (stub or tl.get("provider") == "stub")
    question = item.get("opening_question")
    q_wav = record_question(video, question, d, stub=stub) if question else None
    q_len = _audio(video, segs, voice, loud=loud, question=q_wav)
    total = media_duration(voice)
    scale = (total - q_len) / offset if offset else 1.0
    for s in segs:  # the cut audio can differ from the plan by a few milliseconds
        s["offset"] = s["offset"] * scale + q_len
        s["length"] *= scale
    caption_parts = list(segs)
    if question:  # the question's captions are timed from what is heard in its recording (evenly spread if that fails)
        q_words = question_word_times(d / "question_trimmed.wav", question, q_len, stub=not loud)
        caption_parts.insert(0, {"text": question, "words": q_words, "cut": 0.0, "offset": 0.0, "length": q_len})
    write_srt(caption_parts, d / "captions.srt")

    # One picture per run of sentences that share a shot, re-framed at least every HOLD_S seconds.
    groups = []
    for s in segs:
        if groups and groups[-1]["shot"]["id"] == s["shot"]["id"]:
            groups[-1]["end"] = s["offset"] + s["length"]
        else:
            groups.append({"shot": s["shot"], "start": s["offset"], "end": s["offset"] + s["length"]})
    groups[0]["start"] = 0.0  # the first picture also covers the opening question
    jobs, motion = [], 0
    for g in groups:
        img = video.dir / "images" / f"shot_{g['shot']['id']:03d}.png"
        if not img.exists():
            raise StageError(f"Image for shot {g['shot']['id']} is missing. Run: python -m pipeline images {video.slug}")
        parts = max(1, math.ceil((g["end"] - g["start"]) / HOLD_S))
        for k in range(parts):
            t0 = g["start"] + (g["end"] - g["start"]) * k / parts
            t1 = g["start"] + (g["end"] - g["start"]) * (k + 1) / parts
            f0, f1 = round(t0 * config.FPS), round(t1 * config.FPS)
            jobs.append((img, max(f1 - f0, 1), motion))
            motion += 1
    clips_dir = d / "clips"
    clips_dir.mkdir(exist_ok=True)
    todo, clips = [], []
    for idx, (img, frames, mot) in enumerate(jobs):
        st = img.stat()
        key = hashlib.sha1(f"{CLIP_VERSION}|{st.st_size}|{st.st_mtime_ns}|{frames}|{mot % len(MOTIONS)}|{item['hook_text']}".encode()).hexdigest()[:10]
        clip = clips_dir / f"{idx:02d}_{key}.mp4"
        clips.append(clip)
        if not clip.exists():
            todo.append((img, clip, frames, mot, item["hook_text"]))
    if config.SHORT_END_CARD_S > 0:  # off by default: Shorts that send viewers elsewhere stop being shown
        ttl = video.read_json("metadata.json").get("title", "") if (video.dir / "metadata.json").exists() else video.brief().get("topic", "")
        card = clips_dir / f"end_{hashlib.sha1((CLIP_VERSION + ttl + str(config.LOGO.stat().st_mtime_ns)).encode()).hexdigest()[:10]}.mp4"
        if not card.exists():
            build_end_card(card, ttl)
        clips.append(card)
    for old in clips_dir.glob("*.mp4"):
        if old not in clips:
            retire(old)
    for old in clips_dir.glob("*.png"):
        retire(old)
    log(f"  short {n}: {len(segs)} sentences, {total:.0f}s of voice; building {len(todo)} clips")
    workers = max(1, min(4, (os.cpu_count() or 2) - 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in [pool.submit(build_clip, *job) for job in todo]:
            fut.result()
    (clips_dir / "list.txt").write_text("".join(f"file '{c.name}'\n" for c in clips))

    burn = can_burn
    if not burn:
        log("  WARNING: your ffmpeg can't burn in captions, so this Short has none. Shorts need captions: "
            "brew install ffmpeg-full, then run the shorts command again.")
    cmd = [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "clips/list.txt", "-i", "voice.wav"]
    if burn:
        cmd += ["-vf", f"subtitles=captions.srt:force_style='{CAPTION_STYLE}'"]
    cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-af", "apad", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", "-movflags", "+faststart", "short.mp4"]
    sh(cmd, cwd=d)
    info = {"burned_captions": burn, "end_card_seconds": config.SHORT_END_CARD_S, "voice_seconds": round(total, 2),
            "opening_question": question or "", "question_seconds": round(q_len, 2),
            "shots": sorted({s["shot"]["id"] for s in segs})}
    (d / "assembly.json").write_text(json.dumps(info, indent=2) + "\n")
    return info


def description(item: dict, parent_title: str, sources_md: str) -> str:
    parts = [item["description_intro"].strip(), "", f"Full video: {parent_title}", "",
             "Sources (from the full video):", sources_md.strip(), "", config.DISCLAIMER, "", "#shorts"]
    return "\n".join(parts).strip() + "\n"


# --- checking -----------------------------------------------------------------

def qc_short(video: Video, n: int, stub: bool = False) -> dict:
    from .s10_qc import LOUDNESS_FAIL, LOUDNESS_TARGET, PEAK_MAX_DBTP, SILENCE_MAX_S, _ff

    d = video.dir / "shorts" / f"short_{n}"
    mp4 = str(d / "short.mp4")
    failures, warnings, checks = [], [], {}
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height:format=duration", "-of", "json", mp4], capture_output=True, text=True)
    try:
        info = json.loads(probe.stdout)
        w, h = info["streams"][0]["width"], info["streams"][0]["height"]
        dur = float(info["format"]["duration"])
    except (ValueError, KeyError, IndexError):
        return {"passed": False, "failures": ["Couldn't read short.mp4 (ffprobe failed). Run the shorts command again."], "warnings": [], "checks": {}}
    checks.update(resolution=f"{w}x{h}", duration=round(dur, 1))
    if (w, h) != (config.SHORT_WIDTH, config.SHORT_HEIGHT):
        failures.append(f"The Short is {w}x{h}, not {config.SHORT_WIDTH}x{config.SHORT_HEIGHT}.")
    if dur > 60.5 + config.SHORT_END_CARD_S:
        failures.append(f"The Short is {dur:.0f} seconds; keep it under about a minute.")
    asm = json.loads((d / "assembly.json").read_text()) if (d / "assembly.json").exists() else {}
    if not asm.get("burned_captions"):
        failures.append("No captions are burned in. Most Shorts are watched on mute: install ffmpeg-full and run the shorts command again.")
    if not stub:
        out = _ff(["-i", mp4, "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"])
        m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out, re.S)
        if m:
            ln = json.loads(m.group(0))
            lufs, peak = float(ln["input_i"]), float(ln["input_tp"])
            checks.update(loudness_lufs=lufs, true_peak_dbtp=peak)
            if not LOUDNESS_FAIL[0] <= lufs <= LOUDNESS_FAIL[1]:
                failures.append(f"Loudness is {lufs:.1f} LUFS; it will sound far too {'quiet' if lufs < -14 else 'loud'}.")
            elif not LOUDNESS_TARGET[0] <= lufs <= LOUDNESS_TARGET[1]:
                warnings.append(f"Loudness is {lufs:.1f} LUFS; about -14 is ideal.")
            if peak > PEAK_MAX_DBTP:
                warnings.append(f"Audio peaks at {peak:.1f} dBTP and may clip.")
        out = _ff(["-i", mp4, "-vn", "-af", f"silencedetect=n=-45dB:d={SILENCE_MAX_S}", "-f", "null", "-"])
        for start, length in re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", out):
            s = float(start) - float(length)
            if s < dur - config.SHORT_END_CARD_S - 1.0:
                failures.append(f"A long silence at {fmt_ts(s)} ({float(length):.1f}s).")
    out = _ff(["-i", mp4, "-an", "-vf", "blackdetect=d=0.4:pix_th=0.10", "-f", "null", "-"])
    blacks = [fmt_ts(float(s)) for s in re.findall(r"black_start:([\d.]+)", out)]
    if blacks:
        failures.append("Black frames at " + ", ".join(blacks[:5]) + ".")
    # Pictures were checked in the full video's quality check; warn when a Short uses one it flagged.
    qc = video.dir / "qc.json"
    if qc.exists():
        flagged = {int(x) for t in json.loads(qc.read_text()).get("warnings", []) + json.loads(qc.read_text()).get("failures", [])
                   for x in re.findall(r"\bShot (\d+)\b", t)}
        used = set(asm.get("shots", []))
        for sid in sorted(flagged & used):
            warnings.append(f"Uses shot {sid}, which the full video's quality check flagged (see qc.md). Look at it before publishing.")
    result = {"passed": not failures, "failures": failures, "warnings": warnings, "checks": checks}
    (d / "qc.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


# --- the command ----------------------------------------------------------------

def _plan_current(video: Video, plan_path) -> dict | None:
    if not plan_path.exists():
        return None
    plan = json.loads(plan_path.read_text())
    sha = hashlib.sha1(video.read_text("script.txt").encode()).hexdigest()[:12]
    fresh = (plan.get("version") == PLAN_VERSION and plan.get("script_sha") == sha
             and plan.get("voice_seconds") == round(video.read_json("timeline.json")["duration"], 1))
    return plan if fresh else None


def run(video: Video, count: int = config.SHORTS_COUNT, only: int | None = None, plan_only: bool = False,
        replan: bool = False, stub: bool = False, force: bool = False) -> None:
    if not 2 <= count <= 4:
        raise StageError("Choose 2, 3 or 4 Shorts per video (--count).")
    fc = video.dir / "factcheck.json"
    if not force and not (fc.exists() and json.loads(fc.read_text()).get("passed")):
        raise StageError("The video's fact-check hasn't passed, and every Short is cut from its script. Fix that first (or --force).")
    for need in ("timeline.json", "shots.json", "voiceover.wav"):
        if not (video.dir / need).exists():
            raise StageError(f"{need} is missing. Run the full video pipeline first: python -m pipeline run {video.slug}")
    tl = video.read_json("timeline.json")
    plan_path = video.path("shorts", "plan.json")
    plan = None if replan else _plan_current(video, plan_path)
    if plan:
        log(f"  using the Shorts plan in out/{video.slug}/shorts/plan.json (add --replan to choose again)")
    else:
        items = choose(video, count, stub=stub)
        plan = {"version": PLAN_VERSION, "script_sha": hashlib.sha1(video.read_text("script.txt").encode()).hexdigest()[:12],
                "voice_seconds": round(tl["duration"], 1), "shorts": items}
        log(f"  planned {len(items)} Shorts:")
        for n, it in enumerate(items, start=1):
            log(f"    {n}. {it['title']}  ({it['seconds']:.0f}s)  on screen: \"{it['hook_text']}\"")
    # Every Short that will be built opens on a spoken question; plans made before that get one now.
    targets = [n for n, it in enumerate(plan["shorts"], start=1)
               if (not only or n == only) and it.get("review", {}).get("verdict") != "REJECT"]
    write_questions(video, plan, targets, stub=stub)
    # Re-check the written lines whenever they changed (including hand edits to plan.json).
    by_i = {s["i"]: s for s in tl["sentences"]}
    for n, it in enumerate(plan["shorts"], start=1):
        rule = written_problems(f"Short {n}", " ".join(by_i[i]["text"] for i in it["sentences"]), it.get("hook_text", ""),
                                it.get("title", ""), it.get("description_intro", ""), it.get("opening_question"))
        if rule:
            raise StageError("plan.json breaks the rules:\n  " + "\n  ".join(rule))
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    log("  checking each Short's on-screen text, title and description (separate Claude check)...")
    review(video, plan, stub=stub)
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    for n, it in enumerate(plan["shorts"], start=1):
        r = it["review"]
        log(f"    short {n}: {r['verdict']}" + ("" if r["verdict"] == "PASS" else "  " + "; ".join(r["problems"])))
    if plan_only:
        write_review(video, plan, json.loads((video.dir / "metadata.json").read_text()).get("title", "")
                     if (video.dir / "metadata.json").exists() else "")
        return
    parent_title = json.loads((video.dir / "metadata.json").read_text()).get("title", "") if (video.dir / "metadata.json").exists() else video.brief().get("topic", "")
    sources_md = (video.dir / "sources.md").read_text() if (video.dir / "sources.md").exists() else ""
    results = {}
    for n, item in enumerate(plan["shorts"], start=1):
        if only and n != only:
            continue
        if item["review"]["verdict"] != "PASS":
            old = video.dir / "shorts" / f"short_{n}" / "short.mp4"
            if old.exists():  # never leave an earlier build of an unapproved Short lying around to be uploaded
                retire(old)
            log(f"  short {n}: NOT built, the check did not pass it (see shorts/REVIEW.md). Edit its lines in plan.json and "
                f"run again, or --replan.")
            continue
        build_short(video, n, item, stub=stub)
        d = video.dir / "shorts" / f"short_{n}"
        meta = {"title": item["title"], "opening_question": item.get("opening_question", ""), "hook_text": item["hook_text"], "tags": item["tags"], "sentences": item["sentences"], "why": item["why"],
                "parent_title": parent_title}
        (d / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        (d / "description.txt").write_text(description(item, parent_title, sources_md))
        results[n] = qc_short(video, n, stub=stub)
        log(f"  short {n} quality check: {'PASSED' if results[n]['passed'] else 'FAILED'}")
        for f in results[n]["failures"]:
            log(f"    FAIL  {f}")
    write_review(video, plan, parent_title)
    log(f"\nDone. Open out/{video.slug}/shorts/REVIEW.md")


def write_review(video: Video, plan: dict, parent_title: str) -> None:
    lines = [f"# Shorts for: {parent_title}", "",
             "Each Short is made only of sentences from the approved script, in the narrator's own voice. The on-screen text, "
             "title and description were checked by a separate Claude review. Watch them, then upload one at a time, a few "
             "days apart. When you upload, set the full video as the Related video.", ""]
    for n, item in enumerate(plan["shorts"], start=1):
        d = video.dir / "shorts" / f"short_{n}"
        r = item.get("review", {})
        lines += [f"## Short {n}: {item['title']}", "", f"- Spoken first: *{item.get('opening_question') or '(no opening question)'}*",
                  f"- On screen: **{item['hook_text']}**",
                  f"- Text check: {r.get('verdict', 'not run')}" + (" (lines rewritten by the check)" if item.get("review_history") else "")]
        lines += [f"  - {p}" for p in r.get("problems", [])]
        lines += [f"- Length: about {item['seconds']:.0f} seconds",
                  f"- Why it works: {item['why'] or 'n/a'}", f"- File: shorts/short_{n}/short.mp4",
                  f"- Description: shorts/short_{n}/description.txt", f"- Tags: {', '.join(item['tags'])}"]
        qc = d / "qc.json"
        if r.get("verdict") != "PASS":
            lines.append("- Not built: the text check did not pass it. Do not upload anything from this folder.")
        elif qc.exists():
            r = json.loads(qc.read_text())
            lines.append(f"- Quality check: {'PASSED' if r['passed'] else 'FAILED'}")
            lines += [f"  - FAIL: {f}" for f in r["failures"]] + [f"  - note: {w}" for w in r["warnings"]]
        lines += ["", "Narration:", "", f"> {(item.get('opening_question') + ' ') if item.get('opening_question') else ''}{item['text']}", ""]
    video.path("shorts", "REVIEW.md").write_text("\n".join(lines))
