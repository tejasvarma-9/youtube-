"""Shared helpers: the per-video folder, text splitting, JSON and subprocess utilities."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from . import config


class StageError(RuntimeError):
    """A stage cannot continue; the message tells Tejas what to do."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60].rstrip("-") or "video"


class Video:
    """One video's working folder: out/<slug>/."""

    def __init__(self, slug: str):
        self.slug = slug
        self.dir = config.OUT / slug
        if not self.dir.exists():
            raise StageError(f"No video named '{slug}'. Create it with: python -m pipeline new \"<topic>\"")

    def path(self, *parts: str) -> Path:
        p = self.dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def read_json(self, name: str):
        p = self.dir / name
        if not p.exists():
            raise StageError(f"{name} is missing for '{self.slug}'. Run the earlier stage first.")
        return json.loads(p.read_text())

    def write_json(self, name: str, data) -> Path:
        p = self.path(name)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        return p

    def read_text(self, name: str) -> str:
        p = self.dir / name
        if not p.exists():
            raise StageError(f"{name} is missing for '{self.slug}'. Run the earlier stage first.")
        return p.read_text()

    def brief(self) -> dict:
        return self.read_json("brief.json")


# --- text -------------------------------------------------------------------

_ABBREV = {
    "mr", "mrs", "ms", "dr", "st", "inc", "co", "corp", "ltd", "jr", "sr", "vs", "etc",
    "e.g", "i.e", "u.s", "u.k", "no", "approx", "est", "jan", "feb", "mar", "apr", "jun",
    "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}


def paragraphs(script: str) -> list[str]:
    return [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", script.strip()) if p.strip()]


def split_sentences(paragraph: str) -> list[str]:
    """Split a paragraph into sentences without breaking on $1.5, U.S. or Inc."""
    out, start = [], 0
    for m in re.finditer(r"[.!?]+[\"'”’)]*(?=\s+|$)", paragraph):
        end = m.end()
        before = paragraph[start:m.start()].split()
        last = before[-1].lower().rstrip(".") if before else ""
        if m.group().startswith(".") and (last in _ABBREV or re.fullmatch(r"[a-z]", last)):
            continue
        rest = paragraph[end:].lstrip()
        if rest and rest[0].islower():
            continue
        out.append(paragraph[start:end].strip())
        start = end
    tail = paragraph[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


def sentences_with_paragraphs(script: str) -> list[dict]:
    """Every sentence in order, tagged with its paragraph number (0-based)."""
    rows = []
    for pi, para in enumerate(paragraphs(script)):
        for s in split_sentences(para):
            rows.append({"i": len(rows) + 1, "paragraph": pi, "text": s})
    return rows


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9$%][A-Za-z0-9$%,.'\-]*", text))


def parse_sections(text: str, names: list[str]) -> dict:
    """Split LLM output on ===NAME=== marker lines."""
    parts, current = {}, None
    for line in text.splitlines():
        m = re.fullmatch(r"\s*===\s*([A-Z]+)\s*===\s*", line)
        if m and m.group(1) in names:
            current = m.group(1)
            parts[current] = []
        elif current:
            parts[current].append(line)
    missing = [n for n in names if n not in parts]
    if missing:
        raise StageError(f"The model's answer is missing sections: {', '.join(missing)}")
    return {k: "\n".join(v).strip() for k, v in parts.items()}


def extract_json(text: str):
    m = re.search(r"===JSON===\s*(.*?)\s*(===END===|$)", text, re.S)
    body = m.group(1) if m else text
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body.strip())
    start = min([i for i in (body.find("{"), body.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise StageError("The model's answer had no JSON in it.")
    try:
        obj, _ = json.JSONDecoder().raw_decode(body[start:])
    except json.JSONDecodeError as e:
        raise StageError(f"The model's JSON could not be read: {e}") from e
    return obj


def fill(template: str, **values: str) -> str:
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", v)
    return template


def prompt(name: str) -> str:
    return (config.PROMPTS / name).read_text()


def voice_profile() -> str:
    return config.VOICE_FILE.read_text()


# --- processes and media ----------------------------------------------------

def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if res.returncode != 0:
        raise StageError(f"Command failed: {' '.join(map(str, cmd[:3]))} ...\n{res.stderr[-2000:]}")
    return res


def media_duration(path: Path) -> float:
    res = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(res.stdout.strip())


def fmt_ts(seconds: float, srt: bool = False) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    if srt:
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
