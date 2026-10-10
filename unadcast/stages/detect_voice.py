"""Voices the episode barely uses are ad reads. Hosts and guests talk for many minutes; an announcer for one spot.

Speaker embeddings (WeSpeaker ResNet34, ONNX) every second over 2 s windows. A window's talk time is how many seconds of
the episode sound like the same voice. Runs of windows whose voice talks for less than `cap` seconds, or `cap_share` of the
episode's voiced time if that is less, are flagged.
"""

import hashlib
import os
import wave

import httpx
import numpy as np

from ..core import Stage
from ..spans import close, runs

URL = "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/voxceleb_resnet34_LM.onnx"
SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"
NAME = "wespeaker-resnet34-LM.onnx"
# 10 ms frames; a window is 200 frames (2 s) and starts every 100 (1 s).
WIN, HOP = 200, 100


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def model(models):
    """The model file, downloaded and verified on first use."""
    path = models / NAME
    if path.exists():
        return path
    models.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(NAME + ".part")
    with httpx.stream("GET", URL, follow_redirects=True, timeout=120) as r, open(tmp, "wb") as f:
        r.raise_for_status()
        for chunk in r.iter_bytes(1 << 20):
            f.write(chunk)
    if sha256(tmp) != SHA256:
        tmp.unlink()
        raise RuntimeError(f"{URL} does not match its pinned sha256")
    os.replace(tmp, path)
    return path


def mel_banks(n=80, nfft=512, sr=16000, lo=20.0):
    mel = lambda f: 1127 * np.log1p(f / 700)
    m = np.linspace(mel(lo), mel(sr / 2), n + 2)
    f = mel(np.arange(nfft // 2 + 1) * sr / nfft)
    return np.stack([np.clip(np.minimum((f - m[i]) / (m[i + 1] - m[i]), (m[i + 2] - f) / (m[i + 2] - m[i + 1])), 0, None) for i in range(n)])


BANKS = mel_banks()
# Kaldi's povey window, which WeSpeaker's features were computed with.
WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 399)) ** 0.85


def fbank(x, chunk=60000):
    """Kaldi-style 80-bin log mel filterbank, 25 ms frames every 10 ms, in chunks so hours of audio fit in memory."""
    n = 1 + (len(x) - 400) // 160
    out = np.empty((n, 80), np.float32)
    for f0 in range(0, n, chunk):
        f1 = min(n, f0 + chunk)
        idx = np.arange(400)[None, :] + 160 * np.arange(f0, f1)[:, None]
        fr = x[idx] * 32768.0
        fr -= fr.mean(axis=1, keepdims=True)
        fr = np.concatenate([fr[:, :1] * 0.03, fr[:, 1:] - 0.97 * fr[:, :-1]], axis=1) * WINDOW
        out[f0:f1] = np.log(np.maximum((np.abs(np.fft.rfft(fr, 512)) ** 2) @ BANKS.T, 1.19e-7))
    return out


def embed(F, path, batch=64):
    import onnxruntime as ort

    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    starts = range(0, len(F) - WIN + 1, HOP)
    out = []
    for k in range(0, len(starts), batch):
        feats = np.stack([F[s : s + WIN] - F[s : s + WIN].mean(0) for s in starts[k : k + batch]])
        out.append(sess.run(None, {"feats": feats})[0])
    E = np.concatenate(out) if out else np.zeros((0, 256), np.float32)
    return E / np.linalg.norm(E, axis=1, keepdims=True)


def talk(E, voiced, tau, chunk=2000):
    """Seconds of voiced audio that sound like each window's voice; infinite for unvoiced windows, which never flag."""
    V = E[voiced]
    out = np.full(len(E), np.inf)
    out[voiced] = np.concatenate([(V[k : k + chunk] @ V.T > tau).sum(1) for k in range(0, len(V), chunk)]) * HOP / 100
    return out


def segments(t, c):
    # A short episode has less of everyone: the cap shrinks with it, or its hosts would count as passing voices.
    cap = min(c["cap"], c["cap_share"] * np.isfinite(t).sum() * HOP / 100)
    out = []
    for a, b in close(runs(t < cap, HOP / 100), c["merge_gap"]):
        if b - a < c["min_length"]:
            continue
        i, j = int(a * 100 / HOP), int(b * 100 / HOP)
        inside = t[i:j]
        foreign = inside < cap
        out.append({
            "start": round(a + 0.5, 2),
            "end": round(b + 0.5, 2),
            "confidence": round(float(foreign.mean()), 3),
            "kind": "hint",
            "evidence": f"a voice heard for {float(np.median(inside[foreign])):.0f} s in the whole episode",
        })
    return out


def run(ep):
    c = ep.ctx.cfg["detect"]["voice"]
    with wave.open(str(ep.path("audio.wav"))) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    E = embed(fbank(x), model(ep.ctx.models))
    L = ep.load("loudness.json")
    m, step = np.array(L["m"]), L["step"]
    # Each window's own loudness, judged on its median, so a pause inside a sentence doesn't unvoice it.
    voiced = np.array([np.median(m[int(i / step) : int((i + 2) / step)]) > c["voiced_lufs"] for i in range(len(E))], bool)
    ep.save("votes/voice.json", {"duration": len(x) / 16000, "segments": segments(talk(E, voiced, c["tau"]), c)})


STAGE = Stage(
    "detect_voice", run, inputs=("audio.wav", "loudness.json"), outputs=("votes/voice.json",),
    config=lambda cfg, ep: cfg["detect"]["voice"], enabled=lambda cfg: cfg["detect"]["voice"]["enabled"],
)
