import json
import os
import re
import subprocess
import wave

import numpy as np

from ..core import Stage

STEP = 0.1
FRAME = re.compile(r"\bt:\s*(\S+)\s+TARGET:\S+ LUFS\s+M:\s*(\S+)\s+S:\s*(\S+)")
SILENCE = re.compile(r"silence_(start|end): (\S+)")


def run(ep):
    """One decode of the original gives the analysis wav, loudness curves and silences."""
    c = ep.ctx.cfg["prepare"]
    tmp = ep.path("audio.wav.part")
    af = f"ebur128=framelog=verbose,silencedetect=n={c['silence_db']}dB:d={c['silence_min']},aresample=16000"
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-v", "verbose", "-y", "-i", ep.path("original"), "-af", af, "-ac", "1"]
    log = subprocess.run([*cmd, "-c:a", "pcm_s16le", "-f", "wav", tmp], capture_output=True, text=True, check=True).stderr
    os.replace(tmp, ep.path("audio.wav"))
    with wave.open(str(ep.path("audio.wav"))) as w:
        duration = w.getnframes() / w.getframerate()

    frames = np.array([[float(x) for x in m] for m in FRAME.findall(log)]).reshape(-1, 3)
    t, m, s = frames.T
    # ebur128 reports each value at the end of its window (0.4 s momentary, 3 s short-term) and nan for digital silence.
    # Re-centre both on a common grid so index i describes time i * STEP, with silence at the floor.
    grid = np.arange(int(duration / STEP)) * STEP
    curve = lambda v, window: np.round(np.interp(grid, t - window / 2, np.fmax(v, -70)), 1).tolist()
    ep.save("loudness.json", {"step": STEP, "m": curve(m, 0.4), "s": curve(s, 3.0)})

    silences, start = [], None
    for kind, x in SILENCE.findall(log):
        if kind == "start":
            start = float(x)
        elif start is not None:
            silences.append([round(start, 3), round(float(x), 3)])
            start = None
    if start is not None:
        silences.append([round(start, 3), round(duration, 3)])
    ep.save("silences.json", silences)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name:format=duration", "-of", "json", ep.path("original")],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(probe.stdout)
    ep.save("meta.json", {"codec": info["streams"][0]["codec_name"], "duration": float(info["format"]["duration"])})


STAGE = Stage(
    "prepare", run, inputs=("original",), outputs=("audio.wav", "loudness.json", "silences.json", "meta.json"),
    config=lambda cfg, ep: cfg["prepare"],
)
