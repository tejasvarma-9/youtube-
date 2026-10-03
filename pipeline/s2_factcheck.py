"""Stage 2: fact-check every claim and build the sources list.

The video can't move on to voiceover until this passes (or Tejas forces it).
Writes factcheck.json, factcheck.md (the readable report) and sources.md.
"""

from __future__ import annotations

from . import llm, s1_script
from .common import StageError, Video, extract_json, fill, log, parse_sections, prompt, voice_profile

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
            f"Fact-check failed. Read out/{video.slug}/factcheck.md, then either let Claude fix it:\n"
            f"  python -m pipeline revise {video.slug}\n"
            f"or fix script.txt and facts.txt yourself and run: python -m pipeline factcheck {video.slug}"
        )


def problems_text(video: Video) -> str:
    """The failed fact-check as a plain list the writer can work through."""
    fc = video.read_json("factcheck.json")
    lines = []
    for f in fc.get("facts", []):
        if f.get("verdict", "").upper() in BAD:
            claim = f" Claim: \"{f['claim']}\"." if f.get("claim") else ""
            fix = f" Suggested fix: {f['fix']}" if f.get("fix") else ""
            src = f" Source checked: {f['source_url']}" if f.get("source_url") else ""
            lines.append(f"- {f.get('id')} {f.get('verdict')}.{claim} Note: {f.get('note', '')}.{fix}{src}")
    for p in fc.get("policy", []):
        lines.append(f"- POLICY. Quote: \"{p.get('quote', '')}\". Problem: {p.get('problem', '')}. Suggested fix: {p.get('fix', '')}")
    for fid in fc.get("unchecked", []):
        lines.append(f"- {fid} was not checked. Make sure its claim is stated exactly as its source says.")
    for e in fc.get("lint_errors", []):
        lines.append(f"- RULE ERROR. {e}")
    return "\n".join(lines)


def revise(video: Video, stub: bool = False) -> None:
    """Send the failed fact-check back to the writer, then fact-check the result again."""
    try:
        fc = video.read_json("factcheck.json")
    except StageError:
        raise StageError(f"Run the fact-check first: python -m pipeline factcheck {video.slug}")
    if fc.get("passed"):
        log("  fact-check already passed; nothing to revise.")
        return
    problems = problems_text(video)
    b = video.brief()
    files = ("script.txt", "facts.txt", "sources.txt")
    old = {n: video.read_text(n) for n in files}
    text = fill(prompt("revise.md"), VOICE=voice_profile(), TOPIC=b["topic"], ANGLE=b.get("angle") or "(none given)",
                PROBLEMS=problems, SCRIPT=old["script.txt"], FACTS=old["facts.txt"], SOURCES=old["sources.txt"])
    answer = llm.ask(text, "revise", web=True, stub=stub, context={**old, "problems": problems})

    # Keep every earlier version so nothing the writer changed is lost.
    n = 1
    while video.path("raw", f"v{n}").exists():
        n += 1
    for name, body in old.items():
        video.path("raw", f"v{n}", name).write_text(body)
    video.path("raw", f"v{n}", "factcheck.md").write_text(video.read_text("factcheck.md"))
    video.path("raw", f"revise{n}_answer.md").write_text(answer)

    parts = parse_sections(answer, ["SCRIPT", "FACTS", "SOURCES", "CHANGES"])
    if not parts["SCRIPT"]:
        raise StageError(f"The revised script came back empty. See raw/revise{n}_answer.md.")
    for name, key in zip(files, ("SCRIPT", "FACTS", "SOURCES")):
        video.path(name).write_text(parts[key].strip() + "\n")
    video.path("changes.md").write_text(f"# Revision {n}\n\n" + parts["CHANGES"].strip() + "\n")
    log(f"  revised: the previous version is in raw/v{n}/, the change list in changes.md")
    for line in parts["CHANGES"].splitlines():
        if line.strip():
            log(f"    {line.strip()}")
    run(video, stub=stub)


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
        claim = fact_lines.get(f.get("id"), {}).get("claim") or f.get("claim") or "(found in script)"
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
