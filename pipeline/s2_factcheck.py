"""Stage 2: fact-check every claim and build the sources list.

The video can't move on to voiceover until this passes (or Tejas forces it).
Writes factcheck.json, factcheck.md (the readable report) and sources.md.
"""

from __future__ import annotations

from . import llm, s1_script
from .common import StageError, Video, extract_json, fill, log, prompt

BAD = {"UNSUPPORTED", "WRONG"}


def run(video: Video, stub: bool = False) -> None:
    lint = s1_script.check(video)
    script, facts, sources = (video.read_text(n) for n in ("script.txt", "facts.txt", "sources.txt"))
    text = fill(prompt("factcheck.md"), SCRIPT=script, FACTS=facts, SOURCES=sources)
    answer = llm.ask(text, "factcheck", web=True, stub=stub, context={"facts": facts})
    video.path("raw", "factcheck_answer.md").write_text(answer)
    result = extract_json(answer)

    fact_lines = {}
    for line in facts.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            fact_lines[parts[0]] = {"claim": parts[1], "tag": parts[2]}

    failures = [f for f in result.get("facts", []) if f.get("verdict", "").upper() in BAD]
    checked = {f.get("id") for f in result.get("facts", [])}
    unchecked = [fid for fid in fact_lines if fid not in checked]
    policy = result.get("policy", [])
    passed = not failures and not unchecked and not policy and not lint["errors"]

    result.update({"passed": passed, "unchecked": unchecked, "lint_errors": lint["errors"]})
    video.write_json("factcheck.json", result)
    _write_report(video, result, fact_lines)
    _write_sources(video, sources)

    log(f"  fact-check: {'PASSED' if passed else 'FAILED'} "
        f"({len(failures)} bad facts, {len(unchecked)} unchecked, {len(policy)} policy, {len(lint['errors'])} rule errors)")
    if not passed:
        raise StageError(
            f"Fact-check failed. Read out/{video.slug}/factcheck.md, fix script.txt and facts.txt, "
            f"then run: python -m pipeline factcheck {video.slug}"
        )


def require_pass(video: Video, force: bool) -> None:
    try:
        ok = video.read_json("factcheck.json").get("passed")
    except StageError:
        ok = False
    if not ok and not force:
        raise StageError(f"'{video.slug}' hasn't passed fact-check. Run: python -m pipeline factcheck {video.slug}")


def _write_report(video: Video, result: dict, fact_lines: dict) -> None:
    lines = [f"# Fact-check: {video.brief()['topic']}", "", f"**Result: {'PASSED' if result['passed'] else 'FAILED'}**", ""]
    if result["lint_errors"]:
        lines += ["## Rule errors", ""] + [f"- {e}" for e in result["lint_errors"]] + [""]
    if result.get("policy"):
        lines += ["## Policy problems", ""]
        for p in result["policy"]:
            lines.append(f"- \"{p.get('quote', '')}\": {p.get('problem', '')}. Fix: {p.get('fix', '')}")
        lines.append("")
    lines += ["## Facts", "", "| ID | Verdict | Claim | Note | Fix |", "|---|---|---|---|---|"]
    for f in result.get("facts", []):
        claim = fact_lines.get(f.get("id"), {}).get("claim", "(found in script)")
        cells = [f.get("id", ""), f.get("verdict", ""), claim, f.get("note", ""), f.get("fix", "")]
        lines.append("| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in cells) + " |")
    if result["unchecked"]:
        lines += ["", f"Not checked by the fact-checker: {', '.join(result['unchecked'])}"]
    video.path("factcheck.md").write_text("\n".join(lines) + "\n")


def _write_sources(video: Video, sources: str) -> None:
    out = []
    for line in sources.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            out.append(f"{parts[0]}. {parts[1]}: {parts[2]}")
    video.path("sources.md").write_text("\n".join(out) + "\n")
