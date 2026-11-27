#!/bin/bash
# Double-click this file. It opens the dashboard with a Start button.
cd "$(dirname "$0")" || exit 1
[ -f .venv/bin/activate ] && source .venv/bin/activate
python -m pipeline serve
