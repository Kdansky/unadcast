"""The admin page: which shows to process, a "process now" button, and what the last pass did.

Standalone on purpose: starlette, httpx and the standard library only, so it runs in a small image on any always-on machine. The
processing machine reads the show list from /admin/api/feeds and polls /admin/api/run between passes.
"""

import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

MODES = ("mark", "cut")
DETECTORS = ("repeat", "voice", "loudness", "llm")


def itunes(term):
    r = httpx.get("https://itunes.apple.com/search", params={"media": "podcast", "limit": 8, "term": term}, timeout=20)
    r.raise_for_status()
    return r.json()["results"]


def title_of(url):
    """The feed's title, or ValueError if the URL doesn't serve a podcast feed."""
    r = httpx.get(url, follow_redirects=True, timeout=20, headers={"User-Agent": "unadcast/0.1"})
    if r.status_code != 200:
        raise ValueError(f"the server answered {r.status_code}")
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        raise ValueError("that is not an RSS feed") from None
    title = root.findtext("channel/title")
    if root.tag != "rss" or not title:
        raise ValueError("that is not an RSS feed")
    return title.strip()


def slugify(title, taken):
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "show"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def app(feeds_path, status_path):
    feeds_path, status_path = Path(feeds_path), Path(status_path)
    state_path = feeds_path.with_name("run.json")
    pc_path = feeds_path.with_name("pc.json")
    settings_path = feeds_path.with_name("settings.json")
    lock = threading.Lock()

    def load(path, empty):
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return empty

    def save(path, data):
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, path)

    def run_state():
        return {"requested": 0, "stop": 0, "pause": 0} | load(state_path, {})

    def request_run():
        save(state_path, run_state() | {"requested": time.time()})

    def page(request):
        return HTMLResponse(PAGE)

    def list_feeds(request):
        return JSONResponse(load(feeds_path, []))

    async def add(request: Request):
        body = await request.json()
        url = (body.get("url") or "").strip()
        if not re.match(r"https?://", url):
            return JSONResponse({"error": "a feed URL starts with http:// or https://"}, 400)
        try:
            title = title_of(url)
        except (ValueError, httpx.HTTPError) as e:
            return JSONResponse({"error": f"couldn't read a podcast feed there: {e}"}, 400)
        with lock:
            feeds = load(feeds_path, [])
            if any(f["url"] == url for f in feeds):
                return JSONResponse({"error": "that show is already on the list"}, 409)
            f = {"slug": slugify(title, {f["slug"] for f in feeds}), "url": url, "title": title, "keep": 10, "mode": "mark", "added": time.time()}
            save(feeds_path, feeds + [f])
            request_run()
        return JSONResponse(f, 201)

    async def edit(request: Request):
        slug, body = request.path_params["slug"], await request.json()
        if "mode" in body and body["mode"] not in MODES:
            return JSONResponse({"error": f"mode is one of {', '.join(MODES)}"}, 400)
        if "keep" in body and not (isinstance(body["keep"], int) and 1 <= body["keep"] <= 50):
            return JSONResponse({"error": "keep is a whole number from 1 to 50"}, 400)
        if "exclude" in body:
            ok = isinstance(body["exclude"], list) and len(body["exclude"]) <= 50
            ok = ok and all(isinstance(x, str) and len(x.strip()) <= 200 for x in body["exclude"])
            if not ok:
                return JSONResponse({"error": "exclude is a list of title phrases, each under 200 characters"}, 400)
            body["exclude"] = [x.strip() for x in body["exclude"] if x.strip()]
        with lock:
            feeds = load(feeds_path, [])
            f = next((f for f in feeds if f["slug"] == slug), None)
            if not f:
                return JSONResponse({"error": "no such show"}, 404)
            f.update({k: body[k] for k in ("mode", "keep", "exclude") if k in body})
            save(feeds_path, feeds)
        return JSONResponse(f)

    def remove(request):
        with lock:
            feeds = load(feeds_path, [])
            rest = [f for f in feeds if f["slug"] != request.path_params["slug"]]
            if len(rest) == len(feeds):
                return JSONResponse({"error": "no such show"}, 404)
            save(feeds_path, rest)
        return Response(status_code=204)

    def search(request):
        try:
            results = itunes(request.query_params.get("q", ""))
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"the search didn't answer: {e}"}, 502)
        return JSONResponse([
            {"title": r["collectionName"], "author": r.get("artistName", ""), "url": r["feedUrl"], "artwork": r.get("artworkUrl100", "")}
            for r in results if r.get("feedUrl")
        ])

    def status(request):
        # status.json lives on the published share, which can be down while the admin page is fine.
        try:
            return JSONResponse(load(status_path, {}))
        except OSError:
            return JSONResponse({"unreadable": True})

    async def run(request: Request):
        if request.method == "POST":
            request_run()
        return JSONResponse(run_state())

    def stop(request):
        """Stop the running pass and cancel a queued one; the PC's watcher sees it within seconds."""
        save(state_path, run_state() | {"stop": time.time()})
        return JSONResponse(run_state())

    async def pause(request: Request):
        """{"hours": n}: stop now, as Stop does, and start no pass for n hours, e.g. for a game. 0 resumes."""
        hours = (await request.json()).get("hours")
        if not (isinstance(hours, (int, float)) and not isinstance(hours, bool) and 0 <= hours <= 24):
            return JSONResponse({"error": "hours: 0 to 24"}, 400)
        now = time.time()
        save(state_path, run_state() | ({"stop": now, "pause": now + hours * 3600} if hours else {"pause": 0}))
        return JSONResponse(run_state())

    async def settings(request: Request):
        """Which detectors run. The PC applies these over its config.toml on every pass."""
        current = {"detect": {d: True for d in DETECTORS} | load(settings_path, {}).get("detect", {})}
        if request.method == "PUT":
            body = (await request.json()).get("detect", {})
            if not all(k in DETECTORS and isinstance(v, bool) for k, v in body.items()):
                return JSONResponse({"error": f"detect takes true or false for {', '.join(DETECTORS)}"}, 400)
            current["detect"] |= body
            save(settings_path, current)
            request_run()
        return JSONResponse(current)

    async def pc(request: Request):
        """The processing machine checks in every 30 s: which one, its repo folder, whether it is mid-pass, and which of
        unadcast's containers are on its GPU, and until when it postpones passes because something else uses the GPU."""
        if request.method == "POST":
            body = await request.json()
            gpu = body.get("gpu", [])
            if not (isinstance(body.get("host"), str) and isinstance(body.get("dir"), str) and isinstance(body.get("busy"), bool)
                    and isinstance(gpu, list) and all(isinstance(g, str) for g in gpu)):
                return JSONResponse({"error": "expected host, dir, busy and gpu"}, 400)
            postponed = body.get("postponed", 0)
            if not isinstance(postponed, (int, float)):
                return JSONResponse({"error": "postponed: a time"}, 400)
            save(pc_path, {"host": body["host"], "dir": body["dir"], "busy": body["busy"], "gpu": gpu, "postponed": postponed,
                           "seen": time.time()})
        return JSONResponse(load(pc_path, {}))

    return Starlette(routes=[
        Route("/admin/", page),
        Route("/admin/api/feeds", list_feeds),
        Route("/admin/api/feeds", add, methods=["POST"]),
        Route("/admin/api/feeds/{slug}", edit, methods=["PATCH"]),
        Route("/admin/api/feeds/{slug}", remove, methods=["DELETE"]),
        Route("/admin/api/search", search),
        Route("/admin/api/status", status),
        Route("/admin/api/run", run, methods=["GET", "POST"]),
        Route("/admin/api/stop", stop, methods=["POST"]),
        Route("/admin/api/pause", pause, methods=["POST"]),
        Route("/admin/api/pc", pc, methods=["GET", "POST"]),
        Route("/admin/api/settings", settings, methods=["GET", "PUT"]),
    ])


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>unadcast</title>
<style>
:root { --bg: #fafaf8; --fg: #1d1d1b; --muted: #6b6b66; --line: #e2e1dc; --card: #fff; --accent: #2f6f4f; --bad: #a33; }
@media (prefers-color-scheme: dark) { :root { --bg: #161615; --fg: #ecebe6; --muted: #9a9a93; --line: #33332f; --card: #1f1f1d; --accent: #6fbf94; --bad: #e07a7a; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.45 system-ui, sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 16px; }
h1 { font-size: 22px; margin: 8px 0 4px; } h2 { font-size: 16px; margin: 28px 0 8px; }
.muted { color: var(--muted); } .bad { color: var(--bad); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin: 10px 0; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
input, select, button { font: inherit; color: inherit; background: var(--card); border: 1px solid var(--line); border-radius: 7px; padding: 7px 10px; }
input[type=search], input[type=url] { flex: 1; min-width: 200px; }
input[type=number] { width: 70px; }
button { cursor: pointer; } button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.show { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.show .name { font-weight: 600; } .url { font-family: ui-monospace, monospace; font-size: 13px; word-break: break-all; }
details summary { cursor: pointer; color: var(--muted); margin-top: 6px; }
ul.eps { margin: 6px 0 0; padding-left: 18px; } ul.eps li { margin: 2px 0; }
.result { display: flex; gap: 10px; align-items: center; } .result img { width: 44px; height: 44px; border-radius: 6px; }
ul.lines { list-style: none; margin: 6px 0 0; padding: 0; } ul.lines li { margin: 3px 0; }
time { font-variant-numeric: tabular-nums; color: var(--muted); margin-right: 6px; }
.old { opacity: .5; }
/* The one place responses appear: pinned to the bottom so it is in view wherever the click was. */
#messages { position: fixed; left: 0; right: 0; bottom: 0; background: var(--card); border-top: 1px solid var(--line); box-shadow: 0 -2px 8px #0002; }
#messages > div { max-width: 860px; margin: 0 auto; padding: 8px 16px; max-height: 35vh; overflow: auto; }
#messages[hidden] { display: none; } main { padding-bottom: 35vh; }
</style></head>
<body><main>
<h1>unadcast</h1>
<div class="row"><button id="run" class="primary">Process now</button><button id="stop">Stop</button><span id="pausing" class="row"></span></div>
<div class="card"><b>Status</b> <span id="asof" class="muted"></span><ul id="status" class="lines"></ul></div>

<h2>Add a show</h2>
<div class="row"><input type="search" id="q" placeholder="Search by name"><button id="find">Search</button></div>
<div id="results"></div>
<div class="row" style="margin-top:8px"><input type="url" id="url" placeholder="…or paste a feed URL"><button id="add">Add</button></div>

<h2>Shows</h2>
<p class="muted">Subscribe to the address under each show in your podcast app. New shows start in <b>mark</b> mode
(ads become chapters, nothing is cut); switch to <b>cut</b> once the chapters look right.</p>
<div id="shows"></div>

<h2>Detectors</h2>
<p class="muted">Which ways of finding ads run on every show. Switching one off just removes its votes; changes apply on
the next pass, which saving starts. Measured on hand-checked episodes, all four together catch the most.</p>
<div id="detectors" class="card"></div>
</main>
<div id="messages" hidden><div><div class="row" style="justify-content:space-between"><b>Messages</b><button id="clear" title="Clear">×</button></div>
<ul id="log" class="lines"></ul></div></div>
<script>
const api = (p, o = {}) => fetch("/admin/api/" + p, {headers: {"Content-Type": "application/json"}, ...o})
  .then(async r => { const b = r.status === 204 ? null : await r.json(); if (!r.ok) throw new Error(b.error || r.status); return b; });
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const now = () => Date.now() / 1000;
// A clock time, with the day when it isn't today.
const at = t => { const d = new Date(t * 1000), hm = d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  return d.toDateString() === new Date().toDateString() ? hm : d.toLocaleDateString([], {month: "short", day: "numeric"}) + " " + hm; };
const clock = t => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});

// Responses to what was clicked, newest first. An entry whose effect has happened (`done` returns what came of it, given
// the latest state) or that is older than STALE seconds is dimmed: it is history, not news.
const STALE = 600, log = [];
let latest = null;
function say(text, bad, done, pending = "waiting") {
  log.unshift({t: now(), text, bad, done, pending});
  log.splice(8);
  settle();
}
function settle() {
  // Only state fetched after the message can tell what came of it.
  for (const m of log) if (m.done && !m.outcome && latest && latest.t > m.t) {
    const o = m.done(latest);
    if (o) m.outcome = o + " " + at(now());
  }
  const box = document.getElementById("messages");
  box.hidden = !log.length;
  document.getElementById("log").innerHTML = log.map(m => `<li class="${m.outcome || now() - m.t > STALE ? "old" : ""}"><time>${clock(m.t)}</time><span class="${m.bad ? "bad" : ""}">${esc(m.text)}</span>`
    + (m.outcome ? ` <span class="muted">→ ${esc(m.outcome)}</span>` : m.done && !m.bad ? ` <span class="muted">→ ${m.pending}</span>` : "") + "</li>").join("");
}
document.getElementById("clear").onclick = () => { log.length = 0; settle(); };
// What a change waits for: a pass that started after it (and so read it) to end.
const applied = t => s => s.st.started > t && !s.st.running && "applied by the pass that ended";
// navigator.clipboard only exists on HTTPS or localhost; this page is plain HTTP on the LAN.
function copy(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const t = Object.assign(document.createElement("textarea"), {value: text});
  Object.assign(t.style, {position: "fixed", top: "0", opacity: "0"});
  document.body.append(t);
  t.select();
  const ok = document.execCommand("copy");
  t.remove();
  return ok ? Promise.resolve() : Promise.reject(new Error("Couldn't copy; select the text and copy it instead"));
}
const mmss = s => Math.floor(s / 60) + ":" + String(Math.round(s % 60)).padStart(2, "0");

async function refresh() {
  let feeds, st, run, pc;
  try { [feeds, st, run, pc] = await Promise.all([api("feeds"), api("status"), api("run"), api("pc")]); }
  catch (e) {
    document.getElementById("asof").innerHTML = `<span class="bad">· couldn't reach the admin page at ${clock(now())}; showing ${latest ? clock(latest.t) : "nothing"}</span>`;
    return;
  }
  const on = pc.seen && now() - pc.seen < 120;
  // status.json can't say "stopped" for a pass that was killed; the PC's own check-in can.
  const busy = st.running && on && pc.busy, cur = busy && st.current;
  const paused = run.pause > now(), postponed = on && pc.postponed > now();
  // A request made during a pass still waits: that pass read the shows and settings before it.
  const waiting = run.requested > (st.started || st.updated || 0) && run.requested > run.stop;
  // Ollama holds the language model, a pass holds Whisper: together most of the card, which a game then lacks.
  const gpu = [...new Set((pc.gpu || []).map(g => ({ollama: "Ollama (language model)", cli: "a pass", worker: "the resident worker"}[g] || g)))];
  latest = {t: now(), st, run, pc, on, busy, gpu};

  const lines = [];
  if (st.unreadable) lines.push('<span class="bad">Can\'t read the status file</span> <span class="muted">· is the share (NAS) it lives on reachable?</span>');
  lines.push(!pc.seen ? "The processing PC hasn't checked in yet: is its watcher running?"
    : on ? `PC <b>${esc(pc.host)}</b> is on <span class="muted">· checked in ${at(pc.seen)}</span>`
    : `<span class="bad">PC <b>${esc(pc.host)}</b> is off, or its watcher stopped</span> <span class="muted">· last checked in ${at(pc.seen)}; passes wait until it's back</span>`);
  lines.push(cur ? `Working: <b>${esc(STAGES[cur.stage] || cur.stage)}</b> ${esc(cur.title)} <span class="muted">(${esc(cur.feed)}), episode ${cur.step} of ${cur.of}, this step since ${at(cur.since)}</span>`
    : busy ? "Working: starting a pass"
    : st.updated ? `Idle <span class="muted">· last pass ended ${at(st.updated)}</span>` : 'Idle <span class="muted">· no pass yet</span>');
  if (paused) lines.push(`<b>Paused until ${at(run.pause)}</b> <span class="muted">· no pass starts before then</span>`);
  if (postponed) lines.push(`GPU busy with something else (a game?) at ${at(pc.postponed - 3600)} <span class="muted">· passes wait until ${at(pc.postponed)}</span>`);
  if (waiting) lines.push(`Pass requested at ${at(run.requested)} <span class="muted">· ${paused || postponed ? "starts after the wait above" : on ? "starts within half a minute" : "starts when the PC is on"}</span>`);
  if (on) lines.push(gpu.length ? `<span class="bad">GPU in use by ${esc(gpu.join(", "))}</span> <span class="muted">· Stop frees it</span>` : 'GPU free <span class="muted">· nothing of unadcast on it</span>');
  const cmd = pc.dir ? `cd ${pc.dir} && bin/now` : "";
  if (cmd) lines.push(`<span class="muted">On the PC, to watch it work:</span> <span class="url">${esc(cmd)}</span> <button id="copycmd">Copy</button>`);
  document.getElementById("status").innerHTML = lines.map(l => `<li>${l}</li>`).join("");
  document.getElementById("asof").textContent = "· as of " + clock(now());
  if (cmd) document.getElementById("copycmd").onclick = () => copy(cmd).then(() => say("Copied the command"), e => say(e.message, true));

  document.getElementById("pausing").innerHTML = paused ? '<button id="resume">Resume</button>' : '<button data-pause="3">Pause 3 h</button><button data-pause="6">Pause 6 h</button>';
  if (paused) document.getElementById("resume").onclick = () => api("pause", {method: "POST", body: JSON.stringify({hours: 0})})
    .then(() => { say("Resumed: passes start again"); refresh(); }).catch(e => say(e.message, true));
  for (const b of document.querySelectorAll("[data-pause]")) b.onclick = () => api("pause", {method: "POST", body: JSON.stringify({hours: +b.dataset.pause})})
    .then(r => { say(`Paused until ${at(r.pause)}`, false, s => s.run.pause !== r.pause ? "resumed" : now() > r.pause && "pause over", "in effect"); refresh(); })
    .catch(e => say(e.message, true));
  settle();
  const known = st.feeds || {};
  document.getElementById("shows").innerHTML = feeds.length ? "" : '<p class="muted">No shows yet.</p>';
  for (const f of feeds) {
    const k = known[f.slug], eps = k ? k.episodes : [];
    const done = eps.filter(e => e.state === "done"), failed = eps.filter(e => e.state === "failed");
    const removed = done.reduce((a, e) => a + (e.removed || 0), 0);
    const url = k ? k.url : location.origin + "/" + f.slug + ".xml";
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = `<div class="show"><div><div class="name">${esc(f.title || f.slug)}</div>
      <div class="url">${esc(url)}</div>
      <div class="muted">${k ? `${done.length}/${eps.length} episodes done` + (removed ? `, ${mmss(removed)} of ads removed` : "") : "not processed yet"}
      ${failed.length ? `<span class="bad"> · ${failed.length} failed</span>` : ""}</div></div>
      <div class="row"><button data-copy>Copy address</button>
      <select data-mode><option value="mark">mark</option><option value="cut">cut</option></select>
      <input type="number" min="1" max="50" value="${f.keep}" data-keep title="newest episodes to process">
      <button data-remove>Remove</button></div></div>
      <div class="row" style="margin-top:8px"><input type="text" data-exclude style="flex:1;min-width:200px"
        placeholder="Skip episodes whose title contains… (comma-separated)" value="${esc((f.exclude || []).join(", "))}"></div>
      ${eps.length ? `<details><summary>Episodes</summary><ul class="eps">${eps.map(e => `<li>${esc(e.title)} <span class="${e.state === "failed" ? "bad" : "muted"}">·
        ${e.state === "done" ? (e.cuts ? `${e.cuts} cut (${mmss(e.removed)})` : "no cuts") + (e.marks ? `, ${e.marks} marked` : "") : e.state === "failed" ? esc(e.error) : "waiting"}</span></li>`).join("")}</ul></details>` : ""}`;
    div.querySelector("[data-mode]").value = f.mode;
    const name = f.title || f.slug, edit = (body, text) => api("feeds/" + f.slug, {method: "PATCH", body: JSON.stringify(body)})
      .then(r => say(`${name}: ${text(r)}`, false, applied(now()))).catch(e => say(e.message, true));
    div.querySelector("[data-copy]").onclick = () => copy(url).then(() => say("Copied " + url), e => say(e.message, true));
    div.querySelector("[data-mode]").onchange = e => edit({mode: e.target.value}, r => r.mode + " mode");
    div.querySelector("[data-keep]").onchange = e => edit({keep: +e.target.value}, r => `the newest ${r.keep} episodes`);
    div.querySelector("[data-exclude]").onchange = e => edit({exclude: e.target.value.split(",")}, r => r.exclude.length ? "skipping episodes with " + r.exclude.join(", ") : "skipping nothing");
    div.querySelector("[data-remove]").onclick = () => confirm("Stop processing " + name + "?") && api("feeds/" + f.slug, {method: "DELETE"})
      .then(() => { say("Removed " + name); refresh(); }).catch(e => say(e.message, true));
    document.getElementById("shows").append(div);
  }
}

const STAGES = {download: "Downloading", prepare: "Preparing", detect_loudness: "Loudness check", detect_voice: "Voice check",
  fingerprint: "Fingerprinting", detect_repeat: "Matching repeats", transcribe: "Transcribing", detect_llm: "Reading the transcript",
  fuse: "Deciding", cut: "Cutting"};
const DETECTORS = [
  ["repeat", "Repeats", "Audio heard in other episodes, of any show: network ad blocks and promos. The strongest signal. CPU, cheap."],
  ["voice", "Voices", "Voices the episode barely uses, like an ad's announcer. Extends cuts across a whole ad break. CPU, about 45 s per hour of audio."],
  ["loudness", "Loudness", "Stretches mastered louder or quieter than the show. Weak alone, a useful second opinion. Free."],
  ["llm", "Transcript", "Whisper transcribes, a local language model reads it. The only one that catches ads the hosts read themselves. GPU, about 2 min per hour of audio."],
];
async function detectors() {
  const st = (await api("settings")).detect, box = document.getElementById("detectors");
  box.innerHTML = DETECTORS.map(([k, name, what]) => `<label class="row" style="align-items:flex-start;margin:6px 0">
    <input type="checkbox" data-d="${k}" ${st[k] ? "checked" : ""} style="margin-top:4px"><span><b>${name}</b><br><span class="muted">${what}</span></span></label>`).join("");
  box.querySelectorAll("[data-d]").forEach(c => c.onchange = () => api("settings", {method: "PUT", body: JSON.stringify({detect: {[c.dataset.d]: c.checked}})})
    .then(() => say(`Detector ${c.dataset.d} ${c.checked ? "on" : "off"}; a pass is requested`, false, applied(now()))).catch(e => { say(e.message, true); c.checked = !c.checked; }));
}

async function add(url) {
  try { const f = await api("feeds", {method: "POST", body: JSON.stringify({url})}); say("Added " + f.title, false, s => s.st.feeds?.[f.slug] && "processing started"); refresh(); }
  catch (e) { say(e.message, true); }
}
document.getElementById("add").onclick = () => add(document.getElementById("url").value);
document.getElementById("find").onclick = async () => {
  const box = document.getElementById("results");
  box.innerHTML = '<p class="muted">Searching…</p>';
  try {
    const rs = await api("search?q=" + encodeURIComponent(document.getElementById("q").value));
    box.innerHTML = rs.length ? "" : '<p class="muted">Nothing found.</p>';
    for (const r of rs) {
      const d = document.createElement("div");
      d.className = "card result";
      d.innerHTML = `${r.artwork ? `<img src="${esc(r.artwork)}" alt="">` : ""}<div style="flex:1"><div class="name">${esc(r.title)}</div><div class="muted">${esc(r.author)}</div></div><button>Add</button>`;
      d.querySelector("button").onclick = () => add(r.url);
      box.append(d);
    }
  } catch (e) { box.innerHTML = ""; say(e.message, true); }
};
document.getElementById("q").onkeydown = e => e.key === "Enter" && document.getElementById("find").click();
// Stopped once the PC checks in after the press with nothing of unadcast left running.
const stopped = t => s => s.on && s.pc.seen > t && !s.busy && !s.gpu.length && "stopped, GPU free";
document.getElementById("stop").onclick = () => confirm("Stop processing and free the GPU? The running step is redone on the next pass.") &&
  api("stop", {method: "POST"}).then(r => { say("Stop pressed", false, stopped(r.stop)); refresh(); }).catch(e => say(e.message, true));
document.getElementById("run").onclick = () => api("run", {method: "POST"}).then(r => {
  say("Pass requested", false, s => s.st.started > r.requested ? "pass started" : s.run.stop > r.requested && "cancelled by Stop or Pause");
  refresh();
}).catch(e => say(e.message, true));
refresh();
detectors();
setInterval(refresh, 15000);
</script>
</body></html>
"""


def main():
    """For uvicorn --factory in the admin container."""
    return app(os.environ.get("ADMIN_FEEDS", "/data/feeds.json"), os.environ.get("ADMIN_STATUS", "/srv/status.json"))
