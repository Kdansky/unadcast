import os

from unadcast import config
from unadcast.core import Episode, Stage


def test_build_fills_defaults_and_feed_mode(tmp_path):
    cfg = config.build({"publish": {"mode": "cut"}, "feeds": [{"slug": "a", "url": "u"}, {"slug": "b", "url": "u", "mode": "mark"}]}, tmp_path)
    assert [f["mode"] for f in cfg["feeds"]] == ["cut", "mark"]
    assert cfg["feeds"][0]["keep"] == 10
    assert cfg["fuse"]["kinds"]["ad"] == 1.0
    assert cfg["paths"]["data"] == str(tmp_path / "data")


def test_server_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("UNADCAST_SERVER", "http://pi:8765/")
    cfg = config.build({}, tmp_path)
    assert (cfg["feeds_url"], cfg["serve"]["base_url"]) == ("http://pi:8765/admin/api/feeds", "http://pi:8765")
    assert config.build({"serve": {"base_url": "http://other"}}, tmp_path)["serve"]["base_url"] == "http://other"


def test_load_reads_toml(tmp_path):
    (tmp_path / "c.toml").write_text('[detect.loudness]\nweight = 0.1\n[[feeds]]\nslug = "x"\nurl = "u"\n')
    cfg = config.load(tmp_path / "c.toml")
    assert cfg["detect"]["loudness"]["weight"] == 0.1
    assert cfg["detect"]["loudness"]["enabled"] is True
    assert cfg["feeds"][0]["slug"] == "x"


def make_episode(make_ctx):
    ctx = make_ctx(feeds=[{"slug": "f", "url": "u"}], prepare={"silence_db": -30})
    return Episode("e1", "f", "g", "t", 0.0, "u", 0, ctx=ctx)


def test_stage_staleness(make_ctx):
    ep = make_episode(make_ctx)
    calls = []

    def run(ep):
        calls.append(1)
        ep.path("out.txt").write_text(ep.path("in.txt").read_text().upper())

    st = Stage("up", run, inputs=("in.txt",), outputs=("out.txt",), config=lambda cfg, ep: cfg["prepare"])
    assert not st.ready(ep)
    ep.dir.mkdir(parents=True)
    ep.path("in.txt").write_text("a")
    assert st.ready(ep) and st.stale(ep)
    st.execute(ep)
    assert ep.path("out.txt").read_text() == "A"
    assert not st.stale(ep)

    ep.path("in.txt").write_text("bb")
    assert st.stale(ep)
    st.execute(ep)
    assert not st.stale(ep)

    ep.ctx.cfg["prepare"]["silence_db"] = -40
    assert st.stale(ep)
    st.execute(ep)

    ep.path("out.txt").unlink()
    assert st.stale(ep)
    st.execute(ep)

    st.version = 2
    assert st.stale(ep)
    assert len(calls) == 4


def test_optional_inputs_count_when_present(make_ctx):
    ep = make_episode(make_ctx)
    ep.dir.mkdir(parents=True)
    st = Stage("fuse", lambda ep: None, optional=("votes/*.json",))
    st.execute(ep)
    assert not st.stale(ep)
    ep.save("votes/a.json", {})
    assert st.stale(ep)


def test_failed_stage_leaves_no_stamp(make_ctx):
    ep = make_episode(make_ctx)

    def boom(ep):
        raise RuntimeError("x")

    st = Stage("boom", lambda ep: None)
    st.execute(ep)
    st.run = boom
    try:
        st.execute(ep)
    except RuntimeError:
        pass
    assert not st.stamp(ep).exists()
    assert not any(p.name.endswith(".part") for p in ep.dir.rglob("*"))
    assert os.path.isdir(ep.dir)


def test_feed_entries_keep_their_exclusions(tmp_path):
    cfg = config.build({"feeds": [{"slug": "a", "url": "u", "exclude": ["Weekly"], "title": "dropped"}]}, tmp_path)
    assert cfg["feeds"][0]["exclude"] == ["Weekly"] and "title" not in cfg["feeds"][0]
    assert config.build({"feeds": [{"slug": "a", "url": "u"}]}, tmp_path)["feeds"][0]["exclude"] == []
