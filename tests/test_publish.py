import shutil

from lxml import etree

from unadcast.stages import poll, publish

NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd", "atom": "http://www.w3.org/2005/Atom"}


def fake_output(ep, removed=62.0, duration=3725.0):
    ep.dir.mkdir(parents=True, exist_ok=True)
    ep.path("out.mp3").write_bytes(b"x" * 1234)
    ep.save("out.json", {"mode": "cut", "duration": duration, "removed": removed})
    ep.save("plan.json", {"cuts": [{}, {}], "marks": [{}]})


def items(ctx):
    root = etree.parse(str(ctx.public / "show.xml")).getroot()
    return root, {i.findtext("title"): i for i in root.iter("item")}


def test_rewrites_processed_items(ctx):
    ctx.cfg["serve"]["base_url"] = "http://box:8765/"
    poll.poll(ctx, ctx.feed("show"))
    ep = ctx.pending("show")[1]
    fake_output(ep)
    publish.publish(ctx, ctx.feed("show"))

    root, by = items(ctx)
    enc = by["ep2"].find("enclosure")
    assert enc.get("url") == f"http://box:8765/show/{ep.id}.mp3"
    assert (enc.get("length"), enc.get("type")) == ("1234", "audio/mpeg")
    assert by["ep2"].findtext("itunes:duration", namespaces=NS) == "1:02:05"
    assert by["ep2"].findtext("description").startswith("unadcast: removed 1:02 in 2 cuts, 1 marked.")
    assert by["ep2"].findtext("guid") == "guid-ep2"
    # Unprocessed items still play, from the original host.
    assert by["ep1"].find("enclosure").get("url").endswith("/ep1.mp3")

    channel = root.find("channel")
    assert channel.findtext("title") == "Test Show (unadcast)"
    assert channel.find("atom:link", NS) is None
    assert channel.find("itunes:new-feed-url", NS) is None


def test_hides_unprocessed(ctx):
    ctx.cfg["publish"]["unprocessed"] = "hide"
    poll.poll(ctx, ctx.feed("show"))
    fake_output(ctx.pending("show")[0])
    publish.publish(ctx, ctx.feed("show"))
    assert list(items(ctx)[1]) == [ctx.pending("show")[0].title]


def test_all_cut_episodes_leave_the_feed(ctx):
    poll.poll(ctx, ctx.feed("show"))
    ep2 = next(e for e in ctx.pending("show") if e.title == "ep2")
    fake_output(ep2, removed=200.0, duration=0)
    publish.publish(ctx, ctx.feed("show"))
    assert sorted(items(ctx)[1]) == ["ep1", "ep3"]
    assert not (ctx.public / "show" / f"{ep2.id}.mp3").exists()

def test_mirrors_audio_into_public_dir(ctx):
    poll.poll(ctx, ctx.feed("show"))
    a, b = ctx.pending("show")[:2]
    fake_output(a)
    fake_output(b)
    publish.publish(ctx, ctx.feed("show"))
    pub = ctx.public / "show"
    assert sorted(p.name for p in pub.iterdir()) == sorted([f"{a.id}.mp3", f"{b.id}.mp3"])
    assert (pub / f"{a.id}.mp3").read_bytes() == a.path("out.mp3").read_bytes()
    copied = (pub / f"{a.id}.mp3").stat().st_mtime_ns

    # Unchanged files stay put; changed ones are copied again; gone ones are removed.
    b.path("out.mp3").write_bytes(b"y" * 99)
    shutil.rmtree(ctx.pending("show")[2].dir, ignore_errors=True)
    publish.publish(ctx, ctx.feed("show"))
    assert (pub / f"{a.id}.mp3").stat().st_mtime_ns == copied
    assert (pub / f"{b.id}.mp3").stat().st_size == 99
    shutil.rmtree(a.dir)
    publish.publish(ctx, ctx.feed("show"))
    assert [p.name for p in pub.iterdir()] == [f"{b.id}.mp3"]


def test_public_dir_is_configurable(make_ctx, feed_url, tmp_path):
    ctx = make_ctx(feeds=[{"slug": "show", "url": feed_url}], paths={"public": str(tmp_path / "nas")})
    poll.poll(ctx, ctx.feed("show"))
    fake_output(ctx.pending("show")[0])
    publish.publish(ctx, ctx.feed("show"))
    assert (tmp_path / "nas" / "show.xml").exists() and len(list((tmp_path / "nas" / "show").iterdir())) == 1


def test_excluded_episodes_leave_the_feed(ctx):
    poll.poll(ctx, ctx.feed("show"))
    ep2 = next(e for e in ctx.pending("show") if e.title == "ep2")
    fake_output(ep2)
    publish.publish(ctx, ctx.feed("show"))
    assert "ep2" in items(ctx)[1] and (ctx.public / "show" / f"{ep2.id}.mp3").exists()
    ctx.feed("show")["exclude"] = ["ep2"]
    publish.publish(ctx, ctx.feed("show"))
    assert sorted(items(ctx)[1]) == ["ep1", "ep3"]
    assert not (ctx.public / "show" / f"{ep2.id}.mp3").exists()
