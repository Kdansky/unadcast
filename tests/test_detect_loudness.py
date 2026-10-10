from unadcast.stages import detect_loudness

from tests.conftest import EPISODES, INTRO, ads, episode


def test_flags_loud_stretches(prepared):
    # The intro is loud music too. Loudness can't tell it from an ad; the repeat detector protects it in fuse.
    for title in EPISODES:
        ep = episode(prepared, title)
        detect_loudness.STAGE.execute(ep)
        segs = ep.load("votes/loudness.json")["segments"]
        expected = [(0, INTRO)] + ads(title)
        assert len(segs) == len(expected), (title, segs)
        for seg, ad in zip(segs, expected):
            assert abs(seg["start"] - ad[0]) < 0.75 and abs(seg["end"] - ad[1]) < 0.75, (title, seg, ad)
            assert seg["kind"] == "hint"
            assert 0.5 < seg["confidence"] <= 1


def test_respects_min_length(prepared):
    ep = episode(prepared, "ep2")
    prepared.cfg["detect"]["loudness"]["min_length"] = 27
    try:
        detect_loudness.STAGE.execute(ep)
    finally:
        prepared.cfg["detect"]["loudness"]["min_length"] = 10.0
    segs = ep.load("votes/loudness.json")["segments"]
    assert len(segs) == 1 and abs(segs[0]["start"] - ads("ep2")[1][0]) < 0.75
