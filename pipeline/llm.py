"""Text generation. Real runs call the Claude Code CLI headlessly; --stub runs use canned answers."""

from __future__ import annotations

import shutil
import subprocess
import time

from . import config, stubs
from .common import StageError, log

WEB_TOOLS = ["WebSearch", "WebFetch"]


def ask(prompt_text: str, kind: str, *, web: bool = False, stub: bool = False, context: dict | None = None,
        read_dir: str | None = None) -> str:
    """Send one prompt and return the model's text answer.

    kind names the stage ("script", "factcheck", "shots", "metadata") so stub
    mode can return a matching canned answer.
    """
    if stub:
        return stubs.llm_answer(kind, context or {})

    if not shutil.which(config.CLAUDE_BIN):
        raise StageError(
            "The Claude Code CLI ('claude') isn't on your PATH. Install Claude Code, or run with --stub to test."
        )
    cmd = [config.CLAUDE_BIN, "-p", "--output-format", "text"]
    model = model_for(kind)
    if model:
        cmd += ["--model", model]
    tools = list(WEB_TOOLS) if web else []
    if read_dir:
        # Lets Claude open and search files in one folder, read-only (pictures, saved source documents).
        cmd += ["--add-dir", read_dir]
        tools += ["Read", "Grep", "Glob"]
    if tools:
        cmd += ["--allowedTools", *tools]
    # The writer only needs to read the web; it never touches files or the shell.
    cmd += ["--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit"]
    log(f"  asking Claude ({kind}{', with web research' if web else ''}); this can take several minutes...")
    waited_for_limit = False
    for attempt in range(1, RETRIES + 2):
        try:
            res = subprocess.run(
                cmd, input=prompt_text, capture_output=True, text=True, timeout=config.CLAUDE_TIMEOUT_S,
                cwd=read_dir or None,
            )
        except subprocess.TimeoutExpired as e:
            if attempt <= RETRIES:
                log(f"  Claude took too long on the {kind} step; trying again ({attempt} of {RETRIES})")
                continue
            raise StageError(f"Claude took longer than {config.CLAUDE_TIMEOUT_S}s on the {kind} step, {RETRIES + 1} times.") from e
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout
        if model and "--model" in cmd and _bad_model(res.stderr + res.stdout):
            log(f"  Claude did not accept the model \"{model}\" on the {kind} step; using Claude Code's default model instead. "
                "Check CLAUDE_MODEL in .env.")
            i = cmd.index("--model")
            del cmd[i:i + 2]
            model = ""
            continue
        # Some failures (a usage limit, a login problem) are printed on stdout with nothing on stderr.
        detail = (res.stderr.strip() or res.stdout.strip() or "(no message)")[-2000:]
        wait = _limit_wait_seconds(detail)
        if wait is not None and not waited_for_limit and wait <= MAX_LIMIT_WAIT_H * 3600:
            waited_for_limit = True
            _sleep_until_reset(wait, kind)
            continue
        if wait is None and not _permanent(detail) and attempt <= RETRIES:
            pause = 30 * attempt
            log(f"  Claude failed on the {kind} step ({detail.splitlines()[-1][:120]}); trying again in {pause}s "
                f"({attempt} of {RETRIES})")
            time.sleep(pause)
            continue
        raise StageError(f"Claude failed on the {kind} step (exit code {res.returncode}):\n{detail}\n"
                         f"To see Claude's own message, run: claude -p \"say hi\"")
    raise StageError(f"Claude failed on the {kind} step.")


def model_for(kind: str) -> str:
    """The model for one step: CLAUDE_MODEL_<STEP> in .env if set (for example CLAUDE_MODEL_OVERSEER=opus),
    else CLAUDE_MODEL, else empty (Claude Code's own default)."""
    import os
    return os.environ.get(f"CLAUDE_MODEL_{kind.upper()}", "").strip() or config.CLAUDE_MODEL


def _bad_model(text: str) -> bool:
    t = text.lower()
    return "model" in t and any(w in t for w in ("not found", "invalid", "unknown", "issue with the selected", "may not exist", "not available"))


RETRIES = 2              # a timeout, an overloaded server or an empty answer is tried again, after 30s then 60s
MAX_LIMIT_WAIT_H = 6     # on Claude's usage limit, wait for the reset (up to this long) and carry on by itself


def _permanent(detail: str) -> bool:
    """Failures that the same request will hit again: not worth retrying."""
    d = detail.lower()
    return any(w in d for w in ("not logged in", "login", "authenticat", "invalid api key", "permission", "not found: claude"))


def _limit_wait_seconds(detail: str) -> float | None:
    """Seconds until Claude's usage limit resets, from a message like "resets 10:10pm (Asia/Calcutta)". None: not a limit."""
    import datetime as dt
    import re
    if "limit" not in detail.lower() or "reset" not in detail.lower():
        return None
    m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?(?:\s*\(([^)]+)\))?", detail, re.I)
    if not m:
        return 3600.0
    h, mi, ap, tzname = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(), m.group(4)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname) if tzname else None
    except Exception:
        tz = None
    now = dt.datetime.now(tz) if tz else dt.datetime.now()
    reset = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if reset <= now:
        reset += dt.timedelta(days=1)
    return (reset - now).total_seconds() + 120  # two minutes of margin


def _sleep_until_reset(seconds: float, kind: str) -> None:
    import datetime as dt
    until = (dt.datetime.now() + dt.timedelta(seconds=seconds)).strftime("%H:%M")
    log(f"  Claude's usage limit was reached on the {kind} step. Waiting until about {until} ({seconds / 3600:.1f} h), "
        "then carrying on by itself. Leave this window open (Ctrl+C stops it; nothing is lost).")
    time.sleep(seconds)
