import json
import re
import shutil
import subprocess

import pytest

from unadcast import config
from unadcast.stages import detect_llm, transcribe

from tests.conftest import episode


def words(text, start=0.0, step=0.5, gaps=()):
    """Word triples, one every `step` seconds, with extra silence before the word indices in `gaps`."""
    out, t = [], start
    for i, w in enumerate(text.split()):
        if i in gaps:
            t += 2.0
        out.append([t, t + step * 0.8, " " + w])
        t += step
    return out


def test_lines_break_at_sentences_gaps_and_length():
    ws = words("So that is the news. Support for the show comes from Acme. Use code POD", gaps={12})
    got = [text for _, _, text in detect_llm.lines(ws, max_words=40)]
    assert got == ["So that is the news.", "Support for the show comes from Acme.", "Use code POD"]
    long = words(" ".join(["word"] * 50))
    assert [len(t.split()) for _, _, t in detect_llm.lines(long, max_words=20)] == [20, 20, 10]


def test_chunks_overlap():
    ls = [(t, t + 5, "x") for t in range(0, 1200, 10)]
    cs = detect_llm.chunks(ls, minutes=8, overlap=1)
    assert [(c[0], c[-1]) for c in cs] == [(0, 47), (42, 89), (84, 119)]


def fake_transcript(ep):
    ws = words("Welcome back to the show. We talked a lot about laptops today and it was good.")
    ws += words("Support for the show comes from Acme. Go to acme dot com and use code POD for twenty percent off.", start=40)
    ws += words("All right we are back. Laptops again.", start=80)
    ep.save("transcript.json", {"language": "en", "words": ws})


def test_stage_maps_line_ranges_to_time(prepared, monkeypatch):
    ep = episode(prepared, "ep3")
    fake_transcript(ep)
    prompts = []

    def chat(c, system, user):
        if system == detect_llm.VERIFY:
            return '{"ad": true, "confidence": 1}'
        prompts.append(user)
        n = {re.sub(r"^\[(\d+)\] ", "", l): int(l[1:l.index("]")]) for l in user.splitlines()}
        first = n["Support for the show comes from Acme."]
        return f'Here you go: {{"ads": [{{"first": {first}, "last": {first + 1}, "kind": "ad", "confidence": 0.9}}, {{"first": 99, "last": 120, "confidence": 1}}]}}'

    monkeypatch.setattr(detect_llm.llm, "chat", chat)
    detect_llm.STAGE.execute(ep)
    segs = ep.load("votes/llm.json")["segments"]
    assert len(prompts) == 1 and "[0] Welcome back to the show." in prompts[0]
    assert [(s["start"], round(s["end"], 1), s["kind"], s["confidence"]) for s in segs] == [(40.0, 49.9, "ad", 0.9)]
    assert "Support for the show comes from Acme." in segs[0]["evidence"]


def test_overlapping_chunks_merge(prepared, monkeypatch):
    ep = episode(prepared, "ep3")
    ws = []
    for k in range(120):
        ws += words(f"Sentence number {k} is here.", start=k * 10)
    ep.save("transcript.json", {"language": "en", "words": ws})

    def chat(c, system, user):
        if system == detect_llm.VERIFY:
            return '{"ad": true, "confidence": 1}'
        # Every chunk that sees lines 45-50 calls them an ad, with different confidence.
        local = [i for i, l in enumerate(user.splitlines()) if any(f"number {n} " in l for n in range(45, 51))]
        conf = 0.6 if "number 0 " in user else 0.8
        return json.dumps({"ads": [{"first": local[0], "last": local[-1], "kind": "promo", "confidence": conf}]} if local else {"ads": []})

    monkeypatch.setattr(detect_llm.llm, "chat", chat)
    detect_llm.STAGE.execute(ep)
    segs = ep.load("votes/llm.json")["segments"]
    assert [(s["start"], s["kind"], s["confidence"]) for s in segs] == [(450.0, "promo", 0.8)]


def test_transcribe_real_speech(prepared, tmp_path):
    """Needs ffmpeg's flite voice (Debian's build has it; run bin/test). The tiny model mishears flite's robot voice, so
    this checks mechanics only; accuracy is what `unadcast eval` on real episodes measures."""
    if "flite" not in subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout:
        pytest.skip("ffmpeg without flite")
    ep = episode(prepared, "ep1")
    text = "Support for the show comes from Acme. Go to acme dot com today."
    wav = ep.path("audio.wav")
    keep = tmp_path / "audio.wav"
    shutil.copy(wav, keep)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"flite=text='{text}'", "-ar", "16000", "-ac", "1", wav], check=True)
    c = prepared.cfg
    old = dict(c["transcribe"])
    c["transcribe"].update(model="tiny.en", device="cpu", compute_type="int8")
    c["paths"]["models"] = str(tmp_path / "models")
    try:
        transcribe.STAGE.execute(ep)
        transcribe.STAGE.after(c)
    finally:
        c["transcribe"].update(old)
        shutil.copy(keep, wav)
    t = ep.load("transcript.json")
    assert t["language"] == "en" and len(t["words"]) >= 5
    assert all(a <= b for a, b, _ in t["words"]) and t["words"][-1][1] < 10


def test_verification_drops_rejected_spans(prepared, monkeypatch):
    ep = episode(prepared, "ep3")
    fake_transcript(ep)
    asked = []

    def chat(c, system, user):
        if system == detect_llm.VERIFY:
            asked.append(user)
            # The show's own discussion was flagged by the first pass; the check throws it out.
            return '{"ad": false, "confidence": 0.9}' if "laptops" in user.split("PASSAGE:")[1].split("After")[0].lower() else '{"ad": true, "confidence": 0.7}'
        # Lines: [2]-[3] are the Acme read, [5] is "Laptops again." with [4] between, so they stay two spans.
        return '{"ads": [{"first": 2, "last": 3, "kind": "ad", "confidence": 0.95}, {"first": 5, "last": 5, "kind": "ad", "confidence": 0.9}]}'

    monkeypatch.setattr(detect_llm.llm, "chat", chat)
    detect_llm.STAGE.execute(ep)
    segs = ep.load("votes/llm.json")["segments"]
    assert len(asked) == 2
    # The kept span's confidence is the lower of the two passes.
    assert [(s["start"], s["confidence"]) for s in segs] == [(40.0, 0.7)]
    # Context goes along, marked as such.
    assert "Before (context" in asked[0] and "All right we are back." in asked[0]

    prepared.cfg["detect"]["llm"]["verify"] = False
    try:
        detect_llm.STAGE.execute(ep)
    finally:
        prepared.cfg["detect"]["llm"]["verify"] = True
    assert len(ep.load("votes/llm.json")["segments"]) == 2
