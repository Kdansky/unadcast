import fcntl
import json
import shutil
import subprocess
import time
import traceback

import httpx
from lxml import etree

from . import config, db
from .core import write
from .stages import STAGES, poll, publish


def why(e):
    if isinstance(e, subprocess.CalledProcessError) and e.stderr:
        tail = e.stderr if isinstance(e.stderr, str) else e.stderr.decode(errors="replace")
        return f"{e}\n{tail.strip()[-2000:]}"
    return "".join(traceback.format_exception_only(e)).strip()


def run(ctx, slugs=None, only=None, force=False, fetch=True, log=print):
    """One pass: poll, then each stage across every pending episode, then republish."""
    # The worker's loop and a manual run must not process the same episodes at once.
    with open(ctx.data / "run.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        locked(ctx, slugs, only, force, fetch, log)


def locked(ctx, slugs, only, force, fetch, log):
    # Before reading the shows and settings: a change made earlier than this is in this pass.
    started = time.time()
    sync_feeds(ctx, log)
    status(ctx, started, running=True)
    feeds = [f for f in ctx.cfg["feeds"] if not slugs or f["slug"] in slugs]
    for f in feeds if fetch else []:
        try:
            poll.poll(ctx, f)
        except Exception as e:
            log(f"poll {f['slug']} failed: {why(e)}")

    eps = [e for f in feeds for e in ctx.pending(f["slug"])]
    for st in STAGES:
        if not st.enabled(ctx.cfg) or (only and st.name not in only):
            continue
        todo = [ep for ep in eps if st.ready(ep) and (force or st.stale(ep))]
        for k, ep in enumerate(todo, 1):
            log(f"{st.name} {ep.feed}/{ep.id} {ep.title}")
            status(ctx, started, running=True, current={"stage": st.name, "feed": ep.feed, "title": ep.title, "step": k, "of": len(todo), "since": time.time()})
            try:
                st.execute(ep)
                db.record(ctx.con, ep.id, st.name)
            except Exception as e:
                db.record(ctx.con, ep.id, st.name, why(e))
                log(f"{st.name} {ep.feed}/{ep.id} failed: {why(e)}")
        if todo:
            try:
                st.after(ctx.cfg)
            except Exception as e:
                log(f"{st.name} cleanup failed: {why(e)}")

    for f in feeds:
        prune(ctx, f["slug"], {e.id for e in eps})
        if (ctx.data / "work" / f["slug"] / "feed.xml").exists():
            publish.publish(ctx, f)
    status(ctx, started, running=False)


def remote(ctx, path, log, empty):
    """GET from the admin API, cached in data/: if it is unreachable, the last answer stands."""
    url = ctx.cfg["feeds_url"].rsplit("/feeds", 1)[0] + path
    cache = ctx.data / f"remote{path.replace('/', '-')}.json"
    try:
        r = httpx.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        write(cache, json.dumps(data).encode())
        return data
    except Exception as e:
        log(f"{url} unavailable, using the last answer: {why(e)}")
        return json.loads(cache.read_text()) if cache.exists() else empty


def sync_feeds(ctx, log):
    """Shows from config.toml plus those on the admin page, and the detector switches set there."""
    local = ctx.cfg["feeds_local"]
    if not ctx.cfg["feeds_url"]:
        ctx.cfg["feeds"] = local
        return
    shows = remote(ctx, "/feeds", log, [])
    ctx.cfg["feeds"] = local + [config.feed(f, ctx.cfg) for f in shows if f["slug"] not in {g["slug"] for g in local}]
    for name, on in remote(ctx, "/settings", log, {}).get("detect", {}).items():
        if name in ctx.cfg["detect"]:
            ctx.cfg["detect"][name]["enabled"] = bool(on)
            # Nothing but the LLM reads the transcript: no LLM, no Whisper.
            if name == "llm":
                ctx.cfg["transcribe"]["enabled"] = bool(on)


def status(ctx, started, running, current=None):
    """status.json next to the feeds, for the admin page: per show, its episodes and what happened to them, and while
    a pass runs, the step it is on."""
    failed = {r["episode"]: r["error"].splitlines()[0] for r in ctx.con.execute("SELECT * FROM runs WHERE NOT ok")}
    base = ctx.cfg["serve"]["base_url"].rstrip("/")
    feeds = {}
    for f in ctx.cfg["feeds"]:
        xml = ctx.data / "work" / f["slug"] / "feed.xml"
        title = (poll.parse(xml.read_bytes()).findtext("channel/title") if xml.exists() else None) or f["slug"]
        eps = []
        for ep in ctx.pending(f["slug"]):
            done = ep.path("out.json").exists()
            plan = ep.load("plan.json") if done and ep.path("plan.json").exists() else {"cuts": [], "marks": []}
            out = ep.load("out.json") if done else {}
            eps.append({
                "id": ep.id, "title": ep.title, "published": ep.published,
                "state": "done" if done else "failed" if ep.id in failed else "waiting",
                "error": failed.get(ep.id) if not done else None,
                "cuts": len(plan["cuts"]), "marks": len(plan["marks"]), "removed": out.get("removed", 0),
            })
        feeds[f["slug"]] = {"title": title.strip(), "url": f"{base}/{f['slug']}.xml", "mode": f["mode"], "keep": f["keep"], "episodes": eps}
    out = {"started": started, "updated": time.time(), "running": running, "feeds": feeds}
    if current:
        out["current"] = current
    write(ctx.public / "status.json", json.dumps(out).encode())


def wait(ctx, interval, log=print, poll_every=30):
    """Sleep until the next pass is due, or until someone presses "process now" on the admin page."""
    url = ctx.cfg["feeds_url"].rsplit("/feeds", 1)[0] + "/run" if ctx.cfg["feeds_url"] else None
    since, deadline = time.time(), time.time() + interval
    while time.time() < deadline:
        time.sleep(min(poll_every, max(0, deadline - time.time())))
        if not url:
            continue
        try:
            asked = httpx.get(url, timeout=10).json().get("requested") or 0
        except Exception:
            continue
        if asked > since:
            log("pass requested from the admin page")
            return


def prune(ctx, slug, pending):
    """Delete work dirs of episodes that left the window. Fingerprints stay in the database for matching."""
    for d in (ctx.data / "work" / slug).glob("*/"):
        if d.name not in pending:
            shutil.rmtree(d)
