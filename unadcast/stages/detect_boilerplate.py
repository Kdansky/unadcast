"""Words the show says in every episode: credits, "email us at", "subscribe", the hotline, the greeting.

Read live, they never sound the same twice, so `detect_repeat` can't find them, but the words barely change. Every
run of `n` words is hashed into an index by feed; a stretch of an episode whose runs recur in other episodes of the
same show is boilerplate. Network promos recur this way too, and are found again here.
"""

import re
import zlib

from ..core import Stage


def tokens(words):
    """The words worth comparing and their normalised text: case and punctuation vary from one transcription to the next."""
    out = [(w, re.sub(r"[^\w']", "", w[2].lower())) for w in words]
    return [w for w, t in out if t], [t for _, t in out if t]


def grams(toks, n):
    return [zlib.crc32(" ".join(toks[i : i + n]).encode()) for i in range(len(toks) - n + 1)]


def index(ep):
    c = ep.ctx.cfg["detect"]["boilerplate"]
    con = ep.ctx.con
    _, toks = tokens(ep.load("transcript.json")["words"])
    con.execute("DELETE FROM grams WHERE episode = ?", (ep.id,))
    con.executemany("INSERT INTO grams VALUES (?, ?, ?)", ((h, ep.feed, ep.id) for h in set(grams(toks, c["n"]))))
    con.commit()


def run(ep):
    c = ep.ctx.cfg["detect"]["boilerplate"]
    con = ep.ctx.con
    words, toks = tokens(ep.load("transcript.json")["words"])
    gs = grams(toks, c["n"])
    con.execute("CREATE TEMP TABLE IF NOT EXISTS gq (pos INTEGER, hash INTEGER)")
    con.execute("DELETE FROM gq")
    con.executemany("INSERT INTO gq VALUES (?, ?)", enumerate(gs))
    # CROSS JOIN keeps gq outer. Left to itself, SQLite walks the feed's whole index and scans gq for every row.
    seen = dict(con.execute(
        "SELECT q.pos, COUNT(DISTINCT g.episode) FROM gq q CROSS JOIN grams g ON g.hash = q.hash AND g.feed = ? AND g.episode != ? "
        "GROUP BY q.pos", (ep.feed, ep.id),
    ).fetchall())
    # Per word, the most episodes any run covering it recurs in.
    per = [0] * len(words)
    for i, k in seen.items():
        for j in range(i, i + c["n"]):
            per[j] = max(per[j], k)

    # Whole sentences or nothing: a show's formulas sit inside its own sentences ("today on the show, we're going to talk
    # about…"), and cutting the formula leaves the rest dangling. A sentence ends at . ? ! or a pause; one whose every
    # word recurs is boilerplate, a mixed one stays.
    sentences, start = [], 0
    for k, w in enumerate(words):
        if k + 1 == len(words) or w[2].rstrip().endswith((".", "?", "!")) or words[k + 1][0] - w[1] > c["pause"]:
            sentences.append((start, k + 1))
            start = k + 1
    said = [min(per[a:b]) >= c["min_episodes"] for a, b in sentences]
    segments, k = [], 0
    while k < len(sentences):
        if not said[k]:
            k += 1
            continue
        m = k
        while m < len(sentences) and said[m]:
            m += 1
        i, j = sentences[k][0], sentences[m - 1][1]
        if j - i >= c["min_words"]:
            text = "".join(w[2] for w in words[i:j]).strip()
            segments.append({
                "start": words[i][0], "end": words[j - 1][1], "confidence": round(1 - c["base"] ** min(per[i:j]), 3),
                "kind": "boilerplate", "evidence": f"said in {min(per[i:j])} other episodes: \"{text[:80]}\"",
            })
        k = m
    ep.save("votes/boilerplate.json", {"duration": words[-1][1] if words else 0, "segments": segments})


enabled = lambda cfg: cfg["detect"]["boilerplate"]["enabled"] and cfg["transcribe"]["enabled"]
INDEX = Stage("index_words", index, inputs=("transcript.json",), config=lambda cfg, ep: cfg["detect"]["boilerplate"]["n"], enabled=enabled)
# The index stamp stands in for the rows, which live in the database rather than a file.
STAGE = Stage(
    "detect_boilerplate", run, inputs=("stamps/index_words.json", "transcript.json"), outputs=("votes/boilerplate.json",),
    config=lambda cfg, ep: cfg["detect"]["boilerplate"], enabled=enabled,
)
