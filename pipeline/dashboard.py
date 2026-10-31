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


def _secs(a: dict) -> int:
    s, f = _ts(a.get("started")), _ts(a.get("finished"))
    return int((f or time.time()) - s) if s else 0


def _fmt(secs: int) -> str:
    if secs <= 0:
        return ""
    return f"{secs // 3600}h {secs % 3600 // 60:02d}m" if secs >= 3600 else \
        (f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s")


def _duration(a: dict) -> str:
    return _fmt(_secs(a))


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
SHORT = {"waiting": "Waiting", "running": "Running", "passed": "Passed", "stopped": "Stopped", "skipped": "Done earlier",
         "not run": "Not run"}
ICON = {"passed": "✓", "skipped": "✓", "stopped": "!", "running": "", "waiting": "", "not run": ""}


def _group(kind: str, title: str, items: list, open_: bool = False) -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{E(str(x))}</li>" for x in items)
    return (f"<details class='grp {kind}'{' open' if open_ else ''}><summary><span class='n'>{len(items)}</span>{E(title)}</summary>"
            f"<ul>{lis}</ul></details>")


def _station(k: int, name: str, title: str, vid: dict) -> str:
    state = _state(name, vid)
    a = vid["status"].get("agents", {}).get(name, {})
    r = vid["reports"].get(name) or {}
    sub = _duration(a) or SHORT.get(state, "")
    flags = ""
    if state in ("not run", "waiting"):
        flags = ""  # an old report's flags would mislead: this agent has not run in the current run
    elif r and (r.get("could_not_fix") or state == "stopped"):
        flags = "<i class='flag bad' title='Could not fix something'></i>"
    elif r and r.get("warnings"):
        flags = "<i class='flag warn' title='Worth a look'></i>"
    return (f"<button class='st {state}' data-agent='{name}' aria-label='{E(title)}: {E(LABEL.get(state, state))}'>"
            f"<span class='node'><b>{ICON.get(state) or k}</b></span>{flags}"
            f"<span class='stt'>{E(title)}</span><span class='sts'>{E(sub)}</span></button>")


def _detail(name: str, title: str, what: str, vid: dict) -> str:
    state = _state(name, vid)
    a = vid["status"].get("agents", {}).get(name, {})
    r = vid["reports"].get(name) or {}
    fresh = r and (state in ("passed", "stopped", "skipped") or not a)
    meta = []
    if _duration(a):
        meta.append(("Time", _duration(a)))
    if fresh and r.get("retries_used"):
        meta.append(("Retries", str(r["retries_used"])))
    if fresh and r.get("cost_usd"):
        meta.append(("Spent", f"${r['cost_usd']:.2f}"))
    if fresh and r.get("timestamp"):
        meta.append(("Report", _when(_ts(r["timestamp"]))))
    meta_html = "".join(f"<div><dt>{E(k)}</dt><dd>{E(v)}</dd></div>" for k, v in meta)
    body = ""
    if fresh:
        body += _group("bad", "Could not fix", r.get("could_not_fix", []), True)
        body += _group("good", "Fixed", r.get("fixed", []), state == "passed")
        body += _group("warn", "Worth a look", r.get("warnings", []))
        body += _group("ok", "Checked", r.get("checked", []))
        shorts = (r.get("details") or {}).get("shorts")
        if name == "shorts" and shorts:
            rows = "".join(f"<tr><th>Short {E(n)}</th><td><span class='chip {('passed' if s.get('status') == 'READY' else 'stopped')}'>"
                           f"{E(s.get('status', ''))}</span></td><td>{E('; '.join(s.get('problems', []) + s.get('failures', []))[:300])}</td></tr>"
                           for n, s in shorts.items())
            body += f"<table class='mini'>{rows}</table>"
        if not body:
            body = "<p class='quiet'>This agent reported nothing to list.</p>"
    elif state == "running":
        body = "<p class='quiet'>Working now. Each step appears in the live log.</p>"
    elif state == "waiting":
        body = "<p class='quiet'>Waiting for the agent before it.</p>"
    else:
        body = "<p class='quiet'>No report yet. Run <code>python -m pipeline crew</code> to start this agent.</p>"
    return (f"<section class='detail' data-agent='{name}' hidden><header><div><h2>{E(title)}</h2><p>{E(what)}</p></div>"
            f"<span class='chip {state}'>{E(LABEL.get(state, state))}</span></header>"
            f"{'<dl class=meta>' + meta_html + '</dl>' if meta_html else ''}"
            f"<input class='find' type='search' placeholder='Filter these lines' aria-label='Filter lines'>{body}</section>")


def _verdict(vid: dict) -> str:
    v = vid["verdict"]
    if not v:
        return ("<section class='ledger empty'><h2>Verdict</h2><p class='quiet'>No verdict yet. The overseer runs last and writes "
                "GO or NO-GO for the full video and each Short.</p></section>")
    rows = []

    def row(label, x):
        cls = "go" if x["verdict"] == "GO" else "nogo"
        fixes = "".join(f"<li>{E(str(i))}</li>" for i in x.get("fixes", []))
        why = "".join(f"<li>{E(str(i))}</li>" for i in x.get("reasons", []))
        more = ""
        if why or fixes:
            more = (f"<div class='vmore'>{'<h4>Why</h4><ul>' + why + '</ul>' if why else ''}"
                    f"{'<h4>Fixes</h4><ul class=fx>' + fixes + '</ul>' if fixes else ''}</div>")
        rows.append(f"<details class='vrow {cls}'{' open' if cls == 'nogo' and label == 'Full video' else ''}><summary>"
                    f"<span class='vl'>{E(label)}</span><span class='stamp {cls}'>{E(x['verdict'])}</span></summary>{more}</details>")

    row("Full video", v["full_video"])
    for s in v.get("shorts", []):
        row(f"Short {s['short']}" + (f": {s['title']}" if s.get("title") else ""), s)
    summ = f"<p class='lede'>{E(v['summary'])}</p>" if v.get("summary") else ""
    return (f"<section class='ledger'><h2>Verdict <small>{E(_when(_ts(v.get('timestamp'))))}</small></h2>{summ}{''.join(rows)}"
            "<p class='quiet'>A recommendation only. No agent approved, uploaded or posted anything.</p></section>")


def _totals(vid: dict) -> str:
    cost = sum(float(r.get("cost_usd") or 0) for r in vid["reports"].values())
    secs = sum(_secs(a) for a in vid["status"].get("agents", {}).values())
    parts = [p for p in (f"${cost:.2f} spent" if cost else "", f"{_fmt(secs)} of agent time" if secs else "") if p]
    return " · ".join(parts)


def _video_panel(vid: dict) -> str:
    st = vid["status"]
    states = [_state(n, vid) for n, _, _ in AGENTS]
    done = sum(s in ("passed", "skipped") for s in states)
    pct = round(100 * done / len(AGENTS))
    if st.get("running"):
        cur = dict((n, t) for n, t, _ in AGENTS).get(st.get("current"), "between agents")
        head = f"<span class='chip running'>Running</span><span>{E(cur)}</span>"
    elif st.get("outcome"):
        head = f"<span class='chip {'passed' if st['outcome'] == 'finished' else 'stopped'}'>{E(st['outcome'].capitalize())}</span>"
    else:
        head = ""
    first_open = next((n for (n, _, _), s in zip(AGENTS, states) if s == "running"),
                      next((n for (n, _, _), s in zip(AGENTS, states) if s == "stopped"), AGENTS[-1][0]))
    stations = "".join(_station(k, n, t, vid) for k, (n, t, _) in enumerate(AGENTS, start=1))
    details = "".join(_detail(n, t, w, vid) for n, t, w in AGENTS)
    log = (f"<pre>{E(vid['log'])}</pre>" if vid["log"] else "<p class='quiet'>Nothing logged yet.</p>")
    return (f"<article id='v-{E(vid['slug'])}' class='video' data-open='{first_open}'>"
            f"<header class='vhead'><div class='status'>{head}<span class='tot'>{E(_totals(vid))}</span></div>"
            f"<h1>{E(vid['title'])}</h1><p class='path'>out/{E(vid['slug'])}</p></header>"
            f"<div class='rail' style='--p:{pct}'><div class='track'><div class='fill'></div></div>{stations}</div>"
            f"<div class='stage'>{details}</div>{_verdict(vid)}"
            f"<aside class='drawer'><button class='dtog' aria-expanded='false'>Live log</button><div class='dbody'>{log}</div></aside></article>")


CSS = """
:root{--bg:#eef2ee;--panel:#fbfcfa;--ink:#13241d;--muted:#5d6f66;--line:#d5ded7;--money:#0f7b4f;--moneybg:#d9eee1;
--bad:#c93d2d;--badbg:#fbe4df;--warn:#9a6700;--warnbg:#f8ecc9;--run:#2152e8;--runbg:#dfe7ff;--idle:#8a9890;--idlebg:#e4eae5;
--serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;--sans:ui-rounded,"SF Pro Rounded",-apple-system,"Segoe UI",sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#0d1612;--panel:#142019;--ink:#e6efe9;--muted:#93a69b;--line:#26382e;--money:#4cc98a;--moneybg:#143325;
--bad:#f2806f;--badbg:#3a1c17;--warn:#e9bb4f;--warnbg:#352a0d;--run:#86a8ff;--runbg:#16254f;--idle:#7d8f85;--idlebg:#1b2a22}}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
button{font:inherit;color:inherit}:focus-visible{outline:3px solid var(--run);outline-offset:2px;border-radius:6px}
.top{display:flex;align-items:center;gap:14px;padding:12px clamp(16px,4vw,40px);background:var(--panel);border-bottom:1px solid var(--line)}
.brand{font:700 18px var(--serif);letter-spacing:.2px}.brand span{color:var(--money)}
.pick{margin-left:auto;display:flex;gap:6px;overflow:auto;max-width:60%}
.pick a{padding:6px 12px;border-radius:999px;color:var(--muted);text-decoration:none;white-space:nowrap;font-size:13px;border:1px solid transparent}
.pick a:hover{border-color:var(--line)}.pick a.on{background:var(--ink);color:var(--bg)}
.live{display:inline-flex;align-items:center;gap:7px;font-size:13px;color:var(--run);font-weight:600}.live:before{content:"";width:8px;height:8px;border-radius:50%;background:var(--run);animation:beat 1.2s infinite}
@keyframes beat{50%{opacity:.25}}.idle{font-size:12px;color:var(--muted)}
main{max-width:1120px;margin:0 auto;padding:32px clamp(16px,4vw,40px) 120px}.video{display:none}.video.on{display:block}
.status{display:flex;gap:10px;align-items:center;font-size:13px;color:var(--muted);flex-wrap:wrap}.tot{margin-left:auto}
h1{font:700 clamp(28px,4.4vw,44px)/1.1 var(--serif);margin:14px 0 6px;letter-spacing:-.5px;max-width:22ch;text-wrap:balance}
.path{margin:0;color:var(--muted);font-size:13px}
.chip{font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px;background:var(--idlebg);color:var(--idle);white-space:nowrap}
.chip.passed,.chip.skipped{background:var(--moneybg);color:var(--money)}.chip.stopped{background:var(--badbg);color:var(--bad)}.chip.running{background:var(--runbg);color:var(--run)}
.rail{position:relative;display:grid;grid-template-columns:repeat(7,1fr);margin:40px 0 8px}
.track{position:absolute;left:calc(100%/14);right:calc(100%/14);top:22px;height:4px;border-radius:2px;background:var(--line)}
.fill{height:100%;width:calc(var(--p)*1% * 7/6);max-width:100%;background:var(--money);border-radius:2px;transition:width .6s}
.st{position:relative;background:none;border:0;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:6px;padding:0 4px 10px;text-align:center}
.node{width:48px;height:48px;border-radius:50%;display:grid;place-items:center;background:var(--panel);border:3px solid var(--line);color:var(--idle);font:700 17px var(--serif);transition:transform .15s,border-color .15s;position:relative;z-index:1}
.st:hover .node{transform:translateY(-2px)}
.st.passed .node,.st.skipped .node{border-color:var(--money);background:var(--moneybg);color:var(--money)}
.st.stopped .node{border-color:var(--bad);background:var(--badbg);color:var(--bad)}
.st.running .node{border-color:var(--run);background:var(--runbg);color:var(--run);animation:ring 1.6s infinite}
@keyframes ring{0%{box-shadow:0 0 0 0 var(--runbg)}70%{box-shadow:0 0 0 14px transparent}}
.st.running .node b{width:12px;height:12px;border-radius:50%;background:var(--run);font-size:0}
.st.on .node{transform:scale(1.12);box-shadow:0 0 0 4px var(--bg),0 0 0 6px var(--ink)}
.stt{font-weight:650;font-size:13.5px;line-height:1.25}.sts{font-size:12px;color:var(--muted)}
.flag{position:absolute;top:0;left:calc(50% + 12px);width:12px;height:12px;border-radius:50%;z-index:2;border:2px solid var(--bg)}.flag.bad{background:var(--bad)}.flag.warn{background:var(--warn)}
.stage{margin-top:12px}
.detail{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:24px clamp(16px,3vw,32px)}
.detail>header{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}
.detail h2{font:700 24px var(--serif);margin:0}.detail header p{margin:4px 0 0;color:var(--muted);max-width:60ch}
.meta{display:flex;gap:28px;flex-wrap:wrap;margin:18px 0 6px;padding:0}.meta dt{font-size:12px;color:var(--muted)}.meta dd{margin:0;font:700 19px var(--serif)}
.find{width:100%;margin:14px 0 4px;padding:9px 12px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit}
.grp{border-top:1px solid var(--line);padding:12px 0}.grp summary{cursor:pointer;font-weight:650;display:flex;align-items:center;gap:10px;list-style:none}
.grp summary::-webkit-details-marker{display:none}.grp summary:after{content:"+";margin-left:auto;color:var(--muted);font-size:18px}.grp[open] summary:after{content:"–"}
.n{min-width:26px;height:26px;border-radius:13px;display:inline-grid;place-items:center;font-size:12px;font-weight:700;background:var(--idlebg)}
.grp.bad .n{background:var(--bad);color:#fff}.grp.good .n{background:var(--money);color:#fff}.grp.warn .n{background:var(--warn);color:#fff}
.grp ul{margin:10px 0 0;padding-left:36px;font-size:14px}.grp li{margin:6px 0;overflow-wrap:anywhere;max-width:84ch}.grp li.hide{display:none}
table.mini{width:100%;border-collapse:collapse;margin-top:10px;font-size:14px}table.mini th{text-align:left;font-weight:650;width:90px}
table.mini th,table.mini td{border-top:1px solid var(--line);padding:9px 6px;vertical-align:top}
.quiet{color:var(--muted);font-size:14px}code{background:var(--idlebg);padding:1px 6px;border-radius:5px;font-size:13px}
.ledger{margin-top:28px;background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:24px clamp(16px,3vw,32px)}
.ledger h2{font:700 24px var(--serif);margin:0 0 4px}.ledger h2 small{font:400 13px var(--sans);color:var(--muted);margin-left:8px}.lede{max-width:70ch;margin:8px 0 14px}
.vrow{border-top:1px dashed var(--line)}.vrow summary{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 0;cursor:pointer;list-style:none;font-weight:650}
.vrow summary::-webkit-details-marker{display:none}
.stamp{font:800 14px var(--sans);letter-spacing:1px;padding:5px 14px;border:2.5px solid;border-radius:8px;transform:rotate(-3deg);flex:none}
.stamp.go{color:var(--money)}.stamp.nogo{color:var(--bad)}
.vmore{padding:0 0 16px;font-size:14px}.vmore h4{margin:8px 0 4px;font-size:13px;color:var(--muted)}.vmore ul{margin:0;padding-left:20px}.vmore li{margin:5px 0;max-width:84ch}
.vmore .fx li::marker{color:var(--money)}
.drawer{position:fixed;left:0;right:0;bottom:0;z-index:6;background:#0b1410;color:#cfe5d8;border-top:1px solid var(--line)}
.dtog{width:100%;text-align:left;padding:10px clamp(16px,4vw,40px);background:none;border:0;cursor:pointer;font-weight:650;display:flex;gap:8px}
.dtog:after{content:"▴";margin-left:auto}.dtog[aria-expanded=true]:after{content:"▾"}
.dbody{display:none;max-height:42vh;overflow:auto;padding:0 clamp(16px,4vw,40px) 16px}.dbody.open{display:block}
.dbody pre{margin:0;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap}
@media (max-width:820px){
.rail{grid-template-columns:1fr;margin:24px 0 8px}.track{left:23px;right:auto;top:24px;bottom:24px;width:4px;height:auto}.fill{width:100%;height:calc(var(--p)*1%)}
.st{flex-direction:row;text-align:left;gap:14px;padding:8px 0}.node{width:44px;height:44px;flex:none}.sts{margin-left:auto}.flag{left:34px;top:6px}
.pick{max-width:none}.top{flex-wrap:wrap}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

JS = """
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const vids=$$('.video');let cur='';
const save=(k,v)=>{try{sessionStorage.setItem(k,v)}catch(e){}},load=k=>{try{return sessionStorage.getItem(k)}catch(e){return null}};
function showAgent(v,name){$$('.detail',v).forEach(d=>d.hidden=d.dataset.agent!==name);
$$('.st',v).forEach(s=>s.classList.toggle('on',s.dataset.agent===name));save('a-'+cur,name);}
function showVideo(slug){cur=slug;vids.forEach(v=>v.classList.toggle('on',v.id==='v-'+slug));
$$('.pick a').forEach(a=>a.classList.toggle('on',a.dataset.slug===slug));
const v=$('#v-'+slug);if(!v)return;const names=$$('.st',v).map(s=>s.dataset.agent);
const want=load('a-'+slug);showAgent(v,names.includes(want)?want:v.dataset.open);}
function pick(){const h=decodeURIComponent(location.hash.slice(1));showVideo(vids.some(v=>v.id==='v-'+h)?h:(vids[0]?vids[0].id.slice(2):''));}
document.addEventListener('click',e=>{const s=e.target.closest('.st');if(s)showAgent(s.closest('.video'),s.dataset.agent);
const t=e.target.closest('.dtog');if(t){const o=t.getAttribute('aria-expanded')!=='true';t.setAttribute('aria-expanded',o);
t.nextElementSibling.classList.toggle('open',o);save('log',o?'1':'0');if(o)t.nextElementSibling.scrollTop=1e9;}});
document.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight'].includes(e.key)||/INPUT/.test(e.target.tagName))return;
const v=$('.video.on');if(!v)return;const s=$$('.st',v),i=s.findIndex(x=>x.classList.contains('on'));
const n=s[Math.max(0,Math.min(s.length-1,i+(e.key==='ArrowRight'?1:-1)))];showAgent(v,n.dataset.agent);n.focus();});
document.addEventListener('input',e=>{if(!e.target.classList.contains('find'))return;const q=e.target.value.toLowerCase();
$$('.grp',e.target.parentElement).forEach(g=>{let hit=0;$$('li',g).forEach(li=>{const m=!q||li.textContent.toLowerCase().includes(q);li.classList.toggle('hide',!m);hit+=m;});
g.hidden=!hit;if(q&&hit)g.open=true;});});
window.addEventListener('hashchange',pick);pick();
if(load('log')==='1'){const t=$('.video.on .dtog');if(t)t.click();}
try{const y=sessionStorage.getItem('y');if(y)window.scrollTo(0,+y);}catch(e){}
if(document.body.dataset.live==='1'){setTimeout(()=>{save('y',window.scrollY);location.reload();},REFRESH);}
"""


def render(vids: list[dict]) -> str:
    live = any(v["status"].get("running") for v in vids)
    nav = "".join(f"<a href='#{E(v['slug'])}' data-slug='{E(v['slug'])}'>{E(v['title'])}</a>" for v in vids)
    panels = "".join(_video_panel(v) for v in vids) or \
        "<p class='quiet'>No crew runs yet. Start one with <code>python -m pipeline crew</code>.</p>"
    status = ("<span class='live'>Live</span>" if live else
              f"<span class='idle'>Updated {datetime.now().strftime('%d %b %H:%M')}</span>")
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Crew dashboard</title><style>{CSS}</style></head><body data-live='{1 if live else 0}'>"
            f"<div class='top'><div class='brand'>Who Pays <span>Who</span></div>{status}<nav class='pick'>{nav}</nav></div>"
            f"<main>{panels}</main><script>{JS.replace('REFRESH', str(REFRESH_S * 1000))}</script></body></html>")


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
