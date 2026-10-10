import wave

import numpy as np

from tests.conftest import ads, episode


def test_prepare_outputs(prepared):
    ep = episode(prepared, "ep1")
    with wave.open(str(ep.path("audio.wav"))) as w:
        assert (w.getframerate(), w.getnchannels()) == (16000, 1)
        duration = w.getnframes() / 16000
    assert abs(duration - 172) < 0.2

    meta = ep.load("meta.json")
    assert meta["codec"] == "mp3"
    assert abs(meta["duration"] - duration) < 0.2

    silences = ep.load("silences.json")
    assert len(silences) > 5
    assert all(0 <= s < e <= duration + 0.1 for s, e in silences)

    L = ep.load("loudness.json")
    assert L["step"] == 0.1
    assert abs(len(L["s"]) - duration / 0.1) < 5
    assert len(L["m"]) == len(L["s"])


def test_loudness_is_time_aligned(prepared):
    L = episode(prepared, "ep1").load("loudness.json")
    (a, b), = ads("ep1")
    # Energy averages in dB lean towards the loud side, so a wider window crosses earlier.
    for curve, slack in [("s", 1.0), ("m", 0.3)]:
        x = np.array(L[curve])
        at = lambda t0, t1: np.median(x[int(t0 / L["step"]) : int(t1 / L["step"])])
        speech, ad = at(a - 30, a - 5), at(a + 5, b - 5)
        assert ad - speech > 6
        # Where the curve first crosses halfway between the two levels is where the ad starts.
        cross = np.flatnonzero(x[int((a - 10) / L["step"]) :] > (speech + ad) / 2)[0] * L["step"] + a - 10
        assert abs(cross - a) < slack, curve
