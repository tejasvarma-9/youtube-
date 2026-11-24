"""Local copies of the sources, so checkers read the real document instead of failing on it.

Big PDFs (a 300-page 10-K) are where web reading fails: the checker says "could not open it", the fact stays
unverified, and the video comes back NO-GO run after run. Before each check, every URL in sources.txt is
downloaded once and turned into plain text in out/<slug>/source_text/S<n>.txt. The checker can open those
files and search them (Read and Grep, read-only).

out/<slug>/verified.txt holds facts already confirmed by hand against their source, one per line:
    F16 | confirmed in the 10-K, Financial performance table: Net interest income 95,443 (2024: 92,583), up 3%
Checkers are told not to report those as unverified again.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from html import unescape
from pathlib import Path

from .common import log

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15"}
MAX_CHARS = 4_000_000


def _sources(video) -> list[tuple[str, str, str]]:
    p = video.dir / "sources.txt"
    out = []
    for line in (p.read_text().splitlines() if p.exists() else []):
        parts = [x.strip() for x in line.split("|")]
        if len(parts) >= 3 and re.fullmatch(r"S\d+", parts[0]) and parts[2].startswith("http"):
            out.append((parts[0], parts[1], parts[2]))
    return out


def _pdf_text(data: bytes, tmp: Path) -> str:
    tmp.write_bytes(data)
    exe = shutil.which("pdftotext") or next((p for p in ("/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext") if Path(p).exists()), None)
    try:
        if exe:
            r = subprocess.run([exe, "-layout", str(tmp), "-"], capture_output=True, timeout=180)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode(errors="replace")
        try:
            from pypdf import PdfReader
        except ImportError:
            return ""
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(tmp)).pages)
    except Exception:
        return ""


def _html_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<(br|p|div|tr|li|h[1-6])[^>]*>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    return "\n".join(re.sub(r"[ \t]+", " ", l).strip() for l in text.splitlines() if l.strip())


def prepare(video) -> Path:
    """Download any source not saved yet. Never raises: a source that can't be saved is just listed as missing."""
    folder = video.dir / "source_text"
    folder.mkdir(exist_ok=True)
    try:
        import requests
    except ImportError:
        return folder
    for sid, title, url in _sources(video):
        out = folder / f"{sid}.txt"
        if out.exists() and out.read_text(errors="replace").startswith(f"URL: {url}\n"):
            continue
        text = ""
        try:
            r = requests.get(url, headers=UA, timeout=90)
            if r.status_code == 200:
                if url.lower().endswith(".pdf") or r.content[:4] == b"%PDF":
                    text = _pdf_text(r.content, folder / f"{sid}.pdf")
                    if not text:
                        log(f"    {sid}: PDF saved but no text could be pulled out. One-time fix: brew install poppler")
                else:
                    text = _html_text(r.text)
        except Exception as e:  # network trouble must never stop a check
            log(f"    {sid}: could not download ({type(e).__name__})")
        if text.strip():
            out.write_text(f"URL: {url}\nTITLE: {title}\n\n" + text[:MAX_CHARS])
    return folder


def context(video) -> str:
    """Text added to a checker's prompt: what local copies exist and what is already confirmed by hand."""
    folder = video.dir / "source_text"
    have = sorted(folder.glob("S*.txt"), key=lambda p: int(p.stem[1:]) if p.stem[1:].isdigit() else 0) if folder.exists() else []
    parts = []
    if have:
        lines = []
        for p in have:
            head = p.read_text(errors="replace")[:400].splitlines()
            title = next((h[7:] for h in head if h.startswith("TITLE: ")), "")
            lines.append(f"- {p.name}: {title} ({p.stat().st_size // 1000} KB)")
        parts.append("LOCAL COPIES OF THE SOURCES. The full text of these documents was downloaded before this step and saved in "
                     "your current folder. Open them with Read, and search big ones with Grep (for example the exact figure, "
                     "or a heading like 'Net interest income'), BEFORE trying the web. They are the same documents as the URLs. "
                     "Never call a source unreachable or unreadable if its file is listed here.\n" + "\n".join(lines))
    vf = video.dir / "verified.txt"
    if vf.exists() and vf.read_text().strip():
        parts.append("ALREADY CONFIRMED BY HAND against the primary source. Do not report these as unverified, unreachable or "
                     "resting on memory. Report one only if the script or fact list now says something different:\n"
                     + vf.read_text().strip())
    return ("\n\n" + "\n\n".join(parts) + "\n") if parts else ""
