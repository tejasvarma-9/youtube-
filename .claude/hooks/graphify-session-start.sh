#!/bin/bash
# Builds the Graphify code map (graphify-out/) at the start of a Claude Code session.
# In a cloud session it installs Graphify first, since those containers start fresh.
# On a Mac without Graphify it does nothing.
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
export PATH="$HOME/.local/bin:$PATH"

if ! command -v graphify >/dev/null 2>&1 && [ "$CLAUDE_CODE_REMOTE" = "true" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv tool install --quiet graphifyy >/dev/null 2>&1
  else
    python3 -m pip install --quiet --user graphifyy >/dev/null 2>&1
  fi
fi

if command -v graphify >/dev/null 2>&1; then
  graphify update . >/dev/null 2>&1 || true
fi
exit 0
