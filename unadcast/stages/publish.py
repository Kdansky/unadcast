"""Rewrite the original feed document in place, so every tag and namespace the publisher used survives."""

import json
import os
import shutil

from lxml import etree

from ..config import excluded
from ..core import write
from .poll import episode_id, items, parse

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def clock(t):
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def note(out, plan):
    if out["mode"] == "mark":
        return f"unadcast: nothing removed, {len(plan['cuts']) + len(plan['marks'])} marked."
    r = out["removed"]
    return f"unadcast: removed {int(r // 60)}:{int(r % 60):02d} in {len(plan['cuts'])} cuts, {len(plan['marks'])} marked."


def publish(ctx, feed):
    slug = feed["slug"]
    base = ctx.cfg["serve"]["base_url"].rstrip("/")
    root = parse((ctx.data / "work" / slug / "feed.xml").read_bytes())
    channel = root.find("channel")

    title = channel.find("title")
    title.text = f"{title.text} (unadcast)"
    # Either would let a podcast app "helpfully" move the subscription back to the original feed.
    for el in channel.findall("{http://www.w3.org/2005/Atom}link[@rel='self']") + channel.findall(f"{{{ITUNES}}}new-feed-url"):
        channel.remove(el)

    audio = ctx.public / slug
    audio.mkdir(parents=True, exist_ok=True)
    served = set()
    for item, guid, _, title, _ in list(items(root)):
        if excluded(feed, title):
            channel.remove(item)
            continue
        enc = item.find("enclosure")
        d = ctx.data / "work" / slug / episode_id(slug, guid)
        if not (d / "out.mp3").exists() or not (d / "out.json").exists():
            if ctx.cfg["publish"]["unprocessed"] == "hide":
                channel.remove(item)
            continue
        out = json.loads((d / "out.json").read_text())
        if not out["duration"]:
            channel.remove(item)
            continue
        mirror(d / "out.mp3", audio / f"{d.name}.mp3")
        served.add(f"{d.name}.mp3")
        enc.set("url", f"{base}/{slug}/{d.name}.mp3")
        enc.set("length", str((audio / f"{d.name}.mp3").stat().st_size))
        enc.set("type", "audio/mpeg")
        dur = item.find(f"{{{ITUNES}}}duration")
        if dur is None:
            dur = etree.SubElement(item, f"{{{ITUNES}}}duration")
        dur.text = clock(out["duration"])
        desc = item.find("description")
        if desc is None:
            desc = etree.SubElement(item, "description")
        desc.text = f"{note(out, json.loads((d / 'plan.json').read_text()))}\n\n{desc.text or ''}"

    # The audio is in place before the feed points at it; files the feed no longer names go after.
    write(ctx.public / f"{slug}.xml", etree.tostring(root, xml_declaration=True, encoding="UTF-8"))
    for f in audio.iterdir():
        if f.name not in served:
            f.unlink()


def mirror(src, dst):
    """Copy unless dst already has src's size and mtime. Via a temp name: a reader never sees half a file."""
    s = src.stat()
    if dst.exists() and (dst.stat().st_size, int(dst.stat().st_mtime)) == (s.st_size, int(s.st_mtime)):
        return
    tmp = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, tmp)
    os.utime(tmp, (s.st_atime, s.st_mtime))
    os.replace(tmp, dst)

