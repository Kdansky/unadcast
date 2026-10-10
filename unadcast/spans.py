import numpy as np


def runs(mask, step):
    """(start, end) seconds of each run of True in a mask sampled every `step` seconds."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], np.asarray(mask, int), [0]))))
    return [(a * step, b * step) for a, b in zip(edges[::2], edges[1::2])]


def close(spans, gap):
    """Merge spans separated by less than `gap` seconds."""
    out = []
    for s, e in sorted(spans):
        if out and s - out[-1][1] < gap:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out
