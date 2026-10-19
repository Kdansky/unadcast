import json
import subprocess

import pytest

from unadcast.stages import cut

from tests.conftest import episode


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", path],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def span(start, end, action):
    return {"start": start, "end": end, "p": 0.9, "action": action, "kind": "ad", "scores": {}, "evidence": []}


def write_plan(ep, cuts, marks):
    d = ep.load("meta.json")["duration"]
    keep, t = [], 0.0
    for c in cuts:
        keep.append([t, c["start"]])
        t = c["end"]
    keep.append([t, d])
    ep.save("plan.json", {"duration": d, "keep": keep, "cuts": cuts, "marks": marks, "removed": 0})


@pytest.fixture
def ep(prepared):
    e = episode(prepared, "ep2")
    yield e
    prepared.feed("show")["mode"] = "mark"


def test_cut_mode_removes_cuts(ep, prepared):
    prepared.feed("show")["mode"] = "cut"
    write_plan(ep, [span(57.06, 82.06, "cut"), span(112.06, 142.06, "cut")], [span(20, 35, "mark")])
    cut.STAGE.execute(ep)
    info = probe(ep.path("out.mp3"))
    expected = ep.load("meta.json")["duration"] - 55 - 2 * prepared.cfg["publish"]["crossfade"]
    assert abs(float(info["format"]["duration"]) - expected) < 0.15
    assert info["streams"][0]["codec_name"] == "mp3"
    titles = [(c["tags"]["title"], round(float(c["start_time"]))) for c in info["chapters"]]
    assert titles == [("Show", 0), ("Ad?", 20), ("Show", 35)]
    out = ep.load("out.json")
    assert out["mode"] == "cut" and out["removed"] == pytest.approx(55, abs=0.5)


def test_all_cut_renders_nothing(ep, prepared):
    prepared.feed("show")["mode"] = "cut"
    d = ep.load("meta.json")["duration"]
    ep.save("plan.json", {"duration": d, "keep": [], "cuts": [span(0, d, "cut")], "marks": [], "removed": d})
    cut.STAGE.execute(ep)
    assert ep.load("out.json") == {"mode": "cut", "duration": 0, "removed": d}
    assert not cut.STAGE.stale(ep)

def test_mark_mode_keeps_audio_and_adds_chapters(ep):
    write_plan(ep, [span(57.06, 82.06, "cut")], [span(112.06, 142.06, "mark")])
    cut.STAGE.execute(ep)
    info = probe(ep.path("out.mp3"))
    assert abs(float(info["format"]["duration"]) - ep.load("meta.json")["duration"]) < 0.1
    titles = [(c["tags"]["title"], round(float(c["start_time"]))) for c in info["chapters"]]
    assert titles == [("Show", 0), ("Ad", 57), ("Show", 82), ("Ad?", 112), ("Show", 142)]
    # Nothing was cut, so the audio is the original's, bit for bit.
    assert ep.load("out.json")["removed"] == 0


def test_mode_change_makes_cut_stale(ep, prepared):
    write_plan(ep, [], [])
    cut.STAGE.execute(ep)
    assert probe(ep.path("out.mp3")).get("chapters") == []
    assert not cut.STAGE.stale(ep)
    prepared.feed("show")["mode"] = "cut"
    assert cut.STAGE.stale(ep)


def test_remap_through_cuts():
    keep = [[0, 50], [80, 100], [130, 200]]
    assert cut.remap(10, keep, 0) == 10
    assert cut.remap(90, keep, 0) == 60
    assert cut.remap(90, keep, 1) == 59
    assert cut.remap(60, keep, 0) == 50
    assert cut.remap(250, keep, 0) == 140


def test_keeps_cover_art(tmp_path, ep, prepared):
    cover, art = tmp_path / "cover.jpg", tmp_path / "with-art.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x64", "-frames:v", "1", cover], check=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", ep.path("original"), "-i", cover, "-map", "0:a", "-map", "1:v", "-c", "copy",
         "-disposition:v", "attached_pic", "-metadata", "title=Arty", art],
        check=True,
    )
    ep.path("original").write_bytes(art.read_bytes())
    try:
        prepared.feed("show")["mode"] = "cut"
        write_plan(ep, [span(57.06, 82.06, "cut")], [])
        cut.STAGE.execute(ep)
        info = probe(ep.path("out.mp3"))
        assert [s["codec_type"] for s in info["streams"]] == ["audio", "video"]
        assert info["format"]["tags"]["title"] == "Arty"
        assert abs(float(info["format"]["duration"]) - (ep.load("meta.json")["duration"] - 25)) < 0.5
    finally:
        from unadcast.stages import download
        download.STAGE.execute(ep)
