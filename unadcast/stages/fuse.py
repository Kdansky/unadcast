"""Turn every detector's votes into one plan: what to cut, what to mark as a chapter, what to keep."""

import math
import wave

import numpy as np

from ..core import Stage
from ..spans import close, runs

# Kinds that mean "this is the show": any vote of these clears the span, whatever else voted.
PROTECT = ("intro", "outro")
# Keeps shorter than this between two cuts are not worth a join.
MIN_KEEP = 1.0


def plan(votes, duration, silences, loudness, cfg):
    f = cfg["fuse"]
    res = f["res"]
    n = math.ceil(duration / res)
    detectors = {d: segs for d, segs in votes.items() if cfg["detect"].get(d, {}).get("enabled")}
    score = {d: np.zeros(n) for d in detectors}
    kind = {d: np.full(n, "", dtype=object) for d in detectors}
    veto = np.zeros(n, bool)
    for d, segs in detectors.items():
        for s in segs:
            i, j = int(s["start"] / res), min(n, math.ceil(s["end"] / res))
            if s["kind"] in PROTECT:
                veto[i:j] = True
                continue
            v = cfg["detect"][d]["weight"] * f["kinds"].get(s["kind"], 0) * s["confidence"]
            better = v > score[d][i:j]
            score[d][i:j][better] = v
            kind[d][i:j][better] = s["kind"]
    # Credits read over the outro music are boilerplate all the same: the show's own recurring words beat its theme.
    if "boilerplate" in score:
        veto &= score["boilerplate"] == 0
    p = 1 - np.prod([1 - v for v in score.values()], axis=0) if score else np.zeros(n)
    p[veto] = 0

    def describe(a, b, action):
        i, j = int(a / res), math.ceil(b / res)
        scores = {d: round(float(v[i:j].max()), 3) for d, v in score.items() if v[i:j].max() > 0}
        top = max(scores, key=scores.get)
        return {
            "start": snap(a, -1, silences, loudness, f["snap_window"]),
            "end": min(duration, snap(b, 1, silences, loudness, f["snap_window"])),
            "p": round(float(p[i:j].max()), 3),
            "action": action,
            "kind": kind[top][i:j][score[top][i:j].argmax()],
            "scores": scores,
            "evidence": [f"{d}: {s['evidence']}" for d, segs in detectors.items() for s in segs if s["start"] < b and s["end"] > a],
        }

    # Cut only where p itself is high. A weak fringe next to a strong core, like a loudness hint running on into a loud
    # intro, is marked rather than dragged along with the cut.
    exact = [d for d in score if d in f["exact"]]
    loose = 1 - np.prod([1 - v for d, v in score.items() if d not in exact], axis=0) if len(exact) < len(score) else np.zeros(n)
    loose[veto] = 0
    cut = np.zeros(n, bool)
    for a, b in close(runs(loose >= f["cut_threshold"], res), f["merge_gap"]):
        i, j = int(a / res), math.ceil(b / res)
        voters = [d for d, v in score.items() if d not in exact and v[i:j].max() > 0]
        corroborated = len(voters) >= 2 or any(d in f["strong"] for d in voters)
        if f["min_length"] <= b - a <= f["max_length"] and corroborated:
            cut[i:j] = True
    for d in exact:
        cut |= (score[d] >= f["cut_threshold"]) & ~veto
    # An ad break is contiguous: a span from an `extend` detector that touches a cut belongs to the same break.
    ext = np.zeros(n, bool)
    for d in f["extend"]:
        if d in score:
            ext |= score[d] > 0
    reach = int(f["merge_gap"] / res)
    for a, b in close(runs(ext & ~veto, res), f["merge_gap"]):
        i, j = int(a / res), math.ceil(b / res)
        if cut[max(0, i - reach) : j + reach].any():
            cut[i:j] = True
    spans = [describe(a, b, "cut") for a, b in runs(cut, res)]
    spans += [describe(a, b, "mark") for a, b in close(runs((p >= f["mark_threshold"]) & ~cut, res), f["merge_gap"]) if b - a >= f["min_length"]]

    cuts = [s for s in spans if s["action"] == "cut"]
    keep, t = [], 0.0
    for c in cuts:
        if c["start"] - t >= MIN_KEEP:
            keep.append([t, c["start"]])
        t = max(t, c["end"])
    if duration - t >= MIN_KEEP:
        keep.append([t, duration])
    return {
        "duration": duration,
        "keep": keep,
        "cuts": cuts,
        "marks": [s for s in spans if s["action"] == "mark"],
        "removed": round(duration - sum(b - a for a, b in keep), 2),
    }


def snap(t, outward, silences, loudness, window):
    """Move an edge to the nearest pause, preferring to cut a little ad too little over a little show too much."""
    cost = lambda x: abs(x - t) * (1 if (x - t) * outward >= 0 else 2)
    mids = [(a + b) / 2 for a, b in silences if abs((a + b) / 2 - t) <= window]
    if mids:
        return round(min(mids, key=cost), 2)
    # No silence nearby: settle for a clearly quiet moment, if there is one.
    step, m = loudness["step"], np.array(loudness["m"])
    i, j = max(0, int((t - window) / step)), min(len(m), int((t + window) / step) + 1)
    if j > i and m[i:j].min() < np.median(m[i:j]) - 10:
        return round((i + int(m[i:j].argmin())) * step, 2)
    return t


def replan(ep, cfg):
    """The plan an episode gets under `cfg`, from the votes already on disk."""
    votes = {p.stem: ep.load(f"votes/{p.name}")["segments"] for p in sorted(ep.path("votes").glob("*.json"))}
    with wave.open(str(ep.path("audio.wav"))) as w:
        duration = w.getnframes() / w.getframerate()
    return plan(votes, round(duration, 2), ep.load("silences.json"), ep.load("loudness.json"), cfg)


def run(ep):
    out = replan(ep, ep.ctx.cfg)
    ep.save("plan.json", out)
    ep.path("report.txt").write_text(report(ep.title, out))


def clock(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:04.1f}"


def report(title, plan):
    lines = [f"{title}: {len(plan['cuts'])} cuts ({plan['removed']:.0f} s), {len(plan['marks'])} marks", ""]
    for n, s in enumerate(sorted(plan["cuts"] + plan["marks"], key=lambda s: s["start"]), 1):
        lines.append(f"#{n} {s['action']:4} {clock(s['start'])} - {clock(s['end'])} ({s['end'] - s['start']:.0f} s)  p={s['p']:.2f}  {s['kind']}")
        lines += [f"    {d} {v:.2f}" for d, v in s["scores"].items()]
        lines += [f"    {e}" for e in s["evidence"]]
        lines.append("")
    return "\n".join(lines)


STAGE = Stage(
    "fuse", run, inputs=("audio.wav", "silences.json", "loudness.json"), optional=("votes/*.json",),
    outputs=("plan.json", "report.txt"), config=lambda cfg, ep: {"fuse": cfg["fuse"], "detect": cfg["detect"]},
)
