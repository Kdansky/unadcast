import argparse
import re
import sys
import time

import httpx

from . import config, evaluate, runner
from .core import Ctx
from .stages import STAGES, cut, fuse


def find_episode(ctx, key):
    rows = ctx.con.execute("SELECT id FROM episodes WHERE id LIKE ?", (key + "%",)).fetchall()
    if len(rows) != 1:
        sys.exit(f"{len(rows)} episodes match {key!r}")
    return ctx.episode(rows[0]["id"])


def cmd_run(ctx, a):
    runner.run(ctx, a.feed, fetch=not a.no_poll)


def cmd_loop(ctx, a):
    while True:
        runner.run(ctx, a.feed)
        runner.wait(ctx, a.interval)


def cmd_stage(ctx, a):
    if a.episode:
        ep = find_episode(ctx, a.episode)
        st = next(s for s in STAGES if s.name == a.name)
        print(f"{st.name} {ep.feed}/{ep.id} {ep.title}")
        st.execute(ep)
        return
    runner.run(ctx, a.feed, only={a.name}, force=a.force, fetch=False)


def cmd_status(ctx, a):
    failed = {(r["episode"], r["stage"]): r["error"] for r in ctx.con.execute("SELECT * FROM runs WHERE NOT ok")}
    for f in ctx.cfg["feeds"]:
        if a.feed and f["slug"] not in a.feed:
            continue
        print(f"{f['slug']} ({f['mode']}, newest {f['keep']})")
        for ep in reversed(ctx.pending(f["slug"])):
            done = [s.name for s in STAGES if s.enabled(ctx.cfg) and s.stamp(ep).exists() and not s.stale(ep)]
            plan = ep.load("plan.json") if ep.path("plan.json").exists() else None
            summary = f"{len(plan['cuts'])} cuts, {len(plan['marks'])} marks, {plan['removed']:.0f} s" if plan else ""
            print(f"  {ep.id}  {time.strftime('%Y-%m-%d', time.gmtime(ep.published))}  {ep.title[:50]:50}  {len(done)}/{len(STAGES)}  {summary}")
            for (eid, stage), err in failed.items():
                if eid == ep.id:
                    print(f"      {stage} failed: {err.splitlines()[0]}")


def cmd_report(ctx, a):
    ep = find_episode(ctx, a.episode)
    print(ep.path("report.txt").read_text() if ep.path("report.txt").exists() else "no report yet")


def cmd_spans(ctx, a):
    """Machine-readable spans for scripts on the host, such as bin/play."""
    ep = find_episode(ctx, a.episode)
    plan = ep.load("plan.json")
    for n, s in enumerate(sorted(plan["cuts"] + plan["marks"], key=lambda s: s["start"]), 1):
        print(n, s["start"], s["end"], s["action"], ep.path("original").relative_to(ctx.data), sep="\t")


def cmd_cuts(ctx, a):
    """Every cut and mark with the words around its edges, and where each cut lands in the output, for checking by ear."""
    ep = find_episode(ctx, a.episode)
    plan = ep.load("plan.json")
    words = ep.load("transcript.json")["words"] if ep.path("transcript.json").exists() else []
    say = lambda x, y: "".join(w[2] for w in words if x <= w[0] and w[1] <= y).strip()
    keep, cf = plan["keep"], ctx.cfg["publish"]["crossfade"]
    print(f"{ep.title}: {len(plan['cuts'])} cuts, {len(plan['marks'])} marks, {plan['removed']:.0f} s of {plan['duration']:.0f} s")
    for n, s in enumerate(sorted(plan["cuts"] + plan["marks"], key=lambda s: s["start"]), 1):
        a, b = s["start"], s["end"]
        where = f", join at {fuse.clock(cut.remap(a, keep, cf))} in the output" if s["action"] == "cut" else ""
        print(f"\n#{n} {s['action']} {fuse.clock(a)} - {fuse.clock(b)} ({b - a:.0f} s) p={s['p']:.2f} {s['kind']}{where}")
        print(f"  before: ...{say(a - 12, a)}\n  starts: {say(a, a + 15)}\n  ends:   ...{say(b - 15, b)}\n  after:  {say(b, b + 12)}...")


def cmd_eval(ctx, a):
    print(evaluate.report(evaluate.evaluate(ctx, set(a.only) if a.only else None)))


def cmd_find(ctx, a):
    """Look a podcast up in Apple's directory and print a [[feeds]] entry to paste into config.toml."""
    r = httpx.get("https://itunes.apple.com/search", params={"media": "podcast", "limit": 5, "term": a.term}, timeout=30)
    for p in r.json()["results"]:
        slug = re.sub(r"[^a-z0-9]+", "-", p["collectionName"].lower()).strip("-")
        print(f'# {p["collectionName"]} ({p["artistName"]})\n[[feeds]]\nslug = "{slug}"\nurl = "{p.get("feedUrl")}"\n')


def main(argv=None):
    p = argparse.ArgumentParser(prog="unadcast")
    p.add_argument("--config", help="config file (default: $UNADCAST_CONFIG or ./config.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run", help="poll, process pending episodes, republish")
    s.add_argument("--feed", action="append")
    s.add_argument("--no-poll", action="store_true", help="work on what is already downloaded")
    s = sub.add_parser("loop", help="run forever")
    s.add_argument("--feed", action="append")
    s.add_argument("--interval", type=int, default=1800)
    s = sub.add_parser("stage", help="run one stage, for one episode or every pending one")
    s.add_argument("name", choices=[st.name for st in STAGES])
    s.add_argument("--episode", help="episode id or unique prefix")
    s.add_argument("--feed", action="append")
    s.add_argument("--force", action="store_true", help="rerun even when up to date")
    s = sub.add_parser("status", help="episodes, stages done, failures")
    s.add_argument("--feed", action="append")
    s = sub.add_parser("report", help="print an episode's report")
    s.add_argument("episode")
    s = sub.add_parser("spans", help="tab-separated spans of an episode")
    s.add_argument("episode")
    s = sub.add_parser("cuts", help="cuts and marks with the words at their edges")
    s.add_argument("episode")
    s = sub.add_parser("eval", help="score plans against labels/<feed>/<episode>.json, with the current config")
    s.add_argument("--only", action="append", help="fuse only this detector's votes (repeatable)")
    s = sub.add_parser("find", help="look up a podcast's feed url")
    s.add_argument("term")

    a = p.parse_args(argv)
    ctx = Ctx.open(config.load(a.config))
    globals()[f"cmd_{a.cmd}"](ctx, a)


if __name__ == "__main__":
    main()
