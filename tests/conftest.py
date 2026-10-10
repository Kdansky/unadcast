import functools
import http.server
import threading
from email.utils import formatdate

import pytest

from unadcast import config
from unadcast.core import Ctx
from tests import synth

INTRO = 12.0

# The first music part of each episode is the show's intro; every later one is an ad. Episode 2 starts its ads half a
# chromaprint frame (~0.06 s) off the grid episode 1 uses, the worst case for fingerprint matching.
EPISODES = {
    "ep1": [("music", INTRO, 100), ("speech", 60, 1), ("music", 30, 7), ("speech", 70, 2)],
    "ep2": [("music", INTRO, 100), ("speech", 45.06, 3), ("music", 25, 8), ("speech", 30, 4), ("music", 30, 7), ("speech", 40, 5)],
    "ep3": [("music", INTRO, 100), ("speech", 90, 6)],
}


def ads(title):
    """(start, end) of each ad in an episode."""
    out, t = [], 0.0
    for i, (kind, s, _) in enumerate(EPISODES[title]):
        if kind == "music" and i > 0:
            out.append((t, t + s))
        t += s
    return out


def parts(spec):
    return [synth.speech(s, seed) if kind == "speech" else synth.music(s, seed) for kind, s, seed in spec]


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    for name, spec in EPISODES.items():
        synth.render(parts(spec), d / f"{name}.mp3")
    return d


@pytest.fixture(scope="session")
def server(media):
    handler = functools.partial(QuietHandler, directory=str(media))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def feed_xml(base, names, title="Test Show"):
    items = "".join(
        f"""<item><title>{n}</title><guid isPermaLink="false">guid-{n}</guid>
        <pubDate>{formatdate(1_700_000_000 + 86400 * int(n[2:]))}</pubDate>
        <description>About {n}</description><itunes:duration>3:00</itunes:duration>
        <enclosure url="{base}/{n}.mp3" length="1" type="audio/mpeg"/></item>"""
        for n in reversed(names)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
<channel><title>{title}</title><link>https://example.com</link><description>A show</description>
<atom:link href="{base}/feed.xml" rel="self" type="application/rss+xml"/>
<itunes:new-feed-url>{base}/feed.xml</itunes:new-feed-url>
{items}</channel></rss>"""


@pytest.fixture(scope="session")
def feed_url(media, server):
    (media / "feed.xml").write_text(feed_xml(server, list(EPISODES)))
    return f"{server}/feed.xml"


@pytest.fixture
def make_ctx(tmp_path):
    def make(**over):
        # No GPU or LLM server in tests: transcription is off unless a test turns it on.
        over = {"transcribe": {"enabled": False}, **over, "paths": {"data": "data", **over.get("paths", {})}}
        return Ctx.open(config.build(over, tmp_path))

    return make


@pytest.fixture
def ctx(make_ctx, feed_url):
    return make_ctx(feeds=[{"slug": "show", "url": feed_url}])


@pytest.fixture(scope="session")
def prepared(tmp_path_factory, feed_url):
    """A context whose episodes are polled, downloaded and prepared, shared by the detector tests."""
    from unadcast.stages import download, poll, prepare

    cfg = {"feeds": [{"slug": "show", "url": feed_url}], "transcribe": {"enabled": False}}
    ctx = Ctx.open(config.build(cfg, tmp_path_factory.mktemp("prep")))
    poll.poll(ctx, ctx.feed("show"))
    for ep in ctx.pending("show"):
        download.STAGE.execute(ep)
        prepare.STAGE.execute(ep)
    return ctx


def episode(ctx, title):
    return next(e for e in ctx.pending("show") if e.title == title)


def overlap(a, b):
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
