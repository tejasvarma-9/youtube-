"""The crew dashboard: one page on your Mac showing which agent is doing what, live.

python -m pipeline dashboard          # rebuild it and open it in your browser
                                      # (the crew also opens it when you start a run, and keeps it up to date)

out/_crew/dashboard.html lists every video the crew has worked on. For the one you pick it shows the seven agents
in order: waiting, running, passed, stopped or skipped, how long each took, what it checked, what it fixed, what
it could not fix, what it spent, the Shorts, the overseer's GO / NO-GO with its fixes, and the live log of the
current run. While a run is going the page reloads itself every few seconds.

The page only shows. Nothing on it approves, uploads or posts anything.

Per video the crew keeps agents/status.json (the state of each agent in the current run) and agents/run.log
(everything the run printed).
"""

from __future__ import annotations

import html
import json
import os
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from . import config

AGENTS = [
    ("topic_scout", "Topic scout", "Finds 3 to 5 topics with demand and 3+ primary sources each; you pick one."),
    ("script_factcheck", "Script and fact-check", "Writes the script, checks every fact against its source, revises up to 3 times."),
    ("voice_captions", "Voice and captions", "Records the voice, listens back for caption drift and garbled lines, re-records them."),
    ("images", "Pictures", "Draws every shot, has Claude look at each one, redraws the ones with serious problems."),
    ("packaging", "Packaging", "Builds the video, title, thumbnail and description, runs the full quality check, fixes claims."),
    ("shorts", "Shorts", "Chooses the Shorts, has them reviewed, re-picks rejected ones, builds and checks each one."),
    ("overseer", "Overseer", "A fresh check of everything: re-reads the riskiest sources, gives GO or NO-GO with fixes."),
]
STALE_S = 15 * 60
REFRESH_S = 4
_last_build = [0.0]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s: str) -> float:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def page_path() -> Path:
    p = config.OUT / "_crew"
    p.mkdir(parents=True, exist_ok=True)
    return p / "dashboard.html"


# --- status, written by the crew while it runs -----------------------------------------------

def _status_file(v) -> Path:
    return v.path("agents", "status.json")


def read_status(v) -> dict:
    p = v.dir / "agents" / "status.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def _write_status(v, st: dict) -> None:
    st["updated"] = _now()
    _status_file(v).write_text(json.dumps(st, indent=2) + "\n")
    build(throttle=False)


def run_started(v, plan: dict[str, str]) -> None:
    """plan: agent -> "waiting" (will run) or "skipped" (already done)."""
    st = {"running": True, "pid": os.getpid(), "started": _now(), "current": "", "outcome": "",
          "agents": {name: {"state": state} for name, state in plan.items()}}
    scout = (v.dir / "agents" / "topic_scout.json")
    if scout.exists():
        st["agents"]["topic_scout"] = {"state": "passed" if json.loads(scout.read_text()).get("passed") else "stopped"}
    _write_status(v, st)


def agent_started(v, name: str) -> None:
    st = read_status(v)
    st.setdefault("agents", {})[name] = {"state": "running", "started": _now()}
    st["current"] = name
    _write_status(v, st)


def agent_finished(v, name: str, passed: bool) -> None:
    st = read_status(v)
    a = st.setdefault("agents", {}).setdefault(name, {})
    a.update(state="passed" if passed else "stopped", finished=_now())
    st["current"] = ""
    _write_status(v, st)


def run_finished(v, outcome: str) -> None:
    st = read_status(v)
    st.update(running=False, current="", outcome=outcome, finished=_now())
    for a in st.get("agents", {}).values():
        if a.get("state") == "waiting":
            a["state"] = "not run"
        if a.get("state") == "running":
            a["state"] = "stopped"
    _write_status(v, st)


def log_hook(v):
    """A function for common.log: every line goes to agents/run.log and the page is refreshed (at most every 2 s)."""
    path = v.path("agents", "run.log")
    with path.open("a") as f:
        f.write(f"\n=== run started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    def hook(msg: str) -> None:
        with path.open("a") as f:
            f.write(msg.rstrip("\n") + "\n")
        build(throttle=True)
    return hook


# --- the page -------------------------------------------------------------------

def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _video(d: Path) -> dict:
    agents_dir = d / "agents"
    reports = {}
    for name, _, _ in AGENTS:
        p = agents_dir / f"{name}.json"
        if p.exists():
            try:
                reports[name] = json.loads(p.read_text())
            except ValueError:
                pass
    try:
        st = json.loads((agents_dir / "status.json").read_text()) if (agents_dir / "status.json").exists() else {}
    except ValueError:
        st = {}
    if st.get("running") and time.time() - _ts(st.get("updated")) > STALE_S and not _alive(st.get("pid")):
        st["running"], st["outcome"] = False, "interrupted"
    try:
        verdict = json.loads((agents_dir / "VERDICT.json").read_text()) if (agents_dir / "VERDICT.json").exists() else None
    except ValueError:
        verdict = None
    log = ""
    if (agents_dir / "run.log").exists():
        text = (agents_dir / "run.log").read_text(errors="replace")
        last = text.rfind("=== run started")
        log = "\n".join((text[last:] if last >= 0 else text).splitlines()[-80:])
    title = d.name
    for name in ("metadata.json", "brief.json"):
        try:
            data = json.loads((d / name).read_text())
            title = data.get("title") or data.get("topic") or title
            break
        except (OSError, ValueError):
            continue
    updated = max([_ts(st.get("updated"))] + [_ts(r.get("timestamp")) for r in reports.values()]
                  + [_ts((verdict or {}).get("timestamp"))])
    return {"slug": d.name, "title": title, "reports": reports, "status": st, "verdict": verdict, "log": log,
            "updated": updated}


def collect() -> list[dict]:
    vids = []
    if config.OUT.exists():
        for d in config.OUT.iterdir():
            if d.is_dir() and not d.name.startswith(("_", ".")) and (d / "agents").is_dir():
                vids.append(_video(d))
    vids.sort(key=lambda x: (not x["status"].get("running"), -x["updated"]))
    return vids


E = html.escape


def _when(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%d %b %H:%M")


def _duration(a: dict) -> str:
    s, f = _ts(a.get("started")), _ts(a.get("finished"))
    if not s:
        return ""
    secs = int((f or time.time()) - s)
    return f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"


def _state(name: str, vid: dict) -> str:
    a = vid["status"].get("agents", {}).get(name, {})
    state = a.get("state")
    if state == "running" and not vid["status"].get("running"):
        state = "stopped"
    if state:
        return state
    r = vid["reports"].get(name)
    return ("passed" if r["passed"] else "stopped") if r else "not run"


LABEL = {"waiting": "Waiting", "running": "Running", "passed": "Passed", "stopped": "Stopped", "skipped": "Already done",
         "not run": "Not run"}


def _list(title: str, items: list, cls: str = "", open_: bool = False) -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{E(str(x))}</li>" for x in items)
    return f"<details class='{cls}'{' open' if open_ else ''}><summary>{E(title)} ({len(items)})</summary><ul>{lis}</ul></details>"


def _agent_card(k: int, name: str, title: str, what: str, vid: dict) -> str:
    state = _state(name, vid)
    a = vid["status"].get("agents", {}).get(name, {})
    r = vid["reports"].get(name) or {}
    fresh = r and (state in ("passed", "stopped", "skipped") or not a)
    meta = []
    if _duration(a):
        meta.append(_duration(a))
    if fresh and r.get("retries_used"):
        meta.append(f"{r['retries_used']} retr{'y' if r['retries_used'] == 1 else 'ies'}")
    if fresh and r.get("cost_usd"):
        meta.append(f"${r['cost_usd']:.2f}")
    if fresh and r.get("timestamp"):
        meta.append(f"report {_when(_ts(r['timestamp']))}")
    body = ""
    if fresh:
        body += _list("Could not fix", r.get("could_not_fix", []), "bad", open_=True)
        body += _list("Fixed", r.get("fixed", []), "good", open_=state == "passed")
        body += _list("Checked", r.get("checked", []), "")
        body += _list("Worth a look", r.get("warnings", []), "warn")
        shorts = (r.get("details") or {}).get("shorts")
        if name == "shorts" and shorts:
            rows = "".join(f"<tr><td>Short {E(n)}</td><td><span class='chip {('passed' if s.get('status') == 'READY' else 'stopped')}'>"
                           f"{E(s.get('status', ''))}</span></td><td>{E('; '.join(s.get('problems', []) + s.get('failures', []))[:300])}</td></tr>"
                           for n, s in shorts.items())
            body += f"<table class='mini'>{rows}</table>"
    elif state == "running":
        body = "<p class='muted'>Working on it now. The live log below shows each step.</p>"
    return (f"<li class='agent {state}'><div class='dot'>{k}</div><div class='card'>"
            f"<div class='row'><h3>{E(title)}</h3><span class='chip {state}'>{LABEL.get(state, state)}</span></div>"
            f"<p class='what'>{E(what)}</p>"
            + (f"<p class='meta'>{' · '.join(E(m) for m in meta)}</p>" if meta else "") + body + "</div></li>")


def _verdict(vid: dict) -> str:
    v = vid["verdict"]
    if not v:
        return "<section class='verdict'><h2>Verdict</h2><p class='muted'>No verdict yet: the overseer runs last.</p></section>"
    f = v["full_video"]
    out = [f"<section class='verdict'><h2>Verdict <span class='muted'>{E(_when(_ts(v.get('timestamp'))))}</span></h2>"]
    if v.get("summary"):
        out.append(f"<p>{E(v['summary'])}</p>")

    def block(label, x):
        cls = "go" if x["verdict"] == "GO" else "nogo"
        inner = _list("Why", x.get("reasons", []), "", open_=True) + _list("Fixes", x.get("fixes", []), "good", open_=True)
        return f"<div class='vbox {cls}'><div class='row'><h3>{E(label)}</h3><span class='chip {cls}'>{E(x['verdict'])}</span></div>{inner}</div>"

    out.append(block("Full video", f))
    for s in v.get("shorts", []):
        out.append(block(f"Short {s['short']}: {s.get('title', '')}", s))
    out.append("<p class='muted small'>A recommendation only. No agent approved, uploaded or posted anything.</p></section>")
    return "".join(out)


def _video_panel(vid: dict) -> str:
    st = vid["status"]
    if st.get("running"):
        cur = dict((n, t) for n, t, _ in AGENTS).get(st.get("current"), "between agents")
        head = f"<span class='chip running'>Running</span> {E(cur)}"
    elif st.get("outcome"):
        head = f"<span class='chip {'passed' if st['outcome'] == 'finished' else 'stopped'}'>{E(st['outcome'].capitalize())}</span>"
    else:
        head = ""
    cards = "".join(_agent_card(k, n, t, w, vid) for k, (n, t, w) in enumerate(AGENTS, start=1))
    log = (f"<section><h2>Live log <span class='muted'>(this run)</span></h2><pre>{E(vid['log'])}</pre></section>"
           if vid["log"] else "")
    return (f"<article id='v-{E(vid['slug'])}' class='video'><header class='vhead'><h1>{E(vid['title'])}</h1>"
            f"<p class='muted'>out/{E(vid['slug'])} · {head}</p></header><ol class='timeline'>{cards}</ol>"
            f"{_verdict(vid)}{log}</article>")


CSS = """
:root{--bg:#f6f7f9;--panel:#fff;--ink:#16202c;--muted:#6a7482;--line:#e3e7ec;--good:#1f8a4c;--goodbg:#e6f5ec;
--bad:#c23b2b;--badbg:#fbeae7;--warn:#9a6a00;--warnbg:#fff4d6;--run:#2563eb;--runbg:#e6efff;--idle:#8a94a3;--idlebg:#eef0f3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1318;--panel:#171c23;--ink:#e7ebf0;--muted:#97a1ae;--line:#2a313b;
--good:#4cc27e;--goodbg:#15301f;--bad:#f07b6c;--badbg:#3a1b17;--warn:#e7b84c;--warnbg:#33290f;--run:#7aa7ff;--runbg:#16244a;
--idle:#8a94a3;--idlebg:#20262e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.top{display:flex;gap:12px;flex-wrap:wrap;justify-content:space-between;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:2}
.top b{font-size:16px}.live{color:var(--run);font-weight:600}.wrap{display:grid;grid-template-columns:260px 1fr;min-height:calc(100vh - 54px)}
nav{border-right:1px solid var(--line);padding:12px;background:var(--panel)}nav a{display:block;padding:10px 12px;border-radius:8px;color:inherit;text-decoration:none;margin-bottom:4px}
nav a:hover{background:var(--idlebg)}nav a.on{background:var(--runbg)}nav .t{font-weight:600;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
nav .s{font-size:12px;color:var(--muted)}main{padding:20px 28px;max-width:980px}.video{display:none}.video.on{display:block}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}h3{font-size:15px;margin:0}.muted{color:var(--muted)}.small{font-size:12px}
.timeline{list-style:none;padding:0;margin:18px 0;position:relative}.timeline:before{content:"";position:absolute;left:15px;top:8px;bottom:8px;width:2px;background:var(--line)}
.agent{display:flex;gap:14px;margin-bottom:12px;position:relative}.dot{flex:0 0 32px;height:32px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:13px;background:var(--idlebg);color:var(--idle);z-index:1;border:2px solid var(--bg)}
.agent.passed .dot,.agent.skipped .dot{background:var(--goodbg);color:var(--good)}.agent.stopped .dot{background:var(--badbg);color:var(--bad)}
.agent.running .dot{background:var(--run);color:#fff;animation:pulse 1.4s infinite}@keyframes pulse{50%{box-shadow:0 0 0 6px var(--runbg)}}
.card{flex:1;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-width:0}.agent.running .card{border-color:var(--run)}
.agent.stopped .card{border-color:var(--bad)}.row{display:flex;justify-content:space-between;align-items:center;gap:10px}
.what{margin:4px 0 0;color:var(--muted);font-size:13px}.meta{margin:6px 0 0;font-size:12px;color:var(--muted)}
.chip{font-size:12px;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap;background:var(--idlebg);color:var(--idle)}
.chip.passed,.chip.skipped,.chip.go{background:var(--goodbg);color:var(--good)}.chip.stopped,.chip.nogo{background:var(--badbg);color:var(--bad)}
.chip.running{background:var(--runbg);color:var(--run)}details{margin-top:8px}summary{cursor:pointer;font-size:13px;font-weight:600}
details ul{margin:6px 0 0;padding-left:18px;font-size:13px}details li{margin:3px 0;overflow-wrap:anywhere}details.bad summary{color:var(--bad)}
details.good summary{color:var(--good)}details.warn summary{color:var(--warn)}table.mini{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
table.mini td{border-top:1px solid var(--line);padding:6px 4px;vertical-align:top}.vbox{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px}
.vbox.go{border-color:var(--good)}.vbox.nogo{border-color:var(--bad)}pre{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;font-size:12px;line-height:1.45;overflow:auto;max-height:420px;white-space:pre-wrap}
.empty{padding:40px;color:var(--muted)}
@media (max-width:760px){.wrap{grid-template-columns:1fr}nav{border-right:0;border-bottom:1px solid var(--line);display:flex;overflow:auto;gap:6px}nav a{min-width:180px}main{padding:16px}}
"""

JS = """
const vids=[...document.querySelectorAll('.video')];
function show(slug){vids.forEach(v=>v.classList.toggle('on',v.id==='v-'+slug));
document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('on',a.dataset.slug===slug));}
function pick(){const h=decodeURIComponent(location.hash.slice(1));const ok=vids.find(v=>v.id==='v-'+h);
show(ok?h:(vids[0]?vids[0].id.slice(2):''));}
window.addEventListener('hashchange',pick);pick();
try{const y=sessionStorage.getItem('y');if(y)window.scrollTo(0,+y);}catch(e){}
if(document.body.dataset.live==='1'){setTimeout(()=>{try{sessionStorage.setItem('y',window.scrollY);}catch(e){}location.reload();},REFRESH);}
"""


def render(vids: list[dict]) -> str:
    live = any(v["status"].get("running") for v in vids)
    nav = []
    for v in vids:
        st = v["status"]
        if st.get("running"):
            tag = "Running"
        elif v["verdict"]:
            tag = "Full video " + v["verdict"]["full_video"]["verdict"]
        else:
            tag = (st.get("outcome") or "").capitalize() or "No verdict yet"
        nav.append(f"<a href='#{E(v['slug'])}' data-slug='{E(v['slug'])}'><span class='t'>{E(v['title'])}</span>"
                   f"<span class='s'>{E(tag)} · {E(_when(v['updated']))}</span></a>")
    panels = "".join(_video_panel(v) for v in vids) or \
        "<p class='empty'>No crew runs yet. Start one with: python -m pipeline crew</p>"
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Crew dashboard</title><style>{CSS}</style></head><body data-live='{1 if live else 0}'>"
            f"<div class='top'><b>Who Pays Who · Crew</b><span class='{'live' if live else 'muted'}'>"
            f"{'● Live, updating every few seconds' if live else 'Updated ' + datetime.now().strftime('%d %b %H:%M:%S')}</span></div>"
            f"<div class='wrap'><nav>{''.join(nav)}</nav><main>{panels}</main></div>"
            f"<script>{JS.replace('REFRESH', str(REFRESH_S * 1000))}</script></body></html>")


def build(throttle: bool = False) -> Path | None:
    """Rewrite the page (at most every 2 s when throttled). Never raises: the page must not stop a run."""
    if throttle and time.time() - _last_build[0] < 2:
        return None
    _last_build[0] = time.time()
    try:
        p = page_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(render(collect()))
        tmp.replace(p)
        return p
    except Exception:
        return None


def open_page() -> Path:
    p = build() or page_path()
    webbrowser.open(p.resolve().as_uri())
    return p
