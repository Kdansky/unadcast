from unadcast.stages import download, poll


def test_poll_inserts_episodes_and_honours_keep(ctx):
    assert poll.poll(ctx, ctx.feed("show")) is True
    eps = ctx.pending("show")
    assert [e.title for e in eps] == ["ep3", "ep2", "ep1"]
    assert eps[0].url.endswith("/ep3.mp3")
    assert eps[0].published > eps[1].published
    assert len({e.id for e in eps}) == 3

    ctx.feed("show")["keep"] = 2
    assert [e.title for e in ctx.pending("show")] == ["ep3", "ep2"]


def test_poll_is_conditional(ctx):
    assert poll.poll(ctx, ctx.feed("show")) is True
    assert poll.poll(ctx, ctx.feed("show")) is False
    assert (ctx.data / "work" / "show" / "feed.xml").read_text().startswith("<?xml")


def test_episode_ids_are_stable(ctx, make_ctx, feed_url):
    poll.poll(ctx, ctx.feed("show"))
    other = make_ctx(feeds=[{"slug": "show", "url": feed_url}])
    poll.poll(other, other.feed("show"))
    assert [e.id for e in ctx.pending("show")] == [e.id for e in other.pending("show")]


def test_download(ctx, media):
    poll.poll(ctx, ctx.feed("show"))
    ep = next(e for e in ctx.pending("show") if e.title == "ep1")
    download.STAGE.execute(ep)
    assert ep.path("original").read_bytes() == (media / "ep1.mp3").read_bytes()
    assert not download.STAGE.stale(ep)


def test_excluded_titles_are_skipped(ctx):
    poll.poll(ctx, ctx.feed("show"))
    ctx.feed("show")["exclude"] = ["EP2", "nothing like this"]
    assert [e.title for e in ctx.pending("show")] == ["ep3", "ep1"]
    # Exclusion doesn't eat into keep: the next non-excluded episode moves up.
    ctx.feed("show")["keep"] = 2
    assert [e.title for e in ctx.pending("show")] == ["ep3", "ep1"]
