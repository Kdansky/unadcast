import numpy as np
import pytest

from unadcast import config, db
from unadcast.stages import detect_repeat as dr

from tests.conftest import EPISODES, INTRO, ads, episode


def noisy(x, rng, bits, exact=0.0):
    """Flip `bits` random bits in all but an `exact` share of items, like a re-encode at a different frame phase."""
    out = x.copy()
    hit = rng.random(len(x)) >= exact
    for _ in range(bits):
        out[hit] ^= np.uint32(1) << rng.integers(0, 32, hit.sum()).astype(np.uint32)
    return out


def test_align_finds_shared_run():
    rng = np.random.default_rng(0)
    rand = lambda n: rng.integers(0, 2**32, n, dtype=np.uint32)
    ad = rand(240)
    a = np.concatenate([rand(500), ad, rand(500)])
    b = np.concatenate([rand(900), noisy(ad, rng, 6), rand(300)])
    found = dr.spans(dr.align(a, b, 400, max_bits=10), min_items=64)
    assert len(found) == 1
    s, e = found[0]
    assert abs(s - 500) <= 8 and abs(e - 740) <= 8


def test_align_ignores_unrelated():
    rng = np.random.default_rng(1)
    a, b = (rng.integers(0, 2**32, 3000, dtype=np.uint32) for _ in "ab")
    assert dr.spans(dr.align(a, b, 0, max_bits=10), min_items=64) == []


@pytest.fixture(scope="module")
def votes(prepared):
    eps = prepared.pending("show")
    for ep in eps:
        dr.FINGERPRINT.execute(ep)
    for ep in eps:
        dr.STAGE.execute(ep)
    return {ep.title: ep.load("votes/repeat.json")["segments"] for ep in eps}


def test_fingerprint_covers_episode(prepared, votes):
    ep = episode(prepared, "ep1")
    (data,) = prepared.con.execute("SELECT data FROM prints WHERE episode = ?", (ep.id,)).fetchone()
    assert abs(len(data) // 4 * dr.ITEM - ep.load("meta.json")["duration"]) < 3


def test_repeated_ad_found_in_both_episodes(votes):
    shared = {"ep1": ads("ep1")[0], "ep2": ads("ep2")[1]}
    for title, (a, b) in shared.items():
        hits = [s for s in votes[title] if s["kind"] == "ad"]
        assert len(hits) == 1, (title, votes[title])
        assert abs(hits[0]["start"] - a) < 1.0 and abs(hits[0]["end"] - b) < 1.0, (title, hits)
        assert hits[0]["confidence"] == pytest.approx(1 - config.DEFAULTS["detect"]["repeat"]["base"])


def test_unique_ad_is_not_a_repeat(votes):
    a, b = ads("ep2")[0]
    assert not any(s["start"] < b and s["end"] > a for s in votes["ep2"])


def test_intro_is_marked_as_intro(votes):
    for title in EPISODES:
        intro = [s for s in votes[title] if s["kind"] == "intro"]
        assert len(intro) == 1, (title, votes[title])
        assert intro[0]["start"] < 1.0 and abs(intro[0]["end"] - INTRO) < 1.0
        assert intro[0]["confidence"] == pytest.approx(1 - config.DEFAULTS["detect"]["repeat"]["base"] ** 2)


def test_self_repeat():
    """An ad played twice in one episode is a repeat even with no other episode to compare against."""
    rng = np.random.default_rng(2)
    rand = lambda n: rng.integers(0, 2**32, n, dtype=np.uint32)
    ad = rand(200)
    fp = np.concatenate([rand(300), ad, rand(400), noisy(ad, rng, 5, exact=0.1), rand(300)])
    con = db.connect(":memory:")
    dr.index(con, "me", "f", fp)
    c = config.DEFAULTS["detect"]["repeat"]
    found = sorted((round(s, -1), round(e, -1)) for s, e, other, *_ in dr.find(con, "me", fp, c) if other == "me")
    assert found == [(300, 500), (900, 1100)]


def test_evidence_names_the_right_episodes(votes):
    ad = next(s for s in votes["ep1"] if s["kind"] == "ad")
    assert ad["evidence"] == "also in ep2"
    intro = next(s for s in votes["ep3"] if s["kind"] == "intro")
    assert intro["evidence"] == "also in ep1, ep2"


def test_classify_intro_after_preroll():
    """Better Offline: a network-wide 185 s pre-roll block, then the spoken intro every episode of the feed shares."""
    c = config.DEFAULTS["detect"]["repeat"]
    item = lambda t: int(t / dr.ITEM)
    sibs = set("abcd")
    # The pre-roll recurs in every sibling too, but as a mid-roll in one: an ad, and its time is discounted.
    preroll = (0, item(185), [(0, item(185), x, 0 if x != "d" else item(700), item(3000)) for x in "abcdxy"])
    intro = (item(186), item(198), [(item(186), item(198), x, item(190), item(2000)) for x in "abc"])
    later = (item(600), item(640), [(item(600), item(640), x, item(900), item(2000)) for x in "abcd"])
    assert dr.classify([preroll, intro, later], item(1800), sibs, c, "me") == ["ad", "intro", "ad"]
    # Without the pre-roll ahead of it, the same group sits too far in to be an intro: a mid-show sting at most.
    assert dr.classify([intro], item(1800), sibs, c, "me") == ["bumper"]
    # Other feeds' episodes don't make a theme.
    elsewhere = (item(186), item(198), [(item(186), item(198), x, item(190), item(2000)) for x in "xyz"])
    assert dr.classify([preroll, elsewhere], item(1800), sibs, c, "me") == ["ad", "ad"]


def test_classify_outro_before_postroll():
    c = config.DEFAULTS["detect"]["repeat"]
    item = lambda t: int(t / dr.ITEM)
    outro = (item(1600), item(1610), [(item(1600), item(1610), x, item(1900), item(2100)) for x in "abc"])
    postroll = (item(1611), item(1800), [(item(1611), item(1800), "x", item(10), item(2000))])
    assert dr.classify([outro, postroll], item(1800), set("abcd"), c, "me") == ["outro", "ad"]


def test_classify_rotating_preroll_is_an_ad():
    c = config.DEFAULTS["detect"]["repeat"]
    item = lambda t: int(t / dr.ITEM)
    # The same promo at the very start in most episodes, but mid-show in one: a campaign, not the theme.
    promo = (0, item(60), [(0, item(60), "a", 0, item(2000)), (0, item(60), "b", 0, item(2000)), (0, item(60), "c", item(1300), item(2000))])
    assert dr.classify([promo], item(1800), set("abc"), c, "me") == ["ad"]


def test_classify_short_feed_only_repeat_is_a_bumper():
    c = config.DEFAULTS["detect"]["repeat"]
    item = lambda t: int(t / dr.ITEM)
    sting = (item(900), item(915), [(item(900), item(915), "a", item(1700), item(4000))])
    network = (item(1000), item(1015), [(item(1000), item(1015), "x", item(200), item(4000))])
    promo = (item(2000), item(2040), [(item(2000), item(2040), "a", item(300), item(4000))])
    assert dr.classify([sting, network, promo], item(4000), set("abcd"), c, "me") == ["bumper", "ad", "ad"]


def test_groups_merge_overlaps_but_not_neighbours():
    a = (0, 1000, "x", 0, 5000)
    shifted = (2, 1001, "x", 1, 5000)
    touching = (995, 1100, "a", 50, 3000)
    assert [(s, e, len(ms)) for s, e, ms in dr.groups([a, shifted, touching])] == [(0, 1001, 2), (995, 1100, 1)]
