import copy
import hashlib
import json
import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "paths": {
        "data": "data",
        "labels": "labels",
        "models": "data/models",
        # what gets served: <feed>.xml and <feed>/<episode>.mp3. Point it at a NAS share to serve from elsewhere.
        "public": "data/public",
    },
    "serve": {"base_url": "http://localhost:8765"},
    "feeds": [],
    # The admin page keeps a list of shows too: its /admin/api/feeds URL. Shows from both lists are processed.
    "feeds_url": "",
    "download": {"user_agent": "unadcast/0.1", "timeout": 60},
    "prepare": {"silence_db": -35, "silence_min": 0.25},
    "detect": {
        "loudness": {"enabled": True, "weight": 0.8, "threshold_lu": 3.0, "min_length": 10.0, "max_length": 180.0},
        "voice": {"enabled": True, "weight": 0.6, "tau": 0.4, "cap": 240.0, "cap_share": 0.1, "min_length": 15.0, "merge_gap": 5.0, "voiced_lufs": -45.0},
        "llm": {"enabled": True, "weight": 1.0, "chunk_minutes": 8.0, "overlap_minutes": 1.0, "verify": True},
        "repeat": {"enabled": True, "weight": 1.0, "min_run": 8.0, "min_hits": 4, "max_bits": 10, "base": 0.2, "edge": 90.0, "theme_share": 0.6, "bumper": 25.0},
    },
    "transcribe": {"enabled": True, "model": "large-v3-turbo", "device": "cuda", "compute_type": "int8_float16", "language": "en"},
    "llm": {
        # "ollama", or "openai" for any OpenAI-compatible endpoint (llama.cpp, vLLM, LM Studio, hosted APIs)
        "backend": "ollama",
        "base_url": "http://ollama:11434",
        "model": "qwen3.5:9b-q4_K_M",
        # name of an environment variable holding the key, for the openai backend
        "api_key_env": "",
        "temperature": 0,
        "seed": 1,
        "num_ctx": 8192,
        "keep_alive": "2m",
        "timeout": 600,
    },
    "fuse": {
        "res": 0.25,
        "cut_threshold": 0.75,
        "mark_threshold": 0.45,
        "merge_gap": 4.0,
        "min_length": 8.0,
        "max_length": 600.0,
        "snap_window": 3.0,
        # a cut needs a vote from one of these, or from two detectors of any kind. Not llm: alone, even after its
        # verification pass, it cuts too much show (ARCHITECTURE.md, Evaluation).
        "strong": ["repeat"],
        # spans from these that touch a cut join it: ad breaks are contiguous
        "extend": ["voice"],
        "kinds": {"ad": 1.0, "promo": 1.0, "hint": 0.8, "intro": 0.0, "outro": 0.0},
    },
    "publish": {"mode": "mark", "unprocessed": "original", "crossfade": 0.08, "quality": 3},
}

# exclude: episodes whose title contains any of these (any case) are neither processed nor published
FEED_DEFAULTS = {"keep": 10, "mode": None, "exclude": []}


def merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def build(over, root):
    """Fill in defaults; relative paths resolve against `root`, the config file's directory."""
    # .env names the serving machine once, for the containers and bin/on-demand alike; config.toml still wins.
    server = os.environ.get("UNADCAST_SERVER", "").rstrip("/")
    base = merge(DEFAULTS, {"feeds_url": server + "/admin/api/feeds", "serve": {"base_url": server}}) if server else DEFAULTS
    cfg = merge(base, over)
    cfg["feeds"] = [feed(f, cfg) for f in cfg["feeds"]]
    cfg["feeds_local"] = list(cfg["feeds"])
    for k in ("data", "labels", "models", "public"):
        cfg["paths"][k] = str((Path(root) / cfg["paths"][k]).resolve())
    return cfg


def feed(f, cfg):
    """A feed entry with defaults filled in; only the keys the pipeline reads."""
    f = {**FEED_DEFAULTS, **{k: v for k, v in f.items() if k in ("slug", "url", "keep", "mode", "exclude")}}
    return {**f, "mode": f["mode"] or cfg["publish"]["mode"]}


def excluded(feed, title):
    return any(x.lower() in title.lower() for x in feed["exclude"])


def load(path=None):
    path = Path(path or os.environ.get("UNADCAST_CONFIG", "config.toml"))
    if not path.exists():
        return build({}, Path.cwd())
    return build(tomllib.loads(path.read_text()), path.parent)


def digest(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:12]
