"""Read the transcript, ask a small model which numbered lines are advertising. It never sees a timestamp: small models
are bad at times and good at "lines 57 to 64"."""

from .. import llm
from ..core import Stage

SYSTEM = """You find advertising in podcast transcripts. Lines are numbered.
Advertising is:
- sponsor reads ("support for the show comes from", "brought to you by", "this episode is sponsored by"), including ones the hosts read themselves;
- offers, promo codes and URLs to buy or sign up for something;
- promotions for other podcasts or shows, often a short trailer in another voice ending in "listen wherever you get your podcasts".
Not advertising: the show's own discussion, even when it mentions products or companies; "we'll be right back" and "we're back"; the show's own newsletter, subscription, hotline or credits.
Answer with JSON only, no other text: {"ads": [{"first": <line>, "last": <line>, "kind": "ad" or "promo", "confidence": <0 to 1>}]}. Each entry is one continuous run of advertising lines; neighbouring ads are separate entries. If there is none, answer {"ads": []}."""


# A second, narrower question per flagged span. The first pass reads 8 minutes at once and is eager; asked about one
# passage, the same small model is much better at telling the show talking about products from an inserted ad.
VERIFY = """You check one passage from a podcast transcript. Decide whether the passage itself is advertising that was inserted into the episode:
- a paid sponsor read, also when a host reads it;
- a trailer or promo for a different podcast or show, also one from the same network (other iHeart, Vox, Cool Zone shows), usually ending in "listen on the iHeartRadio app" or "wherever you get your podcasts";
- network tags like "This is an iHeart Podcast".
It is NOT advertising if it is the show's own conversation (even about products, companies, apps, or advertising as a topic), the show plugging itself (its own newsletter, subscription, hotline, merch, credits), or a guest mentioning their own work during the conversation.
Answer with JSON only: {"ad": true or false, "confidence": <0 to 1>}"""


def verify(c, ls, seg):
    """The span's confidence after a focused second look, or None if it isn't an ad after all."""
    inside = [l[2] for l in ls if l[0] >= seg["start"] - 0.01 and l[1] <= seg["end"] + 0.01]
    text = " ".join(inside)
    if len(text) > 3000:
        text = text[:1500] + " [...] " + text[-1500:]
    before = " ".join(l[2] for l in ls if l[1] <= seg["start"])[-400:]
    after = " ".join(l[2] for l in ls if l[0] >= seg["end"])[:400]
    user = f"Before (context, not the passage):\n{before}\n\nPASSAGE:\n{text}\n\nAfter (context):\n{after}"
    r = llm.parse(llm.chat(c, VERIFY, user))
    if r.get("ad") is not True:
        return None
    return round(min(seg["confidence"], float(r.get("confidence", 0.5))), 3)


def lines(words, max_words=40, gap=1.0):
    """Group words into short lines: at sentence ends, pauses over `gap` seconds, or `max_words`."""
    out, cur = [], []
    for w in words:
        if cur and (w[0] - cur[-1][1] > gap or len(cur) >= max_words):
            out.append(cur)
            cur = []
        cur.append(w)
        if w[2].rstrip().endswith((".", "?", "!")):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [(l[0][0], l[-1][1], "".join(w[2] for w in l).strip()) for l in out]


def chunks(ls, minutes, overlap):
    """Index lists of lines, `minutes` long each, overlapping by `overlap`, so no ad is only ever seen cut in half."""
    out, t = [], 0.0
    while True:
        idx = [i for i, l in enumerate(ls) if t <= l[0] < t + minutes * 60]
        if idx:
            out.append(idx)
        if not ls or t + minutes * 60 > ls[-1][0]:
            return out
        t += (minutes - overlap) * 60


def run(ep):
    c = ep.ctx.cfg["detect"]["llm"]
    ls = lines(ep.load("transcript.json")["words"])
    # Per line, the most confident verdict any chunk gave it.
    verdict = {}
    for idx in chunks(ls, c["chunk_minutes"], c["overlap_minutes"]):
        reply = llm.parse(llm.chat(ep.ctx.cfg["llm"], SYSTEM, "\n".join(f"[{k}] {ls[i][2]}" for k, i in enumerate(idx))))
        for a in reply.get("ads", []):
            try:
                first, last, conf = int(a["first"]), int(a["last"]), float(a.get("confidence", 0.5))
            except (KeyError, TypeError, ValueError):
                continue
            for k in range(max(0, first), min(len(idx), last + 1)):
                if conf > verdict.get(idx[k], (0, ""))[0]:
                    verdict[idx[k]] = (conf, a.get("kind") if a.get("kind") in ("ad", "promo") else "ad")

    segments, run_ = [], []
    for i in sorted(verdict) + [None]:
        if run_ and (i is None or i != run_[-1] + 1):
            first, last = ls[run_[0]], ls[run_[-1]]
            best = max(run_, key=lambda j: verdict[j][0])
            segments.append({
                "start": first[0], "end": last[1], "confidence": round(verdict[best][0], 3), "kind": verdict[best][1],
                "evidence": f"\"{first[2][:80]}\" … \"{last[2][-80:]}\"",
            })
            run_ = []
        if i is not None:
            run_.append(i)
    if c["verify"]:
        checked = [(seg, verify(ep.ctx.cfg["llm"], ls, seg)) for seg in segments]
        segments = [{**seg, "confidence": conf} for seg, conf in checked if conf is not None]
    ep.save("votes/llm.json", {"duration": ls[-1][1] if ls else 0, "segments": segments})


STAGE = Stage(
    "detect_llm", run, inputs=("transcript.json",), outputs=("votes/llm.json",),
    config=lambda cfg, ep: {"detect": cfg["detect"]["llm"], "llm": cfg["llm"], "prompts": [SYSTEM, VERIFY]},
    enabled=lambda cfg: cfg["detect"]["llm"]["enabled"] and cfg["transcribe"]["enabled"],
    after=lambda cfg: llm.unload(cfg["llm"]),
)
