import json
import shutil
import subprocess
import time

import pytest
from starlette.testclient import TestClient

from unadcast import admin


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "feeds.json", tmp_path / "status.json"


@pytest.fixture
def client(paths):
    return TestClient(admin.app(*paths))


def test_add_list_edit_remove(client, paths, feed_url):
    r = client.post("/admin/api/feeds", json={"url": feed_url})
    assert r.status_code == 201
    f = r.json()
    # Title and slug come from the feed itself; new shows start in mark mode with ten episodes.
    assert (f["slug"], f["title"], f["keep"], f["mode"]) == ("test-show", "Test Show", 10, "mark")
    assert client.get("/admin/api/feeds").json() == [f]

    before = client.get("/admin/api/run").json()["requested"]
    assert client.patch("/admin/api/feeds/test-show", json={"mode": "cut", "keep": 3}).json()["mode"] == "cut"
    # Applying an edit takes a pass, so it asks for one.
    assert client.get("/admin/api/run").json()["requested"] > before
    assert client.patch("/admin/api/feeds/test-show", json={"mode": "chop"}).status_code == 400
    assert client.patch("/admin/api/feeds/test-show", json={"keep": 0}).status_code == 400
    assert client.patch("/admin/api/feeds/test-show", json={"keep": 500}).status_code == 400

    # Saved to disk, so a restarted app sees the same list.
    again = TestClient(admin.app(*paths)).get("/admin/api/feeds").json()
    assert [(x["slug"], x["mode"], x["keep"]) for x in again] == [("test-show", "cut", 3)]

    assert client.delete("/admin/api/feeds/test-show").status_code == 204
    assert client.get("/admin/api/feeds").json() == []
    assert client.delete("/admin/api/feeds/test-show").status_code == 404


def test_rejects_bad_feeds(client, feed_url, server):
    assert client.post("/admin/api/feeds", json={"url": "ftp://x/feed"}).status_code == 400
    assert client.post("/admin/api/feeds", json={"url": f"{server}/ep1.mp3"}).status_code == 400
    assert client.post("/admin/api/feeds", json={"url": f"{server}/missing.xml"}).status_code == 400
    assert client.post("/admin/api/feeds", json={"url": feed_url}).status_code == 201
    assert client.post("/admin/api/feeds", json={"url": feed_url}).status_code == 409
    # A second show whose title makes the same slug gets a numbered one.
    assert client.post("/admin/api/feeds", json={"url": feed_url + "?other"}).json()["slug"] == "test-show-2"


def test_search(client, monkeypatch):
    monkeypatch.setattr(admin, "itunes", lambda term: [{"collectionName": "Behind the Bastards", "artistName": "iHeart", "feedUrl": "https://x/rss", "artworkUrl100": "a.jpg"}, {"collectionName": "No feed"}])
    assert client.get("/admin/api/search", params={"q": "bastards"}).json() == [
        {"title": "Behind the Bastards", "author": "iHeart", "url": "https://x/rss", "artwork": "a.jpg"}
    ]


def test_status_and_page(client, paths):
    assert client.get("/admin/api/status").json() == {}
    paths[1].write_text(json.dumps({"updated": 1, "feeds": {}}))
    assert client.get("/admin/api/status").json()["updated"] == 1
    page = client.get("/admin/")
    assert page.status_code == 200 and "unadcast" in page.text and "/admin/api/" in page.text


def test_status_over_http(paths, server):
    # From the `files` container: not there yet is no pass yet; no answer is storage unreachable.
    assert TestClient(admin.app(paths[0], f"{server}/status.json")).get("/admin/api/status").json() == {}
    assert TestClient(admin.app(paths[0], "http://127.0.0.1:1/status.json")).get("/admin/api/status").json() == {"unreadable": True}
    assert TestClient(admin.app(paths[0], "http://127.0.0.1:1/status.json")).get("/admin/pending.xml").status_code == 503
    assert TestClient(admin.app(paths[0], "http://127.0.0.1:1/status.json")).get("/admin/api/waiting").json() == {"unreadable": True}


def test_pc_checks_in(client):
    assert client.get("/admin/api/pc").json() == {}
    r = client.post("/admin/api/pc", json={"host": "gpu-box", "dir": "/srv/unadcast", "busy": True})
    assert r.status_code == 200
    pc = client.get("/admin/api/pc").json()
    assert (pc["host"], pc["dir"], pc["busy"], pc["gpu"]) == ("gpu-box", "/srv/unadcast", True, [])
    assert abs(pc["seen"] - time.time()) < 5
    client.post("/admin/api/pc", json={"host": "gpu-box", "dir": "/d", "busy": True, "gpu": ["ollama", "cli"]})
    assert client.get("/admin/api/pc").json()["gpu"] == ["ollama", "cli"]
    assert client.post("/admin/api/pc", json={"host": 3}).status_code == 400
    assert client.post("/admin/api/pc", json={"host": "h", "dir": "/d", "busy": False, "gpu": "ollama"}).status_code == 400
    client.post("/admin/api/pc", json={"host": "h", "dir": "/d", "busy": False, "gpu": [], "postponed": 1234})
    assert client.get("/admin/api/pc").json()["postponed"] == 1234


def test_pass_output(client):
    assert client.get("/admin/api/log").json() == {}
    client.post("/admin/api/log", content="transcribe show/abc Episode 1\n", headers={"Content-Type": "text/plain"})
    out = client.get("/admin/api/log").json()
    assert out["text"] == "transcribe show/abc Episode 1\n" and abs(out["seen"] - time.time()) < 5
    client.post("/admin/api/log", content="x" * 50000)
    assert len(client.get("/admin/api/log").json()["text"]) == 20000


def test_detector_settings(client, paths):
    assert client.get("/admin/api/settings").json() == {"detect": {"repeat": True, "voice": True, "loudness": True, "llm": True}}
    before = client.get("/admin/api/run").json()["requested"]
    r = client.put("/admin/api/settings", json={"detect": {"llm": False, "loudness": False}})
    assert r.json()["detect"] == {"repeat": True, "voice": True, "loudness": False, "llm": False}
    # Saving asks for a pass, so the change shows up without another click.
    assert client.get("/admin/api/run").json()["requested"] > before
    assert TestClient(admin.app(*paths)).get("/admin/api/settings").json()["detect"]["llm"] is False
    assert client.put("/admin/api/settings", json={"detect": {"music": True}}).status_code == 400
    assert client.put("/admin/api/settings", json={"detect": {"llm": "no"}}).status_code == 400


def test_exclusions(client, feed_url):
    client.post("/admin/api/feeds", json={"url": feed_url})
    f = client.patch("/admin/api/feeds/test-show", json={"exclude": [" It Could Happen Here ", "", "Bonus"]}).json()
    assert f["exclude"] == ["It Could Happen Here", "Bonus"]
    assert client.patch("/admin/api/feeds/test-show", json={"exclude": "Weekly"}).status_code == 400
    assert client.patch("/admin/api/feeds/test-show", json={"exclude": ["x" * 201]}).status_code == 400
    assert client.get("/admin/api/feeds").json()[0]["exclude"] == ["It Could Happen Here", "Bonus"]


def test_stop(client):
    assert client.get("/admin/api/run").json() == {"requested": 0, "stop": 0, "pause": 0}
    client.post("/admin/api/run")
    r = client.post("/admin/api/stop").json()
    assert r["stop"] >= r["requested"] > 0
    # Process now after a stop is a new request.
    assert client.post("/admin/api/run").json()["requested"] > r["stop"]


def test_pause(client):
    r = client.post("/admin/api/pause", json={"hours": 3}).json()
    # Pausing also stops what runs now.
    assert abs(r["pause"] - (time.time() + 3 * 3600)) < 5 and abs(r["stop"] - time.time()) < 5
    r = client.post("/admin/api/pause", json={"hours": 0}).json()
    assert r["pause"] == 0 and r["stop"] > 0
    for bad in ({"hours": 25}, {"hours": "3"}, {"hours": True}, {}):
        assert client.post("/admin/api/pause", json=bad).status_code == 400


def test_status_survives_an_unreadable_share(client, paths):
    paths[1].mkdir()
    assert client.get("/admin/api/status").json() == {"unreadable": True}


def test_pending_feed(client, paths, feed_url):
    from unadcast.stages.poll import episode_id

    assert admin.episode_id("s", "g") == episode_id("s", "g")
    slug = client.post("/admin/api/feeds", json={"url": feed_url}).json()["slug"]
    client.patch(f"/admin/api/feeds/{slug}", json={"keep": 2, "exclude": ["EP3"]})
    # Status unreadable, as when the share it lives on is down: an error, not every episode announced as undone.
    paths[1].write_text("{")
    assert client.get("/admin/pending.xml").status_code == 503
    eps =[{"id": episode_id(slug, "guid-ep2"), "state": "done"}, {"id": episode_id(slug, "guid-ep1"), "state": "failed", "error": "boom"}]
    paths[1].write_text(json.dumps({"feeds": {slug: {"episodes": eps}}}))
    r = client.get("/admin/pending.xml", headers={"host": "pi:8765"})
    assert r.headers["content-type"].startswith("application/rss+xml")
    items = admin.ET.fromstring(r.content).findall("channel/item")
    # ep3 is excluded, ep2 done; ep1 failed and so still waits.
    assert [(i.findtext("title"), i.findtext("description"), i.findtext("link")) for i in items] == [
        ("Test Show: ep1", "failed: boom", "http://pi:8765/admin/")
    ]
    # The same, for the page's Process now button.
    assert [e["title"] for e in client.get("/admin/api/waiting").json()["episodes"]] == ["Test Show: ep1"]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_page_script_parses(tmp_path):
    # A syntax error anywhere stops the whole script, and the page shows nothing; Python escapes in PAGE cause them.
    js = tmp_path / "page.js"
    js.write_text(admin.PAGE.split("<script>")[1].split("</script>")[0])
    subprocess.run(["node", "--check", str(js)], check=True)
