"""The dashboard with a Start button: a small server that only answers on this Mac (127.0.0.1).

python -m pipeline serve        # or double-click "Start Crew.command"

It shows the same dashboard as `dashboard`, plus a Start box. Pressing Start runs `python -m pipeline autopilot`
as a separate process, so it keeps going even if you close the browser tab (close the Start Crew window and the
page can no longer show it, but the work continues). Nothing here approves, uploads or posts anything.

Safety: it listens on 127.0.0.1 only; every Start needs a token that only the page it served knows (so another
website open in your browser cannot start a run); it checks the Host header; one autopilot at a time.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, dashboard
from .common import log

PORTS = range(8765, 8776)


def make_handler(token: str, port_box: list):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the window quiet
            pass

        def _host_ok(self) -> bool:
            host = self.headers.get("Host", "")
            return host in (f"127.0.0.1:{port_box[0]}", f"localhost:{port_box[0]}")

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj).encode())

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "Wrong host."})
            if self.path.split("?")[0] in ("/", "/index.html"):
                return self._send(200, dashboard.render(dashboard.collect(), token=token).encode(), "text/html")
            self._json(404, {"error": "Not found."})

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Token") != token:
                return self._json(403, {"error": "Not allowed."})
            if self.path not in ("/start", "/apply"):
                return self._json(404, {"error": "Not found."})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(size, 4096)) or b"{}")
            except ValueError:
                return self._json(400, {"error": "Could not read the request."})
            if self.path == "/apply":
                return self._apply(body)
            topic = str(body.get("topic") or "").strip()[:200]
            slug = str(body.get("slug") or "").strip()
            if slug and not (re.fullmatch(r"[a-z0-9-]{1,80}", slug) and (config.OUT / slug / "brief.json").exists()):
                return self._json(400, {"error": "There is no video with that name."})
            if dashboard.autopilot_running():
                return self._json(409, {"error": "The autopilot is already working on a video. Wait for it to stop."})
            args = [sys.executable, "-m", "pipeline", "autopilot"]
            if slug:
                args.append(f"--slug={slug}")
            elif topic:
                args.append(f"--topic={topic}")
            logs = config.OUT / "_crew"
            logs.mkdir(parents=True, exist_ok=True)
            out = open(logs / "autopilot.log", "a")
            subprocess.Popen(args, cwd=str(config.ROOT), stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            self._json(200, {"ok": True, "slug": slug or (re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:60].rstrip("-") if topic else "")})

        def _apply(self, body: dict) -> None:
            """Apply one of the overseer's script edits that the page showed, then carry on from the fact-check."""
            from . import autopilot
            from .common import StageError, Video
            slug = str(body.get("slug") or "")
            if not (re.fullmatch(r"[a-z0-9-]{1,80}", slug) and (config.OUT / slug / "brief.json").exists()):
                return self._json(400, {"error": "There is no video with that name."})
            try:
                i = int(body.get("i"))
                v = Video(slug)
                fix = autopilot.pending_line_fixes(v)[i]
                if dashboard.autopilot_running():
                    return self._json(409, {"error": "The autopilot is already working on a video. Wait for it to stop."})
                autopilot.apply_line_fix(v, fix["find"], fix["replace"])
            except (ValueError, IndexError, TypeError):
                return self._json(400, {"error": "That edit is no longer available."})
            except StageError as e:
                return self._json(400, {"error": str(e)})
            logs = config.OUT / "_crew"
            out = open(logs / "autopilot.log", "a")
            subprocess.Popen([sys.executable, "-m", "pipeline", "autopilot", f"--slug={slug}", "--from-agent=script_factcheck"],
                             cwd=str(config.ROOT), stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            self._json(200, {"ok": True, "slug": slug})

    return Handler


def run(a) -> None:
    token = secrets.token_urlsafe(16)
    port_box = [0]
    server = None
    for port in ([a.port] if getattr(a, "port", None) else PORTS):
        try:
            port_box[0] = port
            server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(token, port_box))
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("Could not open a local port for the dashboard. Close other copies of Start Crew and try again.")
    url = f"http://127.0.0.1:{port_box[0]}/"
    log(f"Dashboard with a Start button: {url}\nLeave this window open. Press Ctrl-C to close the dashboard (a run in progress keeps going).")
    if not getattr(a, "no_open", False):
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Closed.")
