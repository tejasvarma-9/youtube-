"""Stage 1: research and write the script.

Writes script.txt (clean prose for the voice), facts.txt, sources.txt and script_lint.json.
"""

from __future__ import annotations

from . import config, llm
from .common import StageError, Video, fill, log, parse_sections, prompt, voice_profile
from .lint import lint_script


def run(video: Video, stub: bool = False) -> None:
    b = video.brief()
    text = fill(
        prompt("script.md"),
        VOICE=voice_profile(),
        TOPIC=b["topic"],
        ANGLE=b.get("angle") or "Find one: a calculation, a comparison across two companies, or a contrarian conclusion. State it in the first minute.",
        WORDS=str(b.get("words", config.TARGET_WORDS)),
        CHARACTER=b.get("character") or "none",
        NOTES=b.get("notes") or "none",
    )
    video.path("raw", "script_prompt.md").write_text(text)
    answer = llm.ask(text, "script", web=True, stub=stub)
    video.path("raw", "script_answer.md").write_text(answer)

    parts = parse_sections(answer, ["SCRIPT", "FACTS", "SOURCES"])
    if not parts["SCRIPT"]:
        raise StageError("The script came back empty. See raw/script_answer.md.")
    video.path("script.txt").write_text(parts["SCRIPT"].strip() + "\n")
    video.path("facts.txt").write_text(parts["FACTS"].strip() + "\n")
    video.path("sources.txt").write_text(parts["SOURCES"].strip() + "\n")
    result = check(video)
    if result["words"] < config.WORDS_MIN and not stub:
        _extend(video, result["words"])


def _extend(video: Video, words: int) -> None:
    """A short script gets padded later, during fact-check fixes, and padding is what fails fact-checks. So it is
    lengthened once now, with new sourced material, while nothing has been checked or recorded yet."""
    b = video.brief()
    log(f"  script is {words} words, under {config.WORDS_MIN}: asking for new sourced material once")
    text = fill(prompt("extend.md"), VOICE=voice_profile(), TOPIC=b["topic"], ANGLE=b.get("angle") or "",
                NOTES=b.get("notes") or "none", WORDS=str(words), MIN=str(config.WORDS_MIN), MAX=str(config.WORDS_MAX),
                SCRIPT=video.read_text("script.txt"), FACTS=video.read_text("facts.txt"), SOURCES=video.read_text("sources.txt"))
    try:
        answer = llm.ask(text, "script", web=True)
    except StageError as e:
        log(f"  could not lengthen the script ({str(e)[:200]}); keeping it as it is")
        return
    video.path("raw", "extend_answer.md").write_text(answer)
    parts = parse_sections(answer, ["SCRIPT", "FACTS", "SOURCES"])
    new = (parts.get("SCRIPT") or "").strip()
    old_lint = lint_script(video.read_text("script.txt"), video.read_text("facts.txt"))
    new_lint = lint_script(new, (parts.get("FACTS") or "").strip()) if new else None
    if not new or new_lint["words"] <= words or len(new_lint["errors"]) > len(old_lint["errors"]):
        log("  the longer version was not better (shorter, or more rule errors); keeping the first one")
        return
    from .common import retire
    for name in ("script.txt", "facts.txt", "sources.txt"):
        retire(video.dir / name)
    video.path("script.txt").write_text(new + "\n")
    video.path("facts.txt").write_text(parts["FACTS"].strip() + "\n")
    video.path("sources.txt").write_text(parts["SOURCES"].strip() + "\n")
    check(video)


def check(video: Video) -> dict:
    """Re-run the rule checks, for example after Tejas edits script.txt by hand."""
    result = lint_script(video.read_text("script.txt"), video.read_text("facts.txt"))
    video.write_json("script_lint.json", result)
    log(f"  script: {result['words']} words, {len(result['errors'])} errors, {len(result['warnings'])} warnings")
    for e in result["errors"]:
        log(f"    ERROR  {e}")
    for w in result["warnings"]:
        log(f"    warn   {w}")
    return result
