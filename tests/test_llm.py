import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from unadcast import config, llm


class Fake(BaseHTTPRequestHandler):
    """Speaks just enough of Ollama's and OpenAI's chat APIs; replies with whatever the test put in `reply`."""

    reply = ""
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append((self.path, body, self.headers.get("Authorization")))
        if self.path == "/api/chat":
            out = {"message": {"role": "assistant", "content": Fake.reply}}
        else:
            out = {"choices": [{"message": {"role": "assistant", "content": Fake.reply}}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    Fake.seen.clear()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def cfg(base, **over):
    return {**config.DEFAULTS["llm"], "base_url": base, **over}


def test_ollama_backend(fake):
    Fake.reply = '{"ads": []}'
    assert llm.chat(cfg(fake, model="m"), "sys", "user") == '{"ads": []}'
    path, body, _ = Fake.seen[0]
    assert path == "/api/chat"
    assert body["model"] == "m" and body["think"] is False and body["stream"] is False
    assert body["options"]["temperature"] == 0 and body["options"]["num_ctx"] == config.DEFAULTS["llm"]["num_ctx"]
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_openai_backend(fake, monkeypatch):
    monkeypatch.setenv("SOME_KEY", "secret")
    Fake.reply = "ok"
    assert llm.chat(cfg(fake + "/v1", backend="openai", api_key_env="SOME_KEY"), "s", "u") == "ok"
    path, body, auth = Fake.seen[0]
    assert path == "/v1/chat/completions" and auth == "Bearer secret"
    assert body["temperature"] == 0 and "think" not in body


@pytest.mark.parametrize("text", [
    '{"ads": [{"first": 2, "last": 3}]}',
    'Sure!\n```json\n{"ads": [{"first": 2, "last": 3}]}\n```',
    '<think>first, {maybe} lines</think>{"ads": [{"first": 2, "last": 3}]}',
])
def test_json_is_dug_out_of_chatter(text):
    assert llm.parse(text) == {"ads": [{"first": 2, "last": 3}]}


def test_unparseable_reply_raises():
    with pytest.raises(ValueError):
        llm.parse("no json here")
