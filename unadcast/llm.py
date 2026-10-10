"""Chat with a local or remote model: Ollama's own API, or anything OpenAI-compatible. No SDK, just httpx."""

import json
import os
import re

import httpx


def chat(c, system, user):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    base = c["base_url"].rstrip("/")
    if c["backend"] == "ollama":
        # Thinking costs minutes per chunk and buys nothing here. (Ollama 0.30 also drops `format` when thinking is off,
        # which is why the reply is parsed rather than schema-constrained.)
        body = {
            "model": c["model"], "messages": messages, "stream": False, "think": False, "keep_alive": c["keep_alive"],
            "options": {"temperature": c["temperature"], "seed": c["seed"], "num_ctx": c["num_ctx"]},
        }
        r = httpx.post(f"{base}/api/chat", json=body, timeout=c["timeout"])
        r.raise_for_status()
        return r.json()["message"]["content"]
    key = os.environ.get(c["api_key_env"], "") if c["api_key_env"] else ""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    body = {"model": c["model"], "messages": messages, "temperature": c["temperature"], "seed": c["seed"]}
    r = httpx.post(f"{base}/chat/completions", json=body, headers=headers, timeout=c["timeout"])
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def unload(c):
    """Free the GPU for Whisper on the next pass instead of waiting out keep_alive."""
    if c["backend"] == "ollama":
        httpx.post(f"{c['base_url'].rstrip('/')}/api/generate", json={"model": c["model"], "keep_alive": 0}, timeout=60)


def parse(text):
    """The JSON object in a reply, past any reasoning, code fences or pleasantries around it."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start = text.find("{")
    while start >= 0:
        try:
            return json.JSONDecoder().raw_decode(text[start:])[0]
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
    raise ValueError(f"no JSON object in reply: {text[:200]!r}")
