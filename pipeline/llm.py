"""Text generation. Real runs call the Claude Code CLI headlessly; --stub runs use canned answers."""

from __future__ import annotations

import shutil
import subprocess

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
    if config.CLAUDE_MODEL:
        cmd += ["--model", config.CLAUDE_MODEL]
    if web:
        cmd += ["--allowedTools", *WEB_TOOLS]
    if read_dir:
        # Lets Claude open files (pictures) in one folder, read-only.
        cmd += ["--add-dir", read_dir, "--allowedTools", "Read"]
    # The writer only needs to read the web; it never touches files or the shell.
    cmd += ["--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit"]
    log(f"  asking Claude ({kind}{', with web research' if web else ''}); this can take several minutes...")
    try:
        res = subprocess.run(
            cmd, input=prompt_text, capture_output=True, text=True, timeout=config.CLAUDE_TIMEOUT_S,
            cwd=read_dir or None,
        )
    except subprocess.TimeoutExpired as e:
        raise StageError(f"Claude took longer than {config.CLAUDE_TIMEOUT_S}s on the {kind} step.") from e
    if res.returncode != 0:
        # Some failures (a usage limit, a login problem) are printed on stdout with nothing on stderr.
        detail = (res.stderr.strip() or res.stdout.strip() or "(no message)")[-2000:]
        raise StageError(f"Claude failed on the {kind} step (exit code {res.returncode}):\n{detail}\n"
                         f"To see Claude's own message, run: claude -p \"say hi\"")
    return res.stdout
