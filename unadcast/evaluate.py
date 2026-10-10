"""Compare plans with hand-made labels, second by second. Re-fuses in memory, so tuning [fuse] needs no rerun."""

import copy
import json
import math
from pathlib import Path

import numpy as np

from .stages import fuse

STEP = 0.1


def score(plan, ads):
    """Seconds of ad and show that ended up cut, marked or untouched. A label ending in None runs to the end."""
    n = math.ceil(plan["duration"] / STEP)

    def mask(spans):
        m = np.zeros(n, bool)
        for a, b in spans:
            m[int(a / STEP) : n if b is None else math.ceil(b / STEP)] = True
        return m

    ad = mask(ads)
    cut = mask((s["start"], s["end"]) for s in plan["cuts"])
    mark = mask((s["start"], s["end"]) for s in plan["marks"]) & ~cut
    sec = lambda m: round(float(m.sum()) * STEP, 1)
    return {
        "ad": sec(ad),
        "cut_ad": sec(cut & ad),
        "mark_ad": sec(mark & ad),
        "missed": sec(ad & ~cut & ~mark),
        "cut_show": sec(cut & ~ad),
        "mark_show": sec(mark & ~ad),
    }


def evaluate(ctx, only=None):
    """Score every labelled episode that is still in a work dir. `only` restricts fusion to those detectors."""
    cfg = copy.deepcopy(ctx.cfg)
    if only:
        for name, c in cfg["detect"].items():
            c["enabled"] = c["enabled"] and name in only
    rows = []
    for path in sorted(Path(cfg["paths"]["labels"]).glob("*/*.json")):
        try:
            ep = ctx.episode(path.stem)
        except KeyError:
            continue
        if not ep.path("audio.wav").exists():
            continue
        label = json.loads(path.read_text())
        rows.append({"feed": ep.feed, "published": ep.published, "title": ep.title, "score": score(fuse.replan(ep, cfg), label["ads"])})
    return sorted(rows, key=lambda r: (r["feed"], r["published"]))


def report(rows):
    pct = lambda x, of: f"{100 * x / of:.0f}%" if of else "-"
    lines = [f"{'episode':52} {'ad s':>6} {'cut':>5} {'mark':>5} {'miss':>5} {'show cut s':>11} {'show mark s':>12}"]

    def line(name, s):
        lines.append(
            f"{name[:52]:52} {s['ad']:6.0f} {pct(s['cut_ad'], s['ad']):>5} {pct(s['mark_ad'], s['ad']):>5} "
            f"{pct(s['missed'], s['ad']):>5} {s['cut_show']:11.0f} {s['mark_show']:12.0f}"
        )

    for r in rows:
        line(f"{r['feed']}: {r['title']}", r["score"])
    if rows:
        line("total", {k: sum(r["score"][k] for r in rows) for k in rows[0]["score"]})
    return "\n".join(lines)
