import hashlib
import time
from email.utils import parsedate_to_datetime

import httpx
from lxml import etree

from ..core import write


def episode_id(slug, guid):
    return hashlib.sha1(f"{slug}\n{guid}".encode()).hexdigest()[:12]


def items(root):
    """(element, guid, enclosure url, title, published) for each item with an enclosure. Publish relies on the same guids."""
    for el in root.iter("item"):
        enc = el.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        guid = (el.findtext("guid") or "").strip() or enc.get("url")
        try:
            published = parsedate_to_datetime(el.findtext("pubDate").strip()).timestamp()
        except (AttributeError, TypeError, ValueError):
            published = 0
        yield el, guid, enc.get("url"), (el.findtext("title") or "").strip(), published


def parse(data):
    return etree.fromstring(data, etree.XMLParser(strip_cdata=False, resolve_entities=False, huge_tree=True))


def poll(ctx, feed):
    """Fetch a feed and upsert its episodes. Returns False when the server says nothing changed."""
    slug = feed["slug"]
    path = ctx.data / "work" / slug / "feed.xml"
    row = ctx.con.execute("SELECT * FROM feeds WHERE slug = ?", (slug,)).fetchone()
    headers = {"User-Agent": ctx.cfg["download"]["user_agent"]}
    if row and row["url"] == feed["url"] and path.exists():
        headers |= {k: v for k, v in [("If-None-Match", row["etag"]), ("If-Modified-Since", row["modified"])] if v}

    r = httpx.get(feed["url"], headers=headers, follow_redirects=True, timeout=ctx.cfg["download"]["timeout"])
    if r.status_code == 304:
        ctx.con.execute("UPDATE feeds SET polled = ? WHERE slug = ?", (time.time(), slug))
        ctx.con.commit()
        return False
    r.raise_for_status()
    root = parse(r.content)
    write(path, r.content)

    for pos, (_, guid, url, title, published) in enumerate(items(root)):
        ctx.con.execute(
            "INSERT OR REPLACE INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (episode_id(slug, guid), slug, guid, title, published, url, pos),
        )
    ctx.con.execute(
        "INSERT OR REPLACE INTO feeds VALUES (?, ?, ?, ?, ?)",
        (slug, feed["url"], r.headers.get("etag"), r.headers.get("last-modified"), time.time()),
    )
    ctx.con.commit()
    return True
