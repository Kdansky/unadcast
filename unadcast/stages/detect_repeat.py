"""Audio that recurs across episodes (or twice within one) is an ad, an intro or an outro.

Chromaprint turns audio into one 32-bit item per ~0.124 s. Every episode's items go into an index; for a new episode,
exact item hits against the index suggest alignments (other episode, offset), and each alignment is then verified by bit
errors item by item, which tolerates the re-encoding and frame-phase differences that make most items differ slightly.
"""

import json
import subprocess

import numpy as np

from ..core import Stage
from ..spans import runs

ITEM = 4096 / 3 / 11025
# An item summarises ~16 frames, so its content is centred about 8 items after its own position.
LEAD = 8 * ITEM
SMOOTH = 9
# Items this frequent within one episode are silence or a held tone, and would align everything with everything.
COMMON = 10


def fpcalc(path):
    out = subprocess.run(["fpcalc", "-raw", "-json", "-length", "0", path], capture_output=True, text=True, check=True)
    return np.array(json.loads(out.stdout)["fingerprint"], dtype=np.uint32)


def index(con, episode, feed, fp):
    con.execute("DELETE FROM hashes WHERE episode = ?", (episode,))
    con.executemany("INSERT INTO hashes VALUES (?, ?, ?)", ((int(h), episode, i) for i, h in enumerate(fp)))
    con.execute("INSERT OR REPLACE INTO prints VALUES (?, ?, ?)", (episode, feed, fp.tobytes()))
    con.commit()


def load(con, episode):
    (data,) = con.execute("SELECT data FROM prints WHERE episode = ?", (episode,)).fetchone()
    return np.frombuffer(data, dtype=np.uint32)


def align(a, b, delta, max_bits):
    """Mask over `a`: True where a[i] matches b[i + delta], judged on bit errors averaged over a few items."""
    mask = np.zeros(len(a), bool)
    i = np.arange(max(0, -delta), min(len(a), len(b) - delta))
    if len(i) >= SMOOTH:
        err = np.bitwise_count(a[i] ^ b[i + delta]).astype(float)
        mask[i] = np.convolve(err, np.ones(SMOOTH) / SMOOTH, "same") < max_bits
    return mask


def spans(mask, min_items):
    return [(int(s), int(e)) for s, e in runs(mask, 1) if e - s >= min_items]


def find(con, me, fp, c):
    """Runs of `fp` that recur in indexed episodes: (start, end, episode, other_start, other_length), in items."""
    min_items = int(c["min_run"] / ITEM)
    values, counts = np.unique(fp, return_counts=True)
    common = set(values[counts > COMMON].tolist())
    con.execute("CREATE TEMP TABLE IF NOT EXISTS q (hash INTEGER, pos INTEGER)")
    con.execute("DELETE FROM q")
    con.executemany("INSERT INTO q VALUES (?, ?)", ((int(h), i) for i, h in enumerate(fp) if int(h) not in common))
    rows = con.execute(
        "SELECT h.episode, h.pos - q.pos AS delta, COUNT(*) AS n FROM q JOIN hashes h ON h.hash = q.hash "
        "GROUP BY h.episode, delta HAVING n >= ?",
        (c["min_hits"],),
    ).fetchall()

    found, prints = [], {}
    for other, delta, _ in rows:
        if other == me and abs(delta) < min_items:
            continue
        if other not in prints:
            prints[other] = load(con, other)
        b = prints[other]
        found += [(s, e, other, s + delta, len(b)) for s, e in spans(align(fp, b, delta, c["max_bits"]), min_items)]
    return found


def groups(found):
    """Union overlapping matches; neighbouring offsets of one true alignment all match the same stretch. Matches that
    merely touch stay apart: an intro right after a pre-roll must be judged on its own."""
    out = []
    for m in sorted(found):
        if out and m[0] < out[-1][1] - 2 * SMOOTH:
            out[-1][1] = max(out[-1][1], m[1])
            out[-1][2].append(m)
        else:
            out.append([m[0], m[1], [m]])
    return out


def fingerprint(ep):
    index(ep.ctx.con, ep.id, ep.feed, fpcalc(ep.path("audio.wav")))


def classify(gs, length, siblings, c, me):
    """The kind of each group from `groups`: intro, outro, bumper or ad. `siblings` are the other episodes of the same feed."""
    edge = c["edge"] / ITEM
    # Position alone can't tell a theme from a pre- or post-roll ad. A theme recurs in nearly every episode of its feed;
    # ads rotate, and network ads turn up in other feeds too, which doesn't count towards the share.
    share = [len({m[2] for m in ms} & siblings) for _, _, ms in gs]
    theme = [n >= 2 and n >= c["theme_share"] * len(siblings) for n in share]
    kinds = ["ad"] * len(gs)
    # A pre-roll pushes the intro off the start, in this episode and presumably its siblings, so ad time is discounted.
    # Groups resolve in order from the nearer edge, so every group ahead of one is already known to be ad or not.
    before = 0
    for k, (s, e, ms) in enumerate(gs):
        if theme[k] and all(m[0] - before < edge and m[3] < edge + before for m in ms):
            kinds[k] = "intro"
        else:
            before += e - s
    after = 0
    for k in reversed(range(len(gs))):
        s, e, ms = gs[k]
        if kinds[k] == "intro":
            continue
        if theme[k] and all(length - e - after < edge and m[4] - m[3] - (m[1] - m[0]) < edge + after for m in ms):
            kinds[k] = "outro"
        else:
            after += e - s
    # Shows reuse short stings between segments. Ad spots run 15 s and up, and network ads recur in other feeds too.
    for k, (s, e, ms) in enumerate(gs):
        if kinds[k] == "ad" and (e - s) * ITEM < c["bumper"] and {m[2] for m in ms} <= siblings | {me}:
            kinds[k] = "bumper"
    return kinds


def run(ep):
    c = ep.ctx.cfg["detect"]["repeat"]
    con = ep.ctx.con
    fp = load(con, ep.id)
    duration = ep.load("meta.json")["duration"]
    siblings = {r[0] for r in con.execute("SELECT episode FROM prints WHERE feed = ? AND episode != ?", (ep.feed, ep.id))}
    gs = groups(find(con, ep.id, fp, c))
    segments = []
    for (s, e, ms), kind in zip(gs, classify(gs, len(fp), siblings, c, ep.id)):
        others = sorted({m[2] for m in ms})
        titles = dict(con.execute(f"SELECT id, title FROM episodes WHERE id IN ({','.join('?' * len(others))})", others).fetchall())
        segments.append({
            "start": 0.0 if s == 0 else round(s * ITEM + LEAD, 2),
            "end": round(duration, 2) if e >= len(fp) else round(e * ITEM + LEAD, 2),
            "confidence": round(1 - c["base"] ** len(others), 3),
            "kind": kind,
            "evidence": "also in " + ", ".join(sorted("itself" if o == ep.id else titles.get(o, o) for o in others)),
        })
    ep.save("votes/repeat.json", {"duration": duration, "segments": segments})


enabled = lambda cfg: cfg["detect"]["repeat"]["enabled"]
FINGERPRINT = Stage("fingerprint", fingerprint, inputs=("audio.wav",), enabled=enabled)
# The fingerprint stamp stands in for the index rows, which live in the database rather than a file.
STAGE = Stage(
    "detect_repeat", run, inputs=("stamps/fingerprint.json", "meta.json"), outputs=("votes/repeat.json",),
    config=lambda cfg, ep: cfg["detect"]["repeat"], enabled=enabled,
)
