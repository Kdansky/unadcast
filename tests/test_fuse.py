import copy

import pytest

from unadcast import config
from unadcast.stages import fuse


def seg(start, end, conf=1.0, kind="ad"):
    return {"start": start, "end": end, "confidence": conf, "kind": kind, "evidence": f"{kind} {start}-{end}"}


@pytest.fixture
def cfg():
    return copy.deepcopy(config.DEFAULTS)


def plan(cfg, votes, duration=600.0, silences=(), m=None):
    return fuse.plan(votes, duration, list(silences), m or {"step": 0.1, "m": [-20.0] * int(duration * 10)}, cfg)


def test_strong_vote_cuts(cfg):
    p = plan(cfg, {"repeat": [seg(100, 130)], "loudness": [seg(100, 130, kind="hint")]})
    assert [(c["start"], c["end"]) for c in p["cuts"]] == [(100, 130)]
    assert p["cuts"][0]["kind"] == "ad"
    assert set(p["cuts"][0]["scores"]) == {"repeat", "loudness"}
    assert p["keep"] == [[0, 100], [130, 600]]
    assert p["removed"] == 30
    assert p["marks"] == []


def test_lone_hint_only_marks(cfg):
    p = plan(cfg, {"loudness": [seg(100, 130, kind="hint")]})
    assert p["cuts"] == []
    assert [(m["start"], m["end"]) for m in p["marks"]] == [(100, 130)]
    assert p["marks"][0]["p"] == pytest.approx(0.64)
    assert p["keep"] == [[0, 600]]


def test_two_weak_detectors_corroborate(cfg):
    cfg["detect"]["music"] = {"enabled": True, "weight": 0.9}
    p = plan(cfg, {"loudness": [seg(100, 130, kind="hint")], "music": [seg(101, 129, kind="hint")]})
    assert len(p["cuts"]) == 1


def test_protected_kinds_veto(cfg):
    p = plan(cfg, {"repeat": [seg(0, 12, kind="intro")], "loudness": [seg(0, 12.5, kind="hint")]})
    assert p["cuts"] == [] and p["marks"] == []


def test_close_spans_merge_short_spans_drop(cfg):
    p = plan(cfg, {"repeat": [seg(100, 130), seg(132, 150), seg(300, 305)]})
    assert [(c["start"], c["end"]) for c in p["cuts"]] == [(100, 150)]


def test_overlong_span_only_marks(cfg):
    p = plan(cfg, {"repeat": [seg(100, 800)]}, duration=1200)
    assert p["cuts"] == [] and len(p["marks"]) == 1


def test_disabled_detector_is_ignored(cfg):
    cfg["detect"]["repeat"]["enabled"] = False
    assert plan(cfg, {"repeat": [seg(100, 130)]})["cuts"] == []


def test_edges_snap_to_silence(cfg):
    # Edges snap to silence midpoints. Inner distances count double: leaving a second of ad beats cutting a word.
    silences = [[98.0, 98.6], [100.9, 101.1], [129.0, 129.2], [131.0, 131.4]]
    p = plan(cfg, {"repeat": [seg(100, 130)]}, silences=silences)
    assert (p["cuts"][0]["start"], p["cuts"][0]["end"]) == (98.3, 131.2)

    silences = [[98.0, 98.6], [100.2, 100.4], [129.0, 129.2], [131.0, 131.4]]
    p = plan(cfg, {"repeat": [seg(100, 130)]}, silences=silences)
    assert (p["cuts"][0]["start"], p["cuts"][0]["end"]) == (100.3, 131.2)

    p = plan(cfg, {"repeat": [seg(100, 130)]}, silences=[[90, 91], [140, 141]])
    assert (p["cuts"][0]["start"], p["cuts"][0]["end"]) == (100, 130)


def test_edges_fall_back_to_quietest_moment(cfg):
    m = [-20.0] * 6000
    m[1012] = -60.0
    m[1290] = -55.0
    p = plan(cfg, {"repeat": [seg(100, 130)]}, m={"step": 0.1, "m": m})
    assert (p["cuts"][0]["start"], p["cuts"][0]["end"]) == (101.2, 129.0)


def test_tiny_keeps_are_absorbed(cfg):
    cfg["fuse"]["merge_gap"] = 0
    p = plan(cfg, {"repeat": [seg(50, 100), seg(100.3, 150)]}, duration=300)
    assert p["keep"] == [[0, 50], [150, 300]]


def test_weak_fringe_of_a_cut_is_only_marked(cfg):
    """A loudness hint running on past a repeated ad into a loud intro must not drag the cut along with it."""
    p = plan(cfg, {"repeat": [seg(0, 180)], "loudness": [seg(0, 202, kind="hint")]})
    assert [(c["start"], c["end"]) for c in p["cuts"]] == [(0, 180)]
    assert [(m["start"], m["end"]) for m in p["marks"]] == [(180, 202)]
    assert p["keep"] == [[180, 600]]


def test_foreign_voice_next_to_a_cut_joins_it(cfg):
    """Vergecast: a recurring promo opens the break, unique paid spots follow in voices the show never uses again."""
    votes = {"repeat": [seg(100, 130)], "voice": [seg(128, 200, conf=0.95, kind="hint"), seg(400, 430, conf=0.95, kind="hint")]}
    p = plan(cfg, votes)
    assert [(c["start"], c["end"]) for c in p["cuts"]] == [(100, 200)]
    # On its own, an odd voice is a clip or a guest's sound bite as often as an ad: only marked.
    assert [(m["start"], m["end"]) for m in p["marks"]] == [(400, 430)]


def test_only_extending_detectors_extend(cfg):
    cfg["fuse"]["extend"] = []
    p = plan(cfg, {"repeat": [seg(100, 130)], "voice": [seg(128, 200, conf=0.95, kind="hint")]})
    assert [(c["start"], c["end"]) for c in p["cuts"]] == [(100, 130)]


def test_llm_alone_marks_with_a_second_detector_cuts(cfg):
    alone = plan(cfg, {"llm": [seg(100, 160, conf=0.95)]})
    assert alone["cuts"] == [] and [(m["start"], m["end"]) for m in alone["marks"]] == [(100, 160)]
    both = plan(cfg, {"llm": [seg(100, 160, conf=0.95)], "voice": [seg(102, 158, conf=0.9, kind="hint")]})
    assert [(c["start"], c["end"]) for c in both["cuts"]] == [(100, 160)]
