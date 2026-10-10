"""Synthetic podcast audio: voiced syllables with pauses for the show, louder chords and drums for the ads."""

import subprocess
import wave

import numpy as np

SR = 22050


def rms(x, level):
    return x * level / np.sqrt(np.mean(x**2))


def speech(seconds, seed, level=0.05):
    rng = np.random.default_rng(seed)
    out = np.zeros(int(seconds * SR))
    t = 0.0
    while t < seconds:
        if rng.random() < 0.12:
            t += rng.uniform(0.4, 0.9)
            continue
        n = int(rng.uniform(0.12, 0.3) * SR)
        i = int(t * SR)
        if i + n > len(out):
            break
        tt = np.arange(n) / SR
        f0 = rng.uniform(100, 220)
        out[i : i + n] += sum(np.sin(2 * np.pi * f0 * h * tt) / h for h in range(1, 8)) * np.hanning(n)
        t += n / SR
    return rms(out, level)


def music(seconds, seed, level=0.15):
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    beat = int(0.5 * SR)
    for i in range(0, n, beat):
        tt = t[: min(beat, n - i)]
        for midi in rng.choice(range(48, 72), 3, replace=False):
            f = 440 * 2 ** ((midi - 69) / 12)
            out[i : i + len(tt)] += sum(np.sin(2 * np.pi * f * h * tt) / h**2 for h in range(1, 5))
        k = min(int(0.05 * SR), len(tt))
        out[i : i + k] += rng.normal(0, 2, k) * np.linspace(1, 0, k)
    return rms(out, level)


def silence(seconds):
    return np.zeros(int(seconds * SR))


def render(parts, path, bitrate="64k"):
    """Encode concatenated parts to an mp3 at `path`, via a temporary wav."""
    x = np.concatenate(parts)
    wav = path.with_suffix(".src.wav")
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav, "-c:a", "libmp3lame", "-b:a", bitrate, path], check=True)
    wav.unlink()
    return path
