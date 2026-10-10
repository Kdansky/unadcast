"""bin/on-demand: started every 30 s by the watcher container; starts containers only when a pass is asked for or due."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "bin" / "on-demand"


@pytest.fixture
def root(tmp_path, media, server):
    (media / "admin" / "api").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.toml").write_text(f'feeds_url = "{server}/admin/api/feeds"\n\n[serve]\nbase_url = "x"\n')
    # A stand-in for docker that records what it was asked to do. A pass takes $PASS_SECONDS; ps finds one container;
    # $GPU_USED of 10000 MiB of GPU memory are in use.
    fake = tmp_path / "docker"
    fake.write_text(f'''#!/bin/sh
echo "$@" >> {tmp_path}/calls
case "$*" in
    *nvidia-smi*) echo "${{GPU_USED:-1000}}, 10000" ;;
    "compose run"*) exec sleep "${{PASS_SECONDS:-0}}" ;;
    ps*) echo c0ffee ;;
esac
''')
    fake.chmod(0o755)
    return tmp_path


def request(media, t, stop=0, pause=0):
    (media / "admin" / "api" / "run").write_text(json.dumps({"requested": t, "stop": stop, "pause": pause}))


def run(root, every="12", gpu=1000):
    env = {**os.environ, "UNADCAST_ROOT": str(root), "DOCKER": str(root / "docker"), "UNADCAST_EVERY": every, "UNADCAST_POLL": "0.1",
           "GPU_USED": str(gpu)}
    out = subprocess.run(["sh", SCRIPT], env=env, capture_output=True, text=True, check=True)
    calls = actions(root)
    (root / "calls").unlink(missing_ok=True)
    return out.stdout, calls


def actions(root):
    """What the script had docker do, without its look-ups: the GPU users for every check-in, the GPU's memory."""
    calls = (root / "calls").read_text().splitlines() if (root / "calls").exists() else []
    return [c for c in calls if not c.startswith("ps --filter label=") and "nvidia-smi" not in c]


def test_requested_pass_starts_and_stops_containers(root, media):
    (root / "data" / "watcher").mkdir(parents=True)
    (root / "data" / "watcher" / "last-pass").write_text(str(int(time.time())))
    request(media, 0)
    assert run(root)[1] == []

    request(media, time.time() + 0.5)
    out, calls = run(root)
    assert "requested" in out
    assert calls == ["compose --profile llm up -d ollama", "compose run --rm -T cli unadcast run", "compose --profile llm stop ollama"]
    # The same request again is not a new one.
    assert run(root)[1] == []


def test_scheduled_pass_when_the_last_is_old(root, media):
    request(media, 0)
    # No pass ever: one is due now.
    out, calls = run(root)
    assert "scheduled" in out and len(calls) == 3
    assert run(root)[1] == []
    (root / "data" / "watcher" / "last-pass").write_text(str(int(time.time()) - 13 * 3600))
    assert len(run(root)[1]) == 3
    (root / "data" / "watcher" / "last-pass").write_text(str(int(time.time()) - 13 * 3600))
    assert run(root, every="0")[1] == []


def test_unreachable_pi_is_not_a_request(root, media):
    (root / "config.toml").write_text('feeds_url = "http://127.0.0.1:9/admin/api/feeds"\n')
    (root / "data" / "watcher").mkdir(parents=True)
    (root / "data" / "watcher" / "last-pass").write_text(str(int(time.time())))
    assert run(root)[1] == []


def test_stop_cancels_a_queued_request(root, media):
    (root / "data" / "watcher").mkdir(parents=True)
    (root / "data" / "watcher" / "last-pass").write_text(str(int(time.time())))
    request(media, time.time() - 5, stop=time.time())
    assert not any(c.startswith("compose run") or c.endswith("up -d ollama") for c in run(root)[1])


def test_stop_during_a_pass(root, media):
    (root / "data" / "watcher").mkdir(parents=True)
    (root / "data" / "watcher" / "last-pass").write_text(str(int(time.time())))
    request(media, time.time() + 0.5)
    env = {**os.environ, "UNADCAST_ROOT": str(root), "DOCKER": str(root / "docker"), "PASS_SECONDS": "30", "UNADCAST_POLL": "0.2"}
    started = time.time()
    p = subprocess.Popen(["sh", SCRIPT], env=env, stdout=subprocess.PIPE, text=True)
    time.sleep(1.5)
    request(media, 0, stop=time.time() + 1)
    out, _ = p.communicate(timeout=20)
    assert time.time() - started < 15 and "stop" in out
    calls = actions(root)
    assert "stop c0ffee" in calls and calls[-1] == "compose --profile llm stop ollama"


def test_stop_between_passes_frees_the_gpu(root, media):
    (root / "data" / "watcher").mkdir(parents=True)
    (root / "data" / "watcher" / "last-pass").write_text(str(int(time.time())))
    request(media, 0, stop=time.time())
    out, calls = run(root)
    assert "stop" in out and calls[-1] == "compose --profile llm --profile always stop ollama worker"
    # Once per press.
    assert run(root)[1] == []


def test_pause_holds_passes_until_it_ends(root, media):
    request(media, time.time() + 0.5, pause=time.time() + 100)
    # Due by schedule and requested, but paused.
    assert run(root)[1] == []
    request(media, time.time() + 0.5, pause=time.time() - 1)
    out, calls = run(root)
    assert "requested" in out and "compose run --rm -T cli unadcast run" in calls


def test_busy_gpu_postpones_an_hour(root, media):
    request(media, time.time() + 0.5)
    out, calls = run(root, gpu=6000)
    assert "60%" in out and "postponed" in out and calls == []
    postponed = int((root / "data" / "watcher" / "postponed").read_text())
    assert abs(postponed - (time.time() + 3600)) < 10
    # Still postponed though the game has ended; the request is kept.
    assert run(root)[1] == []
    (root / "data" / "watcher" / "postponed").write_text(str(int(time.time()) - 1))
    out, calls = run(root)
    assert "requested" in out and len(calls) == 3
    # Below the threshold, a pass just runs.
    (root / "data" / "watcher" / "last-pass").write_text("0")
    assert len(run(root, gpu=4900)[1]) == 3
