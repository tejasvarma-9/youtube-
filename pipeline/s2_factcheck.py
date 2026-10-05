"""Stage 2: fact-check every claim and build the sources list.

The video can't move on to voiceover until this passes (or Tejas forces it).
Writes factcheck.json, factcheck.md (the readable report) and sources.md.
"""

from __future__ import annotations

import hashlib
import re

from . import llm, s1_script
from .common import (StageError, Video, extract_json, fill, log, parse_sections, prompt, sentences_with_paragraphs,
                     voice_profile)

BAD = {"UNSUPPORTED", "WRONG"}
AUDIT_BAD = {"MISMATCH", "OVERSTATED", "INCONSISTENT", "ARITHMETIC"}  # UNVERIFIED is listed but does not block


def run(video: Video, stub: bool = False, full: bool = False) -> None:
    lint = s1_script.check(video)
    script, facts, sources = (video.read_text(n) for n in ("script.txt", "facts.txt", "sources.txt"))
    fact_lines = _fact_lines(facts)
    urls = _source_urls(sources)
    sentences = [r["text"] for r in sentences_with_paragraphs(script)]

    # After a revision only what changed is re-checked. A fresh full check finds a different
    # handful of nitpicks every time, so re-checking everything never settles.
    prev = {} if full else _previous(video)
    if "verified" not in prev:
        prev = {}
    carried = []
    for fid, f in fact_lines.items():
        old = prev.get("verified", {}).get(fid)
        if old and old["key"] == _fact_key(f, urls):
            carried.append(old["result"])
    carried_ids = {c["id"] for c in carried}
    to_check = {fid: f for fid, f in fact_lines.items() if fid not in carried_ids}
    clean = set(prev.get("clean_sentences", []))
    changed = [t for t in sentences if _h(t) not in clean]

    if prev and len(changed) < len(sentences):
        log(f"  fact-check: re-checking {len(to_check)} of {len(fact_lines)} facts and {len(changed)} of "
            f"{len(sentences)} sentences (the rest passed last time; --full re-checks everything)")
        scope = ("This script was checked before and then revised. The fact lines below are the only ones that "
                 "still need checking; the others were already verified. Look for missing claims and policy "
                 "problems ONLY in these sentences, and report nothing about any other sentence:\n"
                 + "\n".join(f"- {t}" for t in changed))
    else:
        scope = ("Check every fact line. Read the whole script for missing claims and policy problems.")
    facts_text = "\n".join(f"{fid} | {f['claim']} | {f['tag']}" for fid, f in to_check.items())
    if to_check or changed:
        text = fill(prompt("factcheck.md"), SCRIPT=script, FACTS=facts_text or "(none)", SOURCES=sources, SCOPE=scope)
        answer = llm.ask(text, "factcheck", web=True, stub=stub, context={"facts": facts_text})
        video.path("raw", "factcheck_answer.md").write_text(answer)
        result = extract_json(answer)
    else:
        result = {"facts": [], "policy": []}
    result["facts"] = carried + [f for f in result.get("facts", []) if f.get("id") not in carried_ids]

    failures = [f for f in result.get("facts", []) if f.get("verdict", "").upper() in BAD]
    checked = {f.get("id") for f in result.get("facts", [])}
    unchecked = [fid for fid in fact_lines if fid not in checked]
    policy = result.get("policy", [])
    passed = not failures and not unchecked and not policy and not lint["errors"]

    # A second, independent reading (it never sees the writer's fact list or sources) runs once the
    # first check is clean, and again only when the script's words have changed.
    audit = _previous(video).get("audit") if not full else None
    if passed:
        if not audit or audit.get("sha") != _h(script) or audit.get("problems"):
            audit = _audit(video, script, stub, prev=audit)
        passed = not audit["problems"]
    else:
        audit = None

    result.update({"passed": passed, "unchecked": unchecked, "lint_errors": lint["errors"], "audit": audit})
    result.update(_settled(result, fact_lines, urls, sentences))
    video.write_json("factcheck.json", result)
    _write_report(video, result, fact_lines)
    _write_sources(video, sources)

    n_audit = len(audit["problems"]) if audit else 0
    log(f"  fact-check: {'PASSED' if passed else 'FAILED'} "
        f"({len(failures)} bad facts, {len(unchecked)} unchecked, {len(policy)} policy, {len(lint['errors'])} rule errors"
        f"{f', {n_audit} found by the independent audit' if audit else ''})")
    if not passed:
        raise StageError(
            f"Fact-check failed. Read out/{video.slug}/factcheck.md, then either let Claude fix it:\n"
            f"  python -m pipeline revise {video.slug}\n"
            f"or fix script.txt and facts.txt yourself and run: python -m pipeline factcheck {video.slug}"
        )


def _audit(video: Video, script: str, stub: bool, prev: dict | None = None) -> dict:
    """An independent reader that checks the script against the real sources without the writer's notes.

    After the first audit only the sentences that changed are looked at again (plus anything they
    affect). A full re-read finds a different handful of nitpicks every time and would never settle.
    """
    sentences = [r["text"] for r in sentences_with_paragraphs(script)]
    if prev and "clean" not in prev and prev.get("sha") == _h(script):
        prev = {**prev, "clean": _settled_audit(prev.get("problems", []), sentences)}  # audit from before this field existed
    clean = set((prev or {}).get("clean", []))
    changed = [t for t in sentences if _h(t) not in clean]
    if prev and clean and changed and len(changed) < len(sentences):
        log(f"  independent audit: re-checking {len(changed)} of {len(sentences)} sentences that changed (no writer notes)...")
        scope = ("This script was audited before and then revised. Check ONLY the claims in the sentences below, and "
                 "any total, calculation or earlier claim they affect. Report nothing about any other sentence.\n"
                 + "\n".join(f"- {t}" for t in changed))
    else:
        log("  independent audit: re-checking the script against primary sources (no writer notes)...")
        scope = "Audit the whole script."
    text = fill(prompt("audit.md"), TOPIC=video.brief()["topic"], SCRIPT=script, SCOPE=scope)
    answer = llm.ask(text, "audit", web=True, stub=stub)
    video.path("raw", "audit_answer.md").write_text(answer)
    checks = extract_json(answer).get("checks", [])
    problems = [c for c in checks if str(c.get("verdict", "")).upper() in AUDIT_BAD]
    return {"sha": _h(script), "checks": checks, "problems": problems, "clean": _settled_audit(problems, sentences)}


def _settled_audit(problems: list, sentences: list) -> list:
    """Sentences containing nothing the audit flagged. If a flagged quote can't be placed in the script,
    nothing counts as settled and the next audit reads the whole script."""
    quotes = [" ".join(str(c.get("quote", "")).split()) for c in problems]
    norm = [" ".join(t.split()) for t in sentences]
    hit = [any(q and (q in t or t in q) for q in quotes) for t in norm]
    placed = all(q and any(q in t or t in q for t in norm) for q in quotes)
    return [_h(t) for t, h in zip(sentences, hit) if not h] if placed else []


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
    for c in (fc.get("audit") or {}).get("problems", []):
        said = f" The script says {c['script_value']}." if c.get("script_value") else ""
        found = f" The source says {c['source_value']}." if c.get("source_value") else ""
        fix = f" Suggested fix: {c['fix']}" if c.get("fix") else ""
        src = f" Source: {c['source_url']}" if c.get("source_url") else ""
        lines.append(f"- AUDIT {str(c.get('verdict', '')).upper()}. Quote: \"{c.get('quote', '')}\".{said}{found} Note: {c.get('note', '')}.{fix}{src}")
    for fid in fc.get("unchecked", []):
        lines.append(f"- {fid} was not checked. Make sure its claim is stated exactly as its source says.")
    for e in fc.get("lint_errors", []):
        lines.append(f"- RULE ERROR. {e}")
    return "\n".join(lines)


def revise(video: Video, stub: bool = False, notes: str = "") -> None:
    """Send the failed fact-check (and any editor notes) back to the writer, then fact-check the result again."""
    try:
        fc = video.read_json("factcheck.json")
    except StageError:
        raise StageError(f"Run the fact-check first: python -m pipeline factcheck {video.slug}")
    if fc.get("passed") and not notes:
        log("  fact-check already passed; nothing to revise. To make edits, pass --notes <file>.")
        return
    migrate(video)
    problems = problems_text(video)
    if notes:
        problems = (problems + "\n\n" if problems else "") + "EDITOR NOTES FROM TEJAS:\n" + notes.strip()
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


def _settled(result: dict, fact_lines: dict, urls: dict, sentences: list) -> dict:
    """What the next check can skip: facts that passed (with what they said and cited), and sentences
    that contain nothing that was flagged."""
    verified = {f["id"]: {"key": _fact_key(fact_lines[f["id"]], urls), "result": f}
                for f in result.get("facts", []) if f.get("id") in fact_lines and f.get("verdict", "").upper() not in BAD}
    bad_new = [f for f in result.get("facts", []) if f.get("verdict", "").upper() in BAD and f.get("id") not in fact_lines]
    quotes = [" ".join(f.get("claim", "").split()) for f in bad_new] + \
             [" ".join(p.get("quote", "").split()) for p in result.get("policy", [])]
    norm = [" ".join(t.split()) for t in sentences]
    hit = [any(q and (q in t or t in q) for q in quotes) for t in norm]
    placed = all(q and any(q in t or t in q for t in norm) for q in quotes)
    # A flagged quote we can't find in any one sentence means the whole script gets read again next time.
    clean = [_h(t) for t, h in zip(sentences, hit) if not h] if placed else []
    return {"verified": verified, "clean_sentences": clean}


def migrate(video: Video) -> None:
    """Fact-checks from before incremental checking didn't record what passed. Work it out from the
    files they checked, so the next check can still skip what is settled."""
    fc = _previous(video)
    if not fc or "verified" in fc:
        return
    script, facts, sources = (video.read_text(n) for n in ("script.txt", "facts.txt", "sources.txt"))
    sentences = [r["text"] for r in sentences_with_paragraphs(script)]
    fc.update(_settled(fc, _fact_lines(facts), _source_urls(sources), sentences))
    video.write_json("factcheck.json", fc)


def _fact_lines(facts: str) -> dict:
    out = {}
    for line in facts.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            out[parts[0]] = {"claim": parts[1], "tag": parts[2]}
    return out


def _source_urls(sources: str) -> dict:
    out = {}
    for line in sources.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            out[parts[0]] = parts[2]
    return out


def _fact_key(f: dict, urls: dict) -> str:
    """A fact counts as unchanged only if its words, its tag and the URL behind its source are all the same."""
    cited = " ".join(urls.get(t, t) for t in re.findall(r"S\d+|ESTIMATE", f["tag"].upper()))
    return _h(f"{f['claim']}|{f['tag']}|{cited}")


def _h(text: str) -> str:
    return hashlib.sha1(" ".join(text.split()).encode()).hexdigest()[:16]


def _previous(video: Video) -> dict:
    try:
        return video.read_json("factcheck.json")
    except StageError:
        return {}


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
    audit = result.get("audit")
    if audit:
        bad = audit.get("problems", [])
        lines += ["## Independent audit", "", f"{len(audit.get('checks', []))} items looked at, {len(bad)} problems.", ""]
        for c in audit.get("checks", []):
            lines.append(f"- {str(c.get('verdict', '')).upper()}: \"{c.get('quote', '')}\". Script: {c.get('script_value', '')}. "
                         f"Source: {c.get('source_value', '')} {c.get('source_url', '')}. {c.get('note', '')} {('Fix: ' + c['fix']) if c.get('fix') else ''}".rstrip())
        lines.append("")
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
