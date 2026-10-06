"""Rule checks that need no model: script format and policy, titles, chapters."""

from __future__ import annotations

import re

from . import config
from .common import word_count

BANNED = [
    (r"\b(hit the bell|smash that)\b", "no engagement bait"),
    (r"\bsponsor(ed)? by\b|\btoday'?s sponsor\b", "no sponsor reads"),
    (r"\b(welcome back|hey guys|hi everyone|in this video)\b", "no filler greeting"),
    (r"\byou will (earn|make)\b|\bguaranteed (income|returns?)\b", "never promise viewers income"),
    (r"\b(you should|go) (buy|sell|short)\b|\b(buy|sell|hold) (the|this) stock\b|\bgood investment\b", "no buy/sell/hold advice"),
    (r"\b(the market|stocks?) will (crash|rise|fall|soar)\b", "no market predictions"),
    (r"\bso you want to own\b", "that title format belongs to another channel"),
]

FORMAT = [
    (r"^\s*#", "headings"),
    (r"^\s*([-*•]|\d+[.)])\s", "bullets or numbered list lines"),
    (r"\[(music|sfx|scene|cut|b-?roll)[^\]]*\]", "scene or music tags"),
    (r"\b\d{1,2}:\d{2}\b", "timestamps"),
]

# "$2-5 million", "$2 to5 million", "$2M-$5M": ranges the voice will read badly.
GARBLED_RANGE = re.compile(r"\$\d[\d,.]*\s*(?:-|–|to)\s*\d[\d,.]*\s*(million|billion|thousand|k|m|bn)\b|\$\d[\d,.]*\s*(?:to)\d|\$\d[\d,.]*[kKmMbB]\b")
NUMBER = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")


def _numbers(text: str) -> set[str]:
    out = set()
    for raw in NUMBER.findall(text):
        n = raw.replace(",", "").replace("$", "").rstrip("%").rstrip(".")
        if not n:
            continue
        if re.fullmatch(r"(19|20)\d\d", n):  # years are dates, not figures
            continue
        if re.fullmatch(r"\d", n) and "$" not in raw and "%" not in raw:  # "3 reasons"
            continue
        out.add(n)
    return out


ROADMAP = re.compile(r"\bby the end of (?:this|the) video\b|\byou(?:'ll| will) (?:see|learn|understand|find out)\b|\bwe(?:'ll| will) (?:look at|cover|explore|break down|walk through)\b|\bin today'?s video\b", re.I)


def _opening(script: str) -> tuple[list[str], list[str]]:
    """No channel intro, no roadmap paragraph, and the video's question asked within about
    30 seconds (90 words). Costco (2026-10-04) gave its answer in the first sentence, then spent
    about 27 seconds on "by the end of this video..." and lost most viewers in the first minute."""
    name = config.CHANNEL_NAME
    errors, warnings = [], []
    paragraphs = [p for p in script.split("\n\n") if p.strip()]
    body = "\n\n".join(paragraphs[:-1]) if len(paragraphs) > 1 else script
    if name.lower() in " ".join(body.split()[:150]).lower():
        errors.append(f"Opening: don't say the channel name \"{name}\" in the opening. Go straight from the hook into the question; the name belongs only in the final subscribe line.")
    for m in ROADMAP.finditer(" ".join(script.split()[:250])):
        warnings.append(f"Opening: \"{m.group(0)}\" starts a roadmap of what the video will cover. Cut it and go straight into the story.")
    if "?" not in " ".join(script.split()[:90]):
        warnings.append("Opening: ask the video's core question within the first 90 words (about 30 seconds).")
    return errors, warnings


def _outro(script: str) -> list[str]:
    """One like-and-subscribe line closes the video."""
    name = config.CHANNEL_NAME
    out = []
    paragraphs = [p for p in script.split("\n\n") if p.strip()]
    subs = [i for i, p in enumerate(paragraphs) if re.search(r"\bsubscribe\b", p, re.I)]
    last = len(paragraphs) - 1
    if not subs:
        out.append(f"Outro: end with one sentence asking viewers to like the video and subscribe to {name}.")
    elif subs != [last]:
        out.append("Outro: the like-and-subscribe line belongs only in the final paragraph, not in the middle of the video.")
    elif name.lower() not in paragraphs[last].lower() or not re.search(r"\blike\b", paragraphs[last], re.I):
        out.append(f"Outro: the final line must ask viewers to like the video and subscribe to {name}.")
    elif len(re.findall(r"\bsubscribe\b", script, re.I)) > 1:
        out.append("Outro: ask for the subscribe once, not twice.")
    return out


HOOK_CUE = re.compile(r"\?|\bbut here'?s\b|\bwhat (?:happens|comes) next\b|\bnext\b|\blater\b|\bthe (?:real|hard|catch|twist)\b", re.I)


def _hooks(script: str) -> list[str]:
    """Warn when the middle or the end has no hook cue; the writer is told to add all three."""
    out = []
    paragraphs = [p for p in script.split("\n\n") if p.strip()]
    total = word_count(script)
    if total < 200 or len(paragraphs) < 4:
        return out
    seen, middle = 0, []
    for p in paragraphs:
        w = word_count(p)
        if 0.4 * total <= seen + w / 2 <= 0.65 * total:
            middle.append(p)
        seen += w
    if middle and not any(HOOK_CUE.search(p) for p in middle):
        out.append("Hook: nothing around the halfway mark opens a new question or teases what is coming; add a middle hook.")
    if not HOOK_CUE.search(paragraphs[-2]):
        out.append("Hook: the paragraph before the final subscribe line should end with a hook (a last question or a pointer to the next video).")
    return out


def lint_script(script: str, facts: str) -> dict:
    errors, warnings = [], []
    words = word_count(script)
    if words < config.WORDS_MIN * 0.9 or words > config.WORDS_MAX * 1.1:
        warnings.append(f"Length is {words} words; the target is {config.WORDS_MIN} to {config.WORDS_MAX}.")
    for pat, why in BANNED:
        for m in re.finditer(pat, script, re.I):
            errors.append(f"Policy: \"{m.group(0)}\" ({why}).")
    for pat, what in FORMAT:
        if re.search(pat, script, re.I | re.M):
            errors.append(f"Format: the script contains {what}; it must be clean prose.")
    for m in GARBLED_RANGE.finditer(script):
        errors.append(f"Number format: \"{m.group(0)}\" will be read badly; write it out in full, like \"$2 million to $5 million\".")

    opening_errors, opening_warnings = _opening(script)
    errors += opening_errors + _outro(script)
    warnings += opening_warnings
    warnings += _hooks(script)

    fact_numbers = _numbers(facts)
    missing = sorted(_numbers(script) - fact_numbers, key=lambda s: float(s) if s.replace(".", "").isdigit() else 0)
    for n in missing:
        errors.append(f"Unchecked figure: {n} appears in the script but not in the fact list.")

    for line in facts.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if line.strip() and (len(parts) < 3 or not re.fullmatch(r"S\d+|ESTIMATE", parts[2], re.I)):
            errors.append(f"Fact line has no source or ESTIMATE tag: {line.strip()[:120]}")
    return {"words": words, "errors": errors, "warnings": warnings}


def lint_title(title: str, thumb_text: str) -> list[str]:
    """Title and thumbnail text checked as a pair."""
    problems = []
    if len(title) > 60:
        problems.append(f"Title is {len(title)} characters; keep it under 60 so it isn't cut off.")
    if len(title) > 40 and not re.search(r"\b[A-Z][a-z]+", title[:40]):
        problems.append("The subject should appear in the first 40 characters.")
    if re.search(r"so you want to own", title, re.I):
        problems.append("\"So You Want to Own\" is another channel's format.")
    if re.search(r"[!]{2,}|\b(SHOCKING|INSANE|YOU WON'T BELIEVE)\b", title, re.I):
        problems.append("Clickbait wording; the channel's tone is dry and confident.")
    tw = len(thumb_text.split())
    if not 2 <= tw <= 6:
        problems.append(f"Thumbnail text has {tw} words; aim for 2 to 4.")
    stop = {"the", "a", "an", "of", "to", "how", "why", "is", "for", "who", "does", "and", "in", "on"}
    title_words = {w.lower() for w in re.findall(r"[A-Za-z']+", title)} - stop
    thumb_words = {w.lower() for w in re.findall(r"[A-Za-z']+", thumb_text)} - stop
    overlap = title_words & thumb_words
    if len(overlap) >= 2:
        problems.append(f"Thumbnail text repeats the title ({', '.join(sorted(overlap))}); it should add something new.")
    return problems


def validate_chapters(chapters: list[dict], duration: float) -> list[str]:
    """YouTube's rules: first at 0:00, at least 3, each at least 10 seconds long."""
    problems = []
    if len(chapters) < 3:
        problems.append("YouTube needs at least 3 chapters.")
    if chapters and chapters[0]["start"] != 0:
        problems.append("The first chapter must start at 0:00.")
    for a, b in zip(chapters, chapters[1:] + [{"start": duration}]):
        if b["start"] - a["start"] < 10:
            problems.append(f"Chapter \"{a['title']}\" is shorter than 10 seconds.")
    return problems
