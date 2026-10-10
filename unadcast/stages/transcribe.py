"""faster-whisper on the GPU, word timestamps. The model stays loaded across a pass and is freed after it."""

import ctypes
import gc
import glob
import os
import wave

import numpy as np

from ..core import Stage

loaded = {}


def cuda_libs():
    """Load cuBLAS and cuDNN from the pip nvidia-* packages, so ctranslate2 finds them without LD_LIBRARY_PATH."""
    import nvidia.cublas
    import nvidia.cudnn

    for pkg in (nvidia.cublas, nvidia.cudnn):
        for lib in sorted(glob.glob(os.path.join(pkg.__path__[0], "lib", "lib*.so*"))):
            ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)


def model(c, models):
    key = (c["model"], c["device"], c["compute_type"])
    if key not in loaded:
        from faster_whisper import WhisperModel

        if c["device"] == "cuda":
            cuda_libs()
        loaded.clear()
        loaded[key] = WhisperModel(c["model"], device=c["device"], compute_type=c["compute_type"], download_root=str(models / "whisper"))
    return loaded[key]


def run(ep):
    c = ep.ctx.cfg["transcribe"]
    with wave.open(str(ep.path("audio.wav"))) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    # An array, not a path: faster-whisper's own decoding trips over newer PyAV releases.
    segments, info = model(c, ep.ctx.models).transcribe(
        x, language=c["language"] or None, vad_filter=True, word_timestamps=True, condition_on_previous_text=False
    )
    words = [[round(w.start, 2), round(w.end, 2), w.word] for s in segments for w in s.words]
    ep.save("transcript.json", {"language": info.language, "words": words})


def after(cfg):
    """Hand the GPU to the LLM stage."""
    loaded.clear()
    gc.collect()


STAGE = Stage(
    "transcribe", run, inputs=("audio.wav",), outputs=("transcript.json",),
    config=lambda cfg, ep: cfg["transcribe"], enabled=lambda cfg: cfg["transcribe"]["enabled"], after=after,
)
