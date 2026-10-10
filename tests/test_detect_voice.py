import numpy as np
import pytest

from unadcast import config
from unadcast.stages import detect_voice as dv

from tests.conftest import episode


def test_fbank_shape_and_chunking():
    x = np.random.default_rng(0).normal(0, 0.1, 16000 * 3).astype(np.float32)
    f = dv.fbank(x)
    assert f.shape == (1 + (len(x) - 400) // 160, 80)
    assert np.isfinite(f).all()
    # Chunked computation must match one pass exactly, or long episodes would differ from short ones.
    assert np.allclose(dv.fbank(x, chunk=37), f)


def test_talk_time_counts_similar_windows():
    rng = np.random.default_rng(1)
    host, guest, announcer = (v / np.linalg.norm(v) for v in rng.normal(size=(3, 256)))
    jitter = lambda v, n: (lambda e: e / np.linalg.norm(e, axis=1, keepdims=True))(v + rng.normal(0, 0.03, (n, 256)))
    E = np.concatenate([jitter(host, 300), jitter(announcer, 30), jitter(guest, 200), jitter(host, 100)])
    voiced = np.ones(len(E), bool)
    voiced[:5] = False
    talk = dv.talk(E, voiced, tau=0.4, chunk=64)
    assert talk[:5].tolist() == [np.inf] * 5
    assert talk[10] == 395 and talk[310] == 30 and talk[400] == 200


def test_runs_of_foreign_voice():
    c = {**config.DEFAULTS["detect"]["voice"], "cap_share": 1.0}
    talk = np.full(600, 1000.0)
    talk[100:140] = 30
    # A single show window that sounds odd, inside the ad, is bridged; a lone 8 s blip elsewhere is not an ad.
    talk[120] = 1000
    talk[300:308] = 30
    segs = dv.segments(talk, c)
    # Window i spans i..i+2 s, so a run of windows covers the middle seconds of its first and last window.
    assert [(s["start"], s["end"]) for s in segs] == [(100.5, 140.5)]
    assert segs[0]["kind"] == "hint" and 0.9 < segs[0]["confidence"] <= 1


def test_cap_shrinks_with_short_episodes():
    c = config.DEFAULTS["detect"]["voice"]
    # 200 voiced seconds: a voice heard for 30 s is 15% of the episode, a host, not an announcer.
    talk = np.full(200, 170.0)
    talk[100:130] = 30
    assert dv.segments(talk, c) == []
    talk[100:115] = 15
    assert [(s["start"], s["end"]) for s in dv.segments(talk, c)] == [(100.5, 115.5)]


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    return dv.model(tmp_path_factory.mktemp("models"))


def test_model_download_is_verified_and_cached(model):
    assert model.exists() and dv.sha256(model) == dv.SHA256
    mtime = model.stat().st_mtime_ns
    assert dv.model(model.parent).stat().st_mtime_ns == mtime


def test_stage_writes_votes(prepared, model):
    """Mechanics only: synthetic tones are no test of a speaker model. `unadcast eval` on real labels measures that."""
    prepared.cfg["paths"]["models"] = str(model.parent)
    ep = episode(prepared, "ep2")
    duration = ep.load("meta.json")["duration"]
    c = prepared.cfg["detect"]["voice"]
    c["cap_share"], c["min_length"] = 1.0, 5.0
    try:
        dv.STAGE.execute(ep)
    finally:
        c["cap_share"], c["min_length"] = 0.1, 15.0
    votes = ep.load("votes/voice.json")
    assert abs(votes["duration"] - duration) < 0.5
    # With the cap above the episode's length, every voice is foreign: voiced audio is one long flagged run.
    assert votes["segments"] and all(0 <= s["start"] < s["end"] <= duration + 2 for s in votes["segments"])
    assert sum(s["end"] - s["start"] for s in votes["segments"]) > duration * 0.8
