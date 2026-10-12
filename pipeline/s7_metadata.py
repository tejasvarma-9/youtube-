"""Stage 7: title, thumbnail text, description, tags and chapters.

Writes metadata.json and description.txt (ready to paste into YouTube).
"""

from __future__ import annotations

import hashlib

from . import config, llm
from .common import StageError, Video, extract_json, fill, fmt_ts, log, prompt, voice_profile
from .lint import lint_title, validate_chapters


def run(video: Video, stub: bool = False) -> None:
    tl = video.read_json("timeline.json")
    sents = tl["sentences"]
    timed = "\n".join(f"[{s['i']}] ({fmt_ts(s['start'])}) {s['text']}" for s in sents)
    script_sha = hashlib.sha1(video.read_text("script.txt").encode()).hexdigest()[:12]
    old = video.read_json("metadata.json") if video.path("metadata.json").exists() else {}
    same_script = old.get("script_sha") == script_sha
    same_shape = old.get("sentence_count") == len(sents)  # reworded sentences, same number of them
    if old.get("chapter_sentences") and (same_script or same_shape):
        # Keep the title, thumbnail text and description (including your edits);
        # only the chapter times are re-read from the new timeline.
        meta = old
        meta["script_sha"] = script_sha
        meta["sentence_count"] = len(sents)
        log("  keeping the title, thumbnail text and description in metadata.json (script wording edited, same sentences)"
            if not same_script else "  keeping the title, thumbnail text and description in metadata.json (script unchanged)")
    else:
        text = fill(prompt("metadata.md"), VOICE=voice_profile(), TIMED_SCRIPT=timed, SOURCES=video.read_text("sources.txt"))
        answer = llm.ask(text, "metadata", stub=stub, context={"sentence_count": len(sents)})
        video.path("raw", "metadata_answer.md").write_text(answer)
        meta = extract_json(answer)
        for key in ("title", "thumbnail_text", "description_intro", "chapters"):
            if not meta.get(key):
                raise StageError(f"Metadata is missing '{key}'. See raw/metadata_answer.md and re-run.")
        meta["chapter_sentences"] = [{"sentence": int(c["sentence"]), "title": c["title"].strip()} for c in meta["chapters"]]
        meta["script_sha"] = script_sha
        meta["sentence_count"] = len(sents)

    by_index = {s["i"]: s for s in sents}
    chapters = []
    for c in sorted(meta["chapter_sentences"], key=lambda c: int(c["sentence"])):
        s = by_index.get(int(c["sentence"]))
        if s:
            chapters.append({"start": 0.0 if not chapters else s["start"], "title": c["title"].strip()})
    if chapters:
        chapters[0]["start"] = 0.0
    meta["chapters"] = chapters
    meta["chapter_problems"] = validate_chapters(chapters, tl["duration"])
    meta["title_problems"] = lint_title(meta["title"], meta["thumbnail_text"])
    meta["thumbnail_text"] = meta["thumbnail_text"].upper()

    description = build_description(meta, video.read_text("sources.md"))
    meta["description"] = description
    video.write_json("metadata.json", meta)
    video.path("description.txt").write_text(description)
    log(f"  title: {meta['title']}")
    for p in meta["title_problems"] + meta["chapter_problems"]:
        log(f"    warn   {p}")


def build_description(meta: dict, sources_md: str) -> str:
    parts = [meta["description_intro"].strip(), ""]
    if not meta.get("chapter_problems"):
        parts += [f"{fmt_ts(c['start'])} {c['title']}" for c in meta["chapters"]] + [""]
    parts += ["Sources:", sources_md.strip(), "", config.DISCLAIMER]
    return "\n".join(parts).strip() + "\n"
