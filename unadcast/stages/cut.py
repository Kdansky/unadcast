"""Render the plan in one ffmpeg run: cut mode removes cuts and chapters the marks, mark mode only adds chapters."""

import json
import os
import subprocess

from ..core import Stage


def remap(t, keep, crossfade):
    """Where original time `t` lands in the output; times inside a cut land at the join."""
    out = 0.0
    for k, (a, b) in enumerate(keep):
        shift = k * crossfade
        if t < a:
            return max(0.0, out - shift)
        if t <= b:
            return max(0.0, out + t - a - shift)
        out += b - a
    return max(0.0, out - max(0, len(keep) - 1) * crossfade)


def chapters(labelled, total):
    """Contiguous chapters: labelled spans, with "Show" filling every gap. None when nothing is labelled."""
    if not labelled:
        return None
    out, t = [], 0.0
    for a, b, title in sorted(labelled):
        if a - t > 0.5:
            out.append((t, a, "Show"))
        out.append((max(a, t), b, title))
        t = max(t, b)
    if total - t > 0.5:
        out.append((t, total, "Show"))
    return ";FFMETADATA1\n" + "".join(
        f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(a * 1000)}\nEND={int(b * 1000)}\ntitle={title}\n" for a, b, title in out
    )


def run(ep):
    cfg = ep.ctx.cfg["publish"]
    mode = ep.ctx.feed(ep.feed)["mode"]
    plan = ep.load("plan.json")
    cf = cfg["crossfade"]
    cuts = plan["cuts"] if mode == "cut" else []
    keep = plan["keep"] if cuts else [[0.0, plan["duration"]]]
    if not keep:
        # All of it is cut: a promo or trailer dropped into the feed. Publishing leaves it out.
        ep.path("out.mp3").write_bytes(b"")
        ep.save("out.json", {"mode": mode, "duration": 0, "removed": plan["duration"]})
        return
    total = remap(plan["duration"], keep, cf)
    labelled = [(remap(s["start"], keep, cf), remap(s["end"], keep, cf), "Ad?") for s in plan["marks"]]
    if mode == "mark":
        labelled += [(s["start"], s["end"], "Ad") for s in plan["cuts"]]
    meta = chapters([s for s in labelled if s[1] > s[0]], total)

    cmd = ["ffmpeg", "-v", "error", "-y", "-i", ep.path("original")]
    if meta:
        ep.path("chapters.txt").write_text(meta)
        cmd += ["-i", ep.path("chapters.txt")]
    if cuts:
        n = len(keep)
        graph = [f"[0:a]asplit={n}" + "".join(f"[s{k}]" for k in range(n))]
        graph += [f"[s{k}]atrim={a}:{b},asetpts=PTS-STARTPTS[k{k}]" for k, (a, b) in enumerate(keep)]
        last = "k0"
        for k in range(1, n):
            graph.append(f"[{last}][k{k}]acrossfade=d={cf}[x{k}]")
            last = f"x{k}"
        cmd += ["-filter_complex", ";".join(graph), "-map", f"[{last}]"]
    else:
        cmd += ["-map", "0:a"]
    copy = not cuts and ep.load("meta.json")["codec"] == "mp3"
    cmd += ["-c:a", "copy"] if copy else ["-c:a", "libmp3lame", "-q:a", str(cfg["quality"])]
    # Chapters: ours when there are any; the original's only while the timeline is untouched.
    chap = "1" if meta else "-1" if cuts else "0"
    tmp = ep.path("out.mp3.part")
    cmd += ["-map", "0:v?", "-c:v", "copy", "-map_metadata", "0", "-map_chapters", chap, "-id3v2_version", "3", "-f", "mp3", tmp]
    subprocess.run(cmd, check=True, capture_output=True)
    os.replace(tmp, ep.path("out.mp3"))

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", ep.path("out.mp3")],
        capture_output=True, text=True, check=True,
    )
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    ep.save("out.json", {"mode": mode, "duration": duration, "removed": round(plan["duration"] - total, 2) if cuts else 0})


STAGE = Stage(
    "cut", run, inputs=("original", "plan.json", "meta.json"), outputs=("out.mp3", "out.json"),
    config=lambda cfg, ep: {"publish": cfg["publish"], "mode": ep.ctx.feed(ep.feed)["mode"]},
)
