"""Autopilot: run the crew, fix what is safe to fix, run it again, and stop at GO or when it needs you.

python -m pipeline autopilot [--topic "..."] [--slug <slug>] [--cap 2.00] [--max-cycles 4]
(Or press Start on the dashboard that "Start Crew.command" opens: no command at all.)

Each cycle runs the crew, reads the overseer's verdict and every agent's report, and then does one of three things:
  * everything is GO: it stops and tells you the video is ready for your review;
  * there are problems it may fix on its own (see SAFE below): it fixes them, then re-runs from the first agent
    the fix touches;
  * anything else: it stops and says, in plain words, what needs you.

SAFE edits, and only these:
  - a picture the checker rejected twice: Claude writes a new scene description (no text, no counts above three,
    inside the frame); the old picture moves to _replaced_<date>/; at most 2 rewrites per picture;
  - a note to the pipeline sitting in sources.txt: the note is cut (the id, publisher, title and URL are kept
    exactly, or the line is left alone and you are told);
  - a Short that was rejected or failed its quality check: chosen again (once), then from scratch (once);
  - the overseer's exact script edits (line_fixes), only when each one: is still in script.txt exactly once, adds
    no number that is not already in the fact list, adds at most 12 words, and breaks no script rule. The full
    fact-check and audit then run again on the changed sentences. At most 2 rounds per run;
  - the overseer's title, thumbnail text or description opening (meta_fixes): saved to metadata_overrides.json, so
    later re-runs keep them; the title must stay on the channel template;
  - an agent that stopped: run again once by itself (the voice agent after re-timing the captions), after any of
    the fixes above that apply. A Claude timeout or hiccup is retried inside every step, and on Claude's usage
    limit the run waits for the reset (up to 6 hours) and carries on.
It never adds a fact or a source, and never edits facts.txt or sources.txt. Anything else comes back to you,
and every edit it made is listed in AUTOPILOT.md (old files in _replaced_<date>/).

Hard rules, unchanged: it never runs approve, never uploads, never posts. It stops when the money spent in this
run reaches --cap (default $2.00 per video), and before a cycle whose cost estimate would pass it.
Nothing is deleted: replaced files move to _replaced_<date>/.

State is written to out/<slug>/agents/autopilot.json (the dashboard shows it) and AUTOPILOT.md.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from . import common, config, crew, dashboard, llm
from .common import StageError, Video, fill, log, prompt, retire

CAP_USD = 2.00
MAX_CYCLES = 6
MAX_SCENE_REWRITES = 2   # per picture
MAX_SHORT_REPICKS = 1    # first: choose the rejected ones again; second: choose all of them from scratch
MAX_SCRIPT_FIX_ROUNDS = 2
MAX_AGENT_RETRIES = 1    # an agent that stopped is run again by itself this many times per run before it asks you
MAX_FIX_EXTRA_WORDS = 12
SAFE_LIST = ("a picture the checker rejected (a new scene is written)", "a note to the pipeline in sources.txt (cut out)",
             "a rejected Short (chosen again)", "the overseer's exact script edits (no new numbers)",
             "the overseer's title, thumbnail text or description opening")


def _now() -> str:
    return crew._now()


# --- state ---------------------------------------------------------------------------------------

class State:
    def __init__(self, v: Video, cap: float, max_cycles: int):
        self.v, self.cap, self.max_cycles = v, cap, max_cycles
        p = v.dir / "agents" / "autopilot.json"
        old = json.loads(p.read_text()) if p.exists() else {}
        self.scene_rewrites: dict[str, int] = dict(old.get("scene_rewrites", {}))
        self.short_repicks = 0
        self.script_fix_rounds = 0
        self.agent_retries: dict[str, int] = {}
        self.cycle, self.spent = 0, 0.0
        self.actions: list[dict] = []
        self.needs_you: list[str] = []
        self.status, self.summary = "running", ""
        self.started = _now()

    def data(self) -> dict:
        return {"slug": self.v.slug, "status": self.status, "pid": os.getpid(), "cycle": self.cycle,
                "max_cycles": self.max_cycles, "spent": round(self.spent, 4), "cap": self.cap, "started": self.started,
                "updated": _now(), "actions": self.actions, "needs_you": self.needs_you, "summary": self.summary,
                "scene_rewrites": self.scene_rewrites}

    def save(self) -> None:
        self.v.path("agents", "autopilot.json").write_text(json.dumps(self.data(), indent=2, ensure_ascii=False) + "\n")
        dashboard.build()

    def do(self, text: str) -> None:
        log(f"[autopilot] {text}")
        self.actions.append({"cycle": self.cycle, "text": text})
        self.save()

    def finish(self, status: str, summary: str, needs: list[str] | None = None) -> None:
        self.status, self.summary, self.needs_you = status, summary, needs or []
        self.save()
        lines = [f"# Autopilot: {self.v.slug}", "", f"**{summary}**", "",
                 f"Cycles: {self.cycle} of {self.max_cycles}. Spent about ${self.spent:.2f} of ${self.cap:.2f}.", ""]
        if self.needs_you:
            lines += ["## Needs you", ""] + [f"- {x}" for x in self.needs_you] + [""]
        if self.actions:
            lines += ["## What it did by itself", ""] + [f"- cycle {a['cycle']}: {a['text']}" for a in self.actions] + [""]
        lines += ["---", "", "Autopilot recommends only. It never approved, uploaded or posted anything."]
        self.v.path("agents", "AUTOPILOT.md").write_text("\n".join(lines) + "\n")
        log(f"\n[autopilot] {summary}")
        for x in self.needs_you:
            log(f"  needs you: {x}")


# --- the safe fixes -------------------------------------------------------------------------------

def _clean(scene: str) -> str:
    return scene.split(" (Redraw note:")[0].strip()


def new_scene(v: Video, shot: dict, problem: str, stub: bool) -> str:
    text = llm.ask(fill(prompt("autopilot_scene.md"), NARRATION=shot.get("text", ""), SCENE=_clean(shot["scene"]),
                        PROBLEM=problem), "autopilot_scene", stub=stub, context={})
    scene = " ".join(text.strip().strip('"').split())
    if not scene or len(scene) > 500:
        raise StageError(f"Claude's new scene for shot {shot['id']} was empty or too long.")
    return scene


def rewrite_scene(v: Video, shot_id: int, problem: str, stub: bool) -> str:
    data = v.read_json("shots.json")
    shot = next(s for s in data["shots"] if s["id"] == shot_id)
    scene = new_scene(v, shot, problem, stub)
    tail = shot["prompt"][shot["prompt"].index("\n\nStyle:"):] if "\n\nStyle:" in shot.get("prompt", "") else ""
    retire(v.dir / "shots.json")
    shot["scene"] = scene
    if tail:
        shot["prompt"] = scene + tail
    else:
        shot.pop("prompt", None)
    v.write_json("shots.json", data)
    retire(v.dir / "images" / f"shot_{shot_id:03d}.png")
    return scene


def strip_source_notes(v: Video, stub: bool) -> tuple[list[str], list[str]]:
    """Cut pipeline notes out of sources.txt. Returns (lines changed, lines it could not safely change)."""
    p = v.dir / "sources.txt"
    if not p.exists():
        return [], []
    changed, stuck, out = [], [], []
    for line in p.read_text().splitlines():
        if not crew.INTERNAL_NOTE.search(line):
            out.append(line)
            continue
        parts = [x.strip() for x in line.split("|")]
        new = ""
        if len(parts) > 3 and not crew.INTERNAL_NOTE.search(" | ".join(parts[:3])):
            new = " | ".join(parts[:3])
        elif len(parts) >= 3:
            ask = llm.ask(fill(prompt("autopilot_source.md"), LINE=line), "autopilot_source", stub=stub, context={"line": line})
            cand = " ".join(ask.strip().splitlines()[0].split()) if ask.strip() else ""
            bits = [x.strip() for x in cand.split("|")]
            if len(bits) == 3 and bits[0] == parts[0] and bits[2] == parts[-1] and not crew.INTERNAL_NOTE.search(cand):
                new = cand
        if new:
            changed.append(f"{parts[0]}")
            out.append(new)
        else:
            stuck.append(line.strip()[:220])
            out.append(line)
    if changed:
        retire(p)
        p.write_text("\n".join(out) + "\n")
    return changed, stuck


# --- what to do after a cycle ---------------------------------------------------------------------

def _read(p: Path):
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except ValueError:
        return None


def all_go(verdict: dict | None) -> bool:
    shorts_needed = config.SHORTS_COUNT > 0
    return (bool(verdict) and verdict["full_video"]["verdict"] == "GO" and (verdict["shorts"] or not shorts_needed)
            and all(s["verdict"] == "GO" for s in verdict["shorts"]))


def needs_from_verdict(v: Video, verdict: dict | None) -> list[str]:
    """What is left, one plain line per reason and per fix (nothing cut off mid-sentence)."""
    if not verdict:
        return ["No verdict was written. Read out/%s/agents/run.log." % v.slug]
    out = []

    def add(label: str, x: dict) -> None:
        for r in x.get("reasons", []):
            out.append(f"{label}: {str(r).strip()[:900]}")
        for f in x.get("fixes", []):
            out.append(f"{label}, fix: {str(f).strip()[:900]}")

    if verdict["full_video"]["verdict"] != "GO":
        add("Full video", verdict["full_video"])
    for s in verdict["shorts"]:
        if s["verdict"] != "GO":
            add(f"Short {s['short']}", s)
    return out


def apply_line_fix(v: Video, find: str, replace: str) -> None:
    """Replace one exact piece of script.txt (the old file moves to _replaced_<date>/). Used by the page's Apply button."""
    p = v.dir / "script.txt"
    text = p.read_text()
    if text.count(find) != 1:
        raise StageError("That line is not in the script exactly once any more, so nothing was changed.")
    retire(p)
    p.write_text(text.replace(find, replace))


def pending_line_fixes(v: Video) -> list[dict]:
    """The overseer's script edits that still apply (their old words are still in script.txt exactly once)."""
    verdict = _read(v.dir / "agents" / "VERDICT.json") or {}
    script = (v.dir / "script.txt").read_text() if (v.dir / "script.txt").exists() else ""
    return [f for f in verdict.get("line_fixes", []) if script.count(f.get("find", "\0")) == 1]


def _line_fix_problem(script: str, facts: str, find: str, replace: str) -> str | None:
    """Why autopilot must not apply this edit by itself (None: safe)."""
    from . import cards, lint
    if script.count(find) != 1:
        return "its old words are no longer in the script exactly once"
    new_numbers = (cards.numbers_in(replace, words=False) - cards.numbers_in(find, words=False)
                   - cards.numbers_in(facts, words=False))
    if new_numbers:
        return "it adds a number that is not in the fact list (" + ", ".join(f"{n:g}" for n in sorted(new_numbers)) + ")"
    if len(replace.split()) - len(find.split()) > MAX_FIX_EXTRA_WORDS:
        return f"it adds more than {MAX_FIX_EXTRA_WORDS} words"
    before = set(lint.lint_script(script, facts)["errors"])
    after = set(lint.lint_script(script.replace(find, replace), facts)["errors"]) - before
    if after:
        return "it would break a script rule: " + sorted(after)[0][:160]
    return None


def apply_safe_line_fixes(v: Video, verdict: dict | None) -> tuple[list[str], list[str]]:
    """Apply every overseer script edit that passes _line_fix_problem. Returns (applied, skipped with reasons)."""
    p, fp = v.dir / "script.txt", v.dir / "facts.txt"
    if not verdict or not p.exists():
        return [], []
    script, facts = p.read_text(), fp.read_text() if fp.exists() else ""
    applied, skipped = [], []
    for f in verdict.get("line_fixes", []):
        find, rep = f.get("find", ""), f.get("replace", "")
        why = _line_fix_problem(script, facts, find, rep)
        if why:
            skipped.append(f"\"{find[:70]}\": {why}")
            continue
        script = script.replace(find, rep)
        applied.append(f"\"{find[:60]}\" -> \"{rep[:60]}\"")
    if applied:
        retire(p)
        p.write_text(script)
    return applied, skipped


def apply_meta_fixes(v: Video, verdict: dict | None) -> list[str]:
    from . import s7_metadata
    from .lint import lint_title
    done = []
    current = s7_metadata.overrides(v)
    meta = _read(v.dir / "metadata.json") or {}
    for f in (verdict or {}).get("meta_fixes", []):
        field, val = f["field"], f["value"]
        if field == "thumbnail_text":
            val = val.upper()
        if field == "title" and any("Off template" in x or "characters" in x for x in lint_title(val, "A B")):
            log(f"[autopilot] skipped the overseer's title \"{val}\": it is off the channel template")
            continue
        if current.get(field) == val or meta.get(field) == val:
            continue
        s7_metadata.set_override(v, field, val)
        done.append(f"set the {field.replace('_', ' ')} to \"{val[:90]}\" (saved in metadata_overrides.json)")
    return done


def plan(v: Video, verdict: dict | None, st: State, stub: bool) -> tuple[list[str], str | None, bool]:
    """Apply every safe fix that applies. Returns (what was done, the agent to re-run from, replan the Shorts)."""
    done, first, replan = [], None, False

    def earliest(name: str) -> None:
        nonlocal first
        if first is None or crew.AGENTS.index(name) < crew.AGENTS.index(first):
            first = name

    # 1. pictures the checker rejected
    r = crew.read_report(v, "images")
    if r and not r.get("passed") and _ran(v, "images"):
        high = {int(k): f for k, f in (r.get("details", {}).get("findings") or {}).items() if f.get("severity") == "high"}
        for sid, f in sorted(high.items()):
            if st.scene_rewrites.get(str(sid), 0) >= MAX_SCENE_REWRITES:
                continue
            try:
                rewrite_scene(v, sid, f.get("problem", ""), stub)
            except StageError as e:
                log(f"[autopilot] could not rewrite shot {sid}: {e}")
                continue
            st.scene_rewrites[str(sid)] = st.scene_rewrites.get(str(sid), 0) + 1
            done.append(f"wrote a new scene for shot {sid} ({f.get('problem', '')[:90]}); the old picture is in _replaced_<date>/")
            earliest("images")
    # 1b. pictures the overseer found wrong (scene contradicts its narration, and so on)
    for f in (verdict or {}).get("picture_fixes", []):
        sid = f["shot"]
        if st.scene_rewrites.get(str(sid), 0) >= MAX_SCENE_REWRITES or not (v.dir / "images" / f"shot_{sid:03d}.png").exists():
            continue
        try:
            rewrite_scene(v, sid, f["problem"], stub)
        except StageError as e:
            log(f"[autopilot] could not rewrite shot {sid}: {e}")
            continue
        st.scene_rewrites[str(sid)] = st.scene_rewrites.get(str(sid), 0) + 1
        done.append(f"wrote a new scene for shot {sid} (the overseer: {f['problem'][:90]}); the old picture is in _replaced_<date>/")
        earliest("images")
    # 1b2. pictures the full-video quality check flagged as high severity
    qc = _read(v.dir / "qc.json") or {}
    if _ran(v, "packaging") and not qc.get("passed"):
        for f in ((qc.get("checks") or {}).get("claude_review") or {}).get("images", []) or []:
            try:
                sid = int(f.get("shot"))
            except (TypeError, ValueError):
                continue
            if f.get("severity") != "high" or st.scene_rewrites.get(str(sid), 0) >= MAX_SCENE_REWRITES:
                continue
            try:
                rewrite_scene(v, sid, f.get("problem", ""), stub)
            except StageError as e:
                log(f"[autopilot] could not rewrite shot {sid}: {e}")
                continue
            st.scene_rewrites[str(sid)] = st.scene_rewrites.get(str(sid), 0) + 1
            done.append(f"wrote a new scene for shot {sid} (the quality check: {f.get('problem', '')[:90]}); old picture in _replaced_<date>/")
            earliest("images")
    # 1c. the overseer's exact script edits, and its title / thumbnail / description edits
    if st.script_fix_rounds < MAX_SCRIPT_FIX_ROUNDS:
        applied, skipped = apply_safe_line_fixes(v, verdict)
        for s in skipped:
            log(f"[autopilot] left a script edit for you: {s}")
        if applied:
            st.script_fix_rounds += 1
            done.append("applied the overseer's script edit(s): " + "; ".join(applied) + " (old script in _replaced_<date>/)")
            earliest("script_factcheck")
    meta_done = apply_meta_fixes(v, verdict)
    if meta_done:
        done += meta_done
        earliest("packaging")
    # 1d. an agent that stopped: run it again by itself (once), after the one automatic repair that fits it
    stopped = (verdict or {}).get("stopped_at")
    if stopped and first is None and st.agent_retries.get(stopped, 0) < MAX_AGENT_RETRIES:
        st.agent_retries[stopped] = st.agent_retries.get(stopped, 0) + 1
        note = ""
        if stopped == "voice_captions":
            try:
                from .cli import cmd_align
                cmd_align(v)
                note = " after re-timing the captions from the voice"
            except StageError as e:
                log(f"[autopilot] re-timing the captions did not work: {str(e)[:200]}")
        done.append(f"the {stopped} agent stopped; ran it again by itself{note} (try {st.agent_retries[stopped]} of {MAX_AGENT_RETRIES})")
        earliest(stopped)
    # 2. notes to the pipeline in the source list
    changed, stuck = strip_source_notes(v, stub)
    if changed:
        done.append(f"cut the pipeline note out of sources.txt ({', '.join(changed)}); the old file is in _replaced_<date>/")
        earliest("script_factcheck")
    # 3. Shorts that did not make it
    sr = crew.read_report(v, "shorts")
    bad = [n for n, s in ((sr or {}).get("details", {}).get("shorts") or {}).items() if s.get("status") != "READY"]
    if sr and bad and _ran(v, "shorts") and first is None and st.short_repicks < MAX_SHORT_REPICKS:
        st.short_repicks += 1
        replan = st.short_repicks == 2
        done.append(("chose all the Shorts again from scratch" if replan else f"ran the Shorts agent again for Short(s) {', '.join(bad)}"))
        earliest("shorts")
    return done, first, replan


def _ran(v: Video, name: str) -> bool:
    """Did this agent run in the cycle that just ended? (An old report must not trigger a fix.)"""
    return dashboard.read_status(v).get("agents", {}).get(name, {}).get("state") in ("passed", "stopped")


def _ran_cost(v: Video) -> float:
    st = dashboard.read_status(v)
    total = 0.0
    for name, a in st.get("agents", {}).items():
        if a.get("state") in ("passed", "stopped"):
            total += float((crew.read_report(v, name) or {}).get("cost_usd") or 0)
    return total


def _crew_args(a) -> argparse.Namespace:
    g = lambda k, d=None: getattr(a, k, d)  # noqa: E731
    return argparse.Namespace(topic=g("topic"), angle=g("angle"), character=g("character"), notes=g("notes"),
                              words=g("words", config.TARGET_WORDS), slug=g("slug"), pick_top=True, from_agent=g("from_agent"),
                              yes=True, count=g("count", config.SHORTS_COUNT), replan=False, stub=g("stub", False),
                              sync=g("sync", False), force=g("force", False), no_captions=False)


def run(a) -> State:
    ns = _crew_args(a)
    cap = float(getattr(a, "cap", CAP_USD))
    max_cycles = int(getattr(a, "max_cycles", MAX_CYCLES))
    v = crew.start(ns)
    if v is None:
        raise StageError("No topic was chosen, so there is nothing to run.")
    ns.slug, ns.topic = v.slug, None
    st = State(v, cap, max_cycles)
    st.save()
    try:
        for cycle in range(1, max_cycles + 1):
            st.cycle = cycle
            st.save()
            now = sum(r[1] for r in crew.estimate(v, ns))
            if st.spent + now > cap:
                st.finish("cap", f"Stopped before spending more: the next run is estimated at ${now:.2f} and ${st.spent:.2f} of the "
                                 f"${cap:.2f} limit is already used.",
                          [f"Raise the limit and start again (Start on the dashboard, or --cap {cap + 2:.2f}), or fix it yourself."])
                return st
            log(f"\n[autopilot] cycle {cycle} of {max_cycles}, spent ${st.spent:.2f} of ${cap:.2f}")
            try:
                crew.cmd_crew(ns)
            except StageError as e:
                log(f"[autopilot] the crew stopped: {str(e)[:300]}")
                if "hit your" in str(e) and "limit" in str(e):
                    st.finish("limit", "Stopped: Claude's usage limit was reached. Nothing is lost: run the same command again "
                                       "after the limit resets, and finished agents skip themselves.",
                              [f"After the reset: python -m pipeline autopilot --slug {v.slug}"])
                    return st
            st.spent += _ran_cost(v)
            ns.from_agent, ns.replan = None, False
            verdict = _read(v.dir / "agents" / "VERDICT.json")
            if all_go(verdict):
                st.finish("ready", "Everything is GO. Read the verdict, watch the video, then approve it yourself.")
                return st
            if verdict and verdict["full_video"]["verdict"] == "GO" and not verdict["shorts"] and config.SHORTS_COUNT > 0:
                st.do("the full video is GO, so the Shorts are made now")
                ns.from_agent = "shorts"
                continue
            if st.spent >= cap:
                st.finish("cap", f"Stopped: spent ${st.spent:.2f}, the limit is ${cap:.2f}.", needs_from_verdict(v, verdict))
                return st
            done, first, replan = plan(v, verdict, st, ns.stub)
            if not done:
                st.finish("needs_you", "Stopped: what is left needs a decision from you.", needs_from_verdict(v, verdict))
                return st
            for d in done:
                st.do(d)
            ns.from_agent, ns.replan = first, replan
        verdict = _read(v.dir / "agents" / "VERDICT.json")
        st.finish("max_cycles", f"Stopped after {max_cycles} cycles; it is still not GO.", needs_from_verdict(v, verdict))
    except BaseException as e:
        if st.status == "running":
            st.finish("error", f"Stopped by an error: {type(e).__name__}: {str(e)[:300]}",
                      ["This is a problem in the pipeline, not in your video. The crew reports in out/%s/agents/ show how far it got." % v.slug])
        raise
    return st
