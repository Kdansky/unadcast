import json
import time

import pytest
from lxml import etree

from unadcast import runner

from tests.conftest import ads


def test_end_to_end(make_ctx, feed_url):
    ctx = make_ctx(feeds=[{"slug": "show", "url": feed_url, "mode": "cut"}], serve={"base_url": "http://box"})
    done = []
    runner.run(ctx, log=done.append)
    assert not [d for d in done if "failed" in d], done

    by = {e.title: e for e in ctx.pending("show")}
    plan = {t: e.load("plan.json") for t, e in by.items()}

    # The repeated ad is cut in both episodes; the one-off ad only has loudness behind it, so it is only marked.
    (a, b), = ads("ep1")
    (c,) = plan["ep1"]["cuts"]
    assert abs(c["start"] - a) < 1.5 and abs(c["end"] - b) < 1.5, c
    (b1, b2), (a1, a2) = ads("ep2")
    assert len(plan["ep2"]["cuts"]) == 1 and abs(plan["ep2"]["cuts"][0]["start"] - a1) < 1.5
    assert len(plan["ep2"]["marks"]) == 1 and abs(plan["ep2"]["marks"][0]["start"] - b1) < 1.5
    # The intro is loud music, but it is the show's own: protected.
    assert all(s["start"] > 20 for t in plan for s in plan[t]["cuts"] + plan[t]["marks"])
    assert plan["ep3"]["cuts"] == plan["ep3"]["marks"] == []

    out = by["ep1"].load("out.json")
    assert abs(out["removed"] - 30) < 3

    root = etree.parse(str(ctx.public / "show.xml")).getroot()
    urls = [i.find("enclosure").get("url") for i in root.iter("item")]
    assert sorted(urls) == sorted(f"http://box/show/{e.id}.mp3" for e in by.values())

    # Nothing changed, so a second run does no work.
    done.clear()
    runner.run(ctx, log=done.append)
    assert done == []


def test_failures_are_recorded_and_isolated(make_ctx, feed_url, server):
    ctx = make_ctx(feeds=[{"slug": "show", "url": feed_url}, {"slug": "gone", "url": f"{server}/missing.xml"}])
    done = []
    runner.run(ctx, log=done.append)
    assert any("gone" in d and "failed" in d for d in done)
    assert (ctx.public / "show.xml").exists()


def test_prunes_episodes_that_leave_the_window(make_ctx, feed_url):
    ctx = make_ctx(feeds=[{"slug": "show", "url": feed_url, "keep": 3}], serve={"base_url": "http://box"})
    runner.run(ctx, log=lambda m: None)
    oldest = ctx.pending("show")[-1]
    assert oldest.dir.exists()

    ctx.feed("show")["keep"] = 2
    runner.run(ctx, log=lambda m: None)
    assert not oldest.dir.exists()
    # Its fingerprint stays: older episodes are what newer ones are matched against.
    assert ctx.con.execute("SELECT COUNT(*) FROM prints WHERE episode = ?", (oldest.id,)).fetchone()[0] == 1
    root = etree.parse(str(ctx.public / "show.xml")).getroot()
    urls = {i.findtext("title"): i.find("enclosure").get("url") for i in root.iter("item")}
    assert not urls["ep1"].startswith("http://box") and urls["ep2"].startswith("http://box")


@pytest.mark.parametrize("off", ["loudness", "voice", "repeat", "all"])
def test_each_detector_can_be_switched_off(make_ctx, feed_url, off):
    names = ["loudness", "voice", "repeat"] if off == "all" else [off]
    ctx = make_ctx(feeds=[{"slug": "show", "url": feed_url}], detect={n: {"enabled": False} for n in names})
    logs = []
    runner.run(ctx, log=logs.append)
    assert not [l for l in logs if "failed" in l]
    assert not any(l.startswith(f"detect_{n} ") for l in logs for n in names)
    for ep in ctx.pending("show"):
        assert ep.path("plan.json").exists() and ep.path("out.mp3").exists()
        assert not any(ep.path(f"votes/{n}.json").exists() for n in names)
    if off == "all":
        # Nothing votes, so nothing is cut or marked: the episodes go out untouched.
        assert all(ep.load("plan.json")["cuts"] == ep.load("plan.json")["marks"] == [] for ep in ctx.pending("show"))


def test_feed_list_from_the_admin_page_with_offline_fallback(make_ctx, feed_url, media, server):
    (media / "remote").mkdir(exist_ok=True)
    (media / "remote" / "feeds").write_text(json.dumps([{"slug": "remote-show", "url": feed_url, "keep": 2, "mode": "cut", "title": "T"}]))
    ctx = make_ctx(feeds=[{"slug": "show", "url": feed_url}], feeds_url=f"{server}/remote/feeds")
    runner.run(ctx, log=lambda m: None)
    assert [(f["slug"], f["keep"], f["mode"]) for f in ctx.cfg["feeds"]] == [("show", 10, "mark"), ("remote-show", 2, "cut")]
    assert len(ctx.pending("remote-show")) == 2
    assert (ctx.public / "remote-show.xml").exists()

    # The admin page is down: the last list it gave stands, rather than every remote show vanishing.
    ctx.cfg["feeds_url"] = f"{server}/gone.json"
    ctx.cfg["feeds"] = [f for f in ctx.cfg["feeds"] if f["slug"] == "show"]
    runner.run(ctx, fetch=False, log=lambda m: None)
    assert [f["slug"] for f in ctx.cfg["feeds"]] == ["show", "remote-show"]


def test_status_file_for_the_admin_page(make_ctx, feed_url):
    ctx = make_ctx(feeds=[{"slug": "show", "url": feed_url}], serve={"base_url": "http://box"})
    before = time.time()
    runner.run(ctx, log=lambda m: None)
    st = json.loads((ctx.public / "status.json").read_text())
    assert before <= st["started"] <= st["updated"] and st["running"] is False
    show = st["feeds"]["show"]
    assert show["url"] == "http://box/show.xml" and show["title"] == "Test Show"
    assert [e["title"] for e in show["episodes"]] == ["ep3", "ep2", "ep1"]
    assert all(e["state"] == "done" for e in show["episodes"])
    assert {"cuts", "marks", "removed"} <= set(show["episodes"][0])


def test_wait_returns_early_when_a_pass_is_requested(make_ctx, media, server):
    (media / "admin" / "api").mkdir(parents=True, exist_ok=True)
    ctx = make_ctx(feeds_url=f"{server}/admin/api/feeds")
    (media / "admin" / "api" / "run").write_text(json.dumps({"requested": 0}))
    t = time.time()
    runner.wait(ctx, interval=0.6, log=lambda m: None, poll_every=0.1)
    assert time.time() - t >= 0.6
    (media / "admin" / "api" / "run").write_text(json.dumps({"requested": time.time() + 1}))
    t = time.time()
    runner.wait(ctx, interval=30, log=lambda m: None, poll_every=0.1)
    assert time.time() - t < 2


def test_detector_settings_from_the_admin_page(make_ctx, feed_url, media, server):
    (media / "admin" / "api").mkdir(parents=True, exist_ok=True)
    (media / "admin" / "api" / "feeds").write_text("[]")
    (media / "admin" / "api" / "settings").write_text(json.dumps({"detect": {"voice": False, "llm": False}}))
    ctx = make_ctx(feeds=[{"slug": "show", "url": feed_url}], feeds_url=f"{server}/admin/api/feeds", transcribe={"enabled": True})
    logs = []
    runner.run(ctx, log=logs.append)
    assert not ctx.cfg["detect"]["voice"]["enabled"] and ctx.cfg["detect"]["repeat"]["enabled"]
    # The LLM switch takes transcription with it: nothing else reads the transcript.
    assert not ctx.cfg["transcribe"]["enabled"]
    assert not any(l.startswith(("detect_voice", "transcribe")) for l in logs) and any(l.startswith("detect_repeat") for l in logs)

    # The admin page is down: the last settings stand.
    ctx.cfg["feeds_url"] = f"{server}/nowhere/feeds"
    ctx.cfg["detect"]["voice"]["enabled"] = True
    runner.run(ctx, fetch=False, log=lambda m: None)
    assert not ctx.cfg["detect"]["voice"]["enabled"]


def test_status_shows_what_is_being_done(make_ctx, feed_url, monkeypatch):
    ctx = make_ctx(feeds=[{"slug": "show", "url": feed_url}])
    seen = []
    real = runner.status

    def spy(ctx, started, running, current=None):
        real(ctx, started, running, current)
        if current:
            seen.append(json.loads((ctx.public / "status.json").read_text())["current"])

    monkeypatch.setattr(runner, "status", spy)
    runner.run(ctx, log=lambda m: None)
    assert {"stage", "feed", "title", "step", "of", "since"} <= set(seen[0])
    assert seen[0]["stage"] == "download" and seen[0]["step"] == 1
    # Counted within each stage: whether a later stage has work is only known once the earlier ones ran.
    for stage in {c["stage"] for c in seen}:
        steps = [c for c in seen if c["stage"] == stage]
        assert [c["step"] for c in steps] == list(range(1, len(steps) + 1)) and all(c["of"] == len(steps) for c in steps)
    assert "current" not in json.loads((ctx.public / "status.json").read_text())
