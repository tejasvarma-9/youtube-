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
