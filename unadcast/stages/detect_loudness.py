import numpy as np
from scipy.ndimage import median_filter

from ..core import Stage
from ..spans import close, runs


def run(ep):
    """Inserted ads are mastered apart from the show; flag sustained stretches off the show's usual level."""
    c = ep.ctx.cfg["detect"]["loudness"]
    L = ep.load("loudness.json")
    step, s, m = L["step"], np.array(L["s"]), np.array(L["m"])
    # Pauses and silence would drag the median down, so judge the level on audible frames only.
    active = s > np.percentile(s, 75) - 15
    level = np.median(s[active])
    dev = median_filter(s, size=int(5 / step), mode="nearest") - level
    dev[~active] = 0

    segments = []
    for a, b in close(runs(np.abs(dev) > c["threshold_lu"], step), 2.0):
        if not c["min_length"] <= b - a <= c["max_length"]:
            continue
        d = dev[int(a / step) : int(b / step)]
        a, b = refine(m - level, np.sign(d.mean()) * c["threshold_lu"], a, b, step)
        segments.append({
            "start": round(a, 2),
            "end": round(b, 2),
            "confidence": round(min(1.0, np.abs(d).mean() / (2 * c["threshold_lu"])), 3),
            "kind": "hint",
            "evidence": f"{d.mean():+.1f} LU against the show's {level:.1f} LUFS",
        })
    ep.save("votes/loudness.json", {"duration": len(s) * step, "segments": segments})


def refine(dev, thr, a, b, step, reach=3.0):
    """Short-term loudness smears edges by seconds; momentary loudness puts them where the level actually changes."""
    m = median_filter(dev, size=int(1 / step), mode="nearest") * np.sign(thr)
    lo, hi = int(max(0, a - reach) / step), int(min(len(m) * step, b + reach) / step)
    hit = np.flatnonzero(m[lo:hi] > abs(thr))
    if not len(hit):
        return a, b
    first, last = (lo + hit[0]) * step, (lo + hit[-1] + 1) * step
    return (first if abs(first - a) <= reach else a), (last if abs(last - b) <= reach else b)


STAGE = Stage(
    "detect_loudness", run, inputs=("loudness.json",), outputs=("votes/loudness.json",),
    config=lambda cfg, ep: cfg["detect"]["loudness"], enabled=lambda cfg: cfg["detect"]["loudness"]["enabled"],
)
