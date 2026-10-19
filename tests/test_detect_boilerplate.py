import random

from unadcast.core import Episode
from unadcast.stages import detect_boilerplate as db_

CREDITS = "Thank you for listening. The show is produced by Jane Doe. You can email us at show at example dot com."


def say(text, t):
    out = []
    for w in text.split():
        out.append([round(t, 2), round(t + 0.3, 2), " " + w])
        t += 0.4
    return out, t


def talk(seed, n=300):
    rng = random.Random(seed)
    vocab = [f"word{k}" for k in range(2000)]
    return " ".join(rng.choice(vocab) for _ in range(n))


def make(ctx, id, feed, text):
    ctx.con.execute("INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?)", (id, feed, id, id, 0.0, "", 0))
    ep = Episode(id, feed, id, id, 0.0, "", 0, ctx=ctx)
    words, _ = say(text, 0.0)
    ep.save("transcript.json", {"language": "en", "words": words})
    return ep


def test_finds_what_every_episode_says(make_ctx):
    ctx = make_ctx(feeds=[{"slug": "show", "url": "x"}, {"slug": "other", "url": "y"}])
    # Case and punctuation differ between transcriptions; "at the end of the day" is common but short. The greeting's
    # formula recurs, but its sentence doesn't: it stays.
    greet = lambda k: f"Hello and welcome to the show, the flagship podcast of {talk(k + 70, 5)}."
    eps = [make(ctx, f"e{k}", "show", f"{greet(k)} {talk(k)} at the end of the day {talk(k + 50)}. {CREDITS}") for k in range(4)]
    # The same words in another show don't count for this one.
    stranger = make(ctx, "s0", "other", f"{talk(99)} {CREDITS}")
    for ep in eps + [stranger]:
        db_.INDEX.execute(ep)
    for ep in eps + [stranger]:
        db_.STAGE.execute(ep)

    segs = eps[0].load("votes/boilerplate.json")["segments"]
    assert len(segs) == 1 and segs[0]["kind"] == "boilerplate"
    # Credits start after the 15-word greeting and 300 + 6 + 300 words of talk.
    assert segs[0]["start"] == round(621 * 0.4, 2) and "said in 3 other episodes" in segs[0]["evidence"]
    assert segs[0]["confidence"] > 0.99
    assert stranger.load("votes/boilerplate.json")["segments"] == []


def test_reindexing_replaces_rows(make_ctx):
    ctx = make_ctx(feeds=[{"slug": "show", "url": "x"}])
    ep = make(ctx, "e0", "show", talk(0, 50))
    db_.INDEX.execute(ep)
    db_.INDEX.execute(ep)
    (n,) = ctx.con.execute("SELECT COUNT(*) FROM grams WHERE episode = 'e0'").fetchone()
    assert n == 45
