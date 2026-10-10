import os

import httpx

from ..core import Stage


def run(ep):
    cfg = ep.ctx.cfg["download"]
    tmp = ep.path("original.part")
    headers = {"User-Agent": cfg["user_agent"]}
    with httpx.stream("GET", ep.url, headers=headers, follow_redirects=True, timeout=cfg["timeout"]) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
    os.replace(tmp, ep.path("original"))


# No config: a changed user agent is no reason to fetch the same episode again.
STAGE = Stage("download", run, outputs=("original",))
