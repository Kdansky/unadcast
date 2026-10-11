# unadcast: architecture

unadcast subscribes to podcast feeds, finds the ads in each episode, cuts them out or marks them as chapters, and
publishes a rewritten feed that a podcast app subscribes to instead of the original. Everything runs at home: Whisper and
a small local LLM for words, signal processing for the rest. No cloud APIs; a bigger or hosted model is a config change.

This document is for changing the system. To use it, read [README.md](README.md).

Contents: [System](#system) · [Principles](#principles) · [Code and data layout](#code-and-data-layout) ·
[A pass](#a-pass) · [Stages](#stages) · [Formats](#formats) · [Serving and the admin page](#serving-and-the-admin-page) ·
[Configuration](#configuration) · [Evaluation](#evaluation) · [Decisions](#decisions-and-things-tried) ·
[Extending](#extending) · [Dependencies](#dependencies-and-hardware)

## System

Two roles, and a place for the published files. They can share one machine or be spread over three:

```
 processing (GPU, may be off)               files                             serving (always on, ideally)
 ────────────────────────────               ─────                             ────────────────────────────
 watcher container, every 30 s:             data/public, or an SMB share:     profile `serve`:
   bin/on-demand ── asks the admin page ─────────────────────────────────►     admin   /admin/  show list,
     "pass wanted?"                           <feed>.xml                                     "Process now",
   if so, for one pass:                       <feed>/<episode>.mp3                           status page
     docker compose: ollama + cli ──writes──► status.json  ◄──reads, read-only──  nginx  :8765    feeds + audio
     data/: downloads, work files,                                                             ▲
     database, models                                                                          │
                                                                                         podcast app
```

- **Processing** needs an NVIDIA GPU. Between passes only the `watcher` container runs: a few MB of RAM, a shell loop
  that every 30 s asks the admin page whether a pass is wanted and checks in (host name, repo folder, busy or not, what
  holds the GPU). Only then are ollama and a pass container started, and ollama stopped again afterwards. Docker
  restarts the watcher at boot; nothing is installed into the host's init system. Working data (downloads, analysis
  files, the SQLite database, models) stays on this machine.
- **The files** are `data/public` on the processing machine, or an SMB share (`compose.nas.yml`) when serving runs
  elsewhere. A NAS then only stores files and runs nothing of ours.
- **Serving** is nginx with the published files and the admin page, where shows are added and passes requested. It
  needs about 100 MB of RAM and never the 3.5 GB processing image, so a Raspberry Pi is plenty.

Below, "the PC" is the processing machine and "the server" the serving one, even when they are the same box.

Processing never accepts connections: it asks the admin page, and writes the files. If it is off, serving keeps
offering what was last published, and requests wait for it. Each machine's roles are compose profiles (`process`,
`serve`) in its `.env`; `bin/install` derives them, and everything else machine-specific, from the one `.env` the user
fills in, and sets up a serving machine elsewhere over ssh.

## Principles

- **Stages are separate steps over files.** Each stage reads files from an episode's work dir and writes its own. Any
  stage can be rerun alone (`unadcast stage <name> --episode <id>`), inspected by hand, or its output deleted to force
  a redo.
- **Only what changed reruns.** Each stage stamps what it was made from (config and inputs). Tuning `[fuse]` reruns
  `fuse` and `cut` in seconds; it never re-transcribes.
- **Detectors vote, they don't decide.** Each detector writes time spans with a confidence and evidence. Only `fuse`
  decides what is cut, marked or kept.
- **A detector that fails or is switched off just doesn't vote.** The pipeline degrades; it never blocks.
- **Cutting show is worse than missing an ad.** A missed ad costs a skip; lost show is lost. Uncertain spans become
  "Ad?" chapters instead of cuts, and every new show starts in mark mode.
- **One decode, one encode.** The original is decoded once for analysis and encoded once for output.
- **One GPU, one model at a time.** Whisper and the LLM don't fit together in 12 GB, so a pass runs each stage for all
  episodes before the next stage (stage-major), and frees each model after its stage.

## Code and data layout

```
unadcast/
  cli.py         unadcast run | loop | stage | status | report | spans | eval | find
  config.py      DEFAULTS for every setting; config.toml overrides; feed entry normalisation
  core.py        Ctx (config + db + paths), Episode (a work dir), Stage (run + staleness stamps)
  db.py          SQLite schema: feeds, episodes, runs (failures), prints + hashes (fingerprint index)
  runner.py      one pass: feed list sync, poll, stages, prune, publish, status.json; the 30-minute loop
  stages/        poll download prepare detect_loudness detect_voice detect_repeat (+ fingerprint)
                 transcribe detect_llm fuse cut publish; __init__.py holds the execution order
  llm.py         Ollama and OpenAI-compatible chat over httpx; tolerant JSON parsing
  evaluate.py    plans against hand labels, second by second
  spans.py       boolean masks to time spans, span merging
  admin.py       the admin page and API; standalone (starlette, httpx, stdlib)
bin/
  watch          the watcher container's loop: bin/on-demand every 30 s
  now            what a pass is doing right now, then its live output
  stop           stop running passes and ollama
  on-demand      one check: start ollama + one pass + stop, when requested or due; checks in with the admin page
  unadcast       the CLI inside the container
  install        validates .env, then sets up processing here and serving here or over ssh; rerun for any change
  play           listen to a detected span on the host
  pending        desktop notification when episodes wait for a pass, from the admin page's pending.xml; a systemd user timer
  test           the test suite inside the image
serve/           nginx config, admin image
docker-compose.yml   every service, by profile: process, serve, llm, always, cli, test
compose.nas.yml, Dockerfile
labels/          hand-made ad labels for evaluation
tests/           synthetic episodes (speech-like show, music ads) from a local HTTP feed; fakes for LLM and docker
```

On disk, under `paths.data` (default `data/`):

```
unadcast.db                          feeds, episodes, failures, fingerprint index
remote-feeds.json, remote-settings.json   last answers from the admin page (used when it is unreachable)
watcher/handled, watcher/last-pass   bin/on-demand's memory: last request served, last pass started
pending-announced                    bin/pending's memory: episodes already announced
models/                              Whisper (huggingface/), speaker model (wespeaker-resnet34-LM.onnx)
work/<feed>/feed.xml                 the original feed as last fetched; publish rewrites a copy of it
work/<feed>/<episode-id>/
  original                           the downloaded enclosure
  audio.wav  meta.json               16 kHz mono for analysis; codec and duration
  loudness.json  silences.json
  transcript.json                    words with timestamps
  votes/<detector>.json              each detector's spans
  plan.json  report.txt              fuse's decision, and the human-readable why
  chapters.txt  out.mp3  out.json    cut's output
  stamps/<stage>.json                what each finished stage was made from
public/                              (or the NAS share) <feed>.xml, <feed>/<episode-id>.mp3, status.json
```

`<episode-id>` is the first 12 hex digits of `sha1(feed slug + "\n" + item GUID)`: stable across runs and machines.
Work dirs of episodes that fall out of a feed's `keep` window are deleted at the end of a pass; their fingerprints stay
in the database, because older episodes are what newer ones are matched against.

## A pass

### What starts it

The `watcher` service in `docker-compose.yml` (`docker:27.5.1-cli`, Alpine with the docker CLI and compose) runs
`bin/watch`, which runs `bin/on-demand` every 30 s. That is POSIX sh with busybox `wget`, one request per check. It starts
a pass when either:

- **"Process now" was pressed.** It reads `/admin/api/run` on the server (`{"requested": <unix time>}`, under
  `UNADCAST_SERVER`, else beside `feeds_url`) and compares with `data/watcher/handled`. Adding a show on the admin page presses it too.
- **The last pass is older than `UNADCAST_EVERY` hours** (default 12; `0` turns scheduled passes off), so new episodes
  arrive without clicking whenever the PC is on.

A wanted pass waits, without being marked handled, so it starts once the wait is over:

- **while paused**: `pause` in `/admin/api/run` is a time in the future ("Pause 3 h" / "6 h" on the page; pausing also
  sets `stop`, so whatever runs is stopped and earlier requests are dropped).
- **while postponed**: before starting, the watcher reads the GPU's memory with `nvidia-smi` through the processing
  image (it doesn't run on the watcher's Alpine; 0.6 s). At `UNADCAST_GPU_BUSY` percent or more (default 50; the
  desktop alone uses under 40) something else, typically a game, has the card, and the watcher writes
  `data/watcher/postponed` an hour ahead. Only the start is guarded: a game started during a pass shares the GPU with it
  until the pass ends or is stopped.

Every check also posts `{"host", "dir", "busy", "gpu", "postponed"}` to `/admin/api/pc`, so the admin page shows whether the PC is
on, busy, where its repo lives, and which of unadcast's GPU users are up (`gpu`: the compose services among ollama, cli
and worker that `docker ps` finds running; during a pass it is posted every poll). It records the request and the start time before a pass, so a pass that fails is not retried
every 30 s. Then:

```
docker compose --profile llm up -d ollama       # the LLM server, GPU
docker compose run --rm -T cli unadcast run     # one pass in a throwaway container
docker compose --profile llm stop ollama        # always, also after a failure
```

The pass runs in the background of `bin/on-demand`, which meanwhile reads `/admin/api/run` every `UNADCAST_POLL`
seconds (default 10): a `stop` time later than the pass's start runs `bin/stop` (`docker stop` on every pass container,
then ollama and the resident worker). Between passes, a `stop` newer than the last one handled (`data/watcher/stopped`)
runs `bin/stop` too, so "Stop" also frees the GPU of an ollama or pass started by hand. A step's stamp is removed when the step starts, so an interrupted step is redone on the next pass. A
"Process now" older than the last "Stop" is treated as cancelled. The watcher's next check comes only after a pass ends,
and passes take a lock file, so two never overlap.

How the watcher can start containers: it mounts the docker socket (with the socket's group, `DOCKER_GID`, which `bin/install` looks up)
and runs as your user. Docker resolves bind mounts on the host, so the watcher mounts the repo at its real path
(`${PWD}`, captured when `bin/install` started it) and runs compose there; `HOME`, `UID` and `GID` are passed
through for the paths and users compose fills into the other services. Move the repo, and run `bin/install`
again from the new place.

Alternatives: `bin/unadcast run` by hand, or `docker compose --profile always up -d` for a resident worker and ollama
(`unadcast loop`: a pass every 30 minutes, plus polling for "Process now").

### What a pass does

`runner.run`, under a lock file so two passes never overlap:

1. **Show list and detector switches.** `config.toml`'s `[[feeds]]` plus the server's list from `feeds_url`, and the server's
   `/settings` applied over `[detect.*] enabled` (switching the LLM off switches transcription off with it). Both are
   cached in `data/remote-*.json`: if the server is unreachable the last answers stand, so an outage never looks like
   "every show was removed". The switches change config hashes, so the stamps rerun exactly what they affect.
2. **`status.json`** with the published files says `running: true` and `started`, taken before the sync: a change made on the page
   before that time is in this pass, one made after waits for the next.
3. **Poll** every feed (conditional GET), and record its items. The newest `keep` episodes per show whose titles match
   none of its `exclude` phrases are pending, newest first.
4. **Stages**, in the order of `stages.STAGES`, each across all pending episodes before the next: download, prepare,
   detect_loudness, detect_voice, fingerprint, detect_repeat, transcribe, detect_llm, fuse, cut. Before each step,
   `status.json` gets `current`: stage, show, episode, step k of the stage's n, since when. A stage runs for an
   episode when its inputs exist and its stamp is stale. After a stage that ran, its `after` hook frees its model
   (Whisper; the LLM is told to unload).
5. **Prune** work dirs of episodes outside the window, **publish** every feed, write the final `status.json`.

A failing stage is recorded in the `runs` table (shown by `unadcast status` and on the admin page) and the pass moves
on: a failed detector just doesn't vote; a failed `prepare` leaves later stages without input until the next pass.

### Staleness

A finished stage writes `stamps/<stage>.json`:

```json
{"version": 1, "config": "9f2c41a0be11", "inputs": {"loudness.json": "48113:1760087411203948123"}}
```

`config` hashes only what the stage reads (`Stage.config`); for `detect_llm` that includes its prompts, so editing a
prompt reruns it. `inputs` holds size and mtime of every input file and every match of the stage's optional globs
(fuse reads `votes/*.json`). The stage is stale when the stamp is missing or differs, or an output is missing. A stage
deletes its stamp before running and every file is written via a `.part` name, so a crash never leaves something that
looks finished. Code changes do not invalidate stamps: bump the stage's `version`, or run
`unadcast stage <name> --force`.

## Stages

### poll

`httpx` with `ETag`/`If-Modified-Since`. The response is parsed with lxml before the stored `feed.xml` is replaced, so
a broken response never clobbers a good feed. Items are read by `poll.items`, the same function publish uses, so both
derive the same GUID (the `<guid>`, else the enclosure URL) and therefore the same episode id.

### download

Streams the enclosure to `original.part`, then renames. It has no config, so changing the user agent never re-fetches.

### prepare

One ffmpeg run over the original produces everything later stages read: `audio.wav` (16 kHz mono), `loudness.json`
(`ebur128` momentary 0.4 s and short-term 3 s loudness every 0.1 s, re-centred so index `i` means `i × 0.1 s`; ebur128's
`nan` over digital silence becomes the −70 LUFS floor), `silences.json` (`silencedetect`, −35 dB, 0.25 s: the candidate
cut points), and `meta.json` (codec and duration from ffprobe).

### detect_repeat: audio heard before

The strongest signal. Ad networks insert the same spots everywhere, so a stretch heard in another episode, of this show
or any other, is almost always an ad.

- `fingerprint` runs `fpcalc -raw` on `audio.wav`: chromaprint, one 32-bit item per 4096/3 samples at 11025 Hz
  (≈0.124 s). Each item summarises ~16 frames, so its content is centred ~8 items after its index. Items go into
  `hashes(hash, episode, pos)`, the whole print into `prints`.
- `detect_repeat` looks this episode's items up in the index. Exact hits grouped by `(other episode, offset)` with at
  least `min_hits` hits propose alignments; items frequent within the episode (silence, held tones) are left out, or
  they would align everything with everything. Each alignment is verified item by item: bit errors, averaged over 9
  items, below `max_bits` of 32. Unrelated audio averages ~16 bits; the same ad at another frame phase 2–8, with only
  ~10% of items identical, which is why exact hits only propose and bit errors decide. Runs of at least `min_run`
  seconds count. An episode is matched against itself too, so an ad played twice in one episode is found.
- Classification. A theme recurs in nearly every episode of its feed; ads rotate, and network ads appear in other feeds.
  A repeat is `intro`/`outro` when it sits within `edge` seconds of the start/end in every match **and** recurs in at
  least `theme_share` of the feed's other episodes. Ad time before it (a pre-roll) is discounted from its position, or a
  pre-roll would push the intro out of the window. A short (`bumper` s) repeat found only within its own feed is a
  `bumper`: the show's own stings between segments, which carry no weight. Everything else is `ad`, confidence
  `1 − base^n` for `n` distinct episodes.

A runner pass indexes all its episodes before matching any. Earlier episodes are not re-scored when later ones arrive
(`unadcast stage detect_repeat --force` does that).

### detect_voice: voices the episode barely uses

Hosts and guests talk for many minutes; the announcer of a spot for thirty seconds, once.

1. Kaldi-style 80-bin log-mel filterbank in numpy, in chunks so four-hour episodes fit in memory.
2. Speaker embeddings for 2 s windows every second: WeSpeaker ResNet34 (VoxCeleb, CC-BY-4.0), a 25 MB ONNX file run by
   onnxruntime on the CPU (~45 s per hour of audio), downloaded on first use and checked against a pinned sha256.
3. Each voiced window's *talk time*: seconds of the episode with cosine similarity above `tau` to it. One chunked
   matrix product; no clustering.
4. Windows whose talk time is under `cap` (or `cap_share` of the voiced time, for short episodes) are foreign. Runs of
   them (gaps under `merge_gap` bridged, at least `min_length`) become `hint` votes.

Alone it flags 98% of ad time but also theme songs, archival clips and guests' sound bites, so it is not trusted to cut
by itself. Its job in fusion is to extend a confirmed cut across the rest of an ad break.

### detect_loudness: mastered differently

The show's level is the median short-term loudness over audible frames; stretches whose 5 s median deviates by more
than `threshold_lu` for `min_length`–`max_length` seconds are flagged, edges refined on the momentary curve. A weak
signal (it can't tell a loud theme from an ad, and Vox masters its ads to the show's level); its use is corroboration.

### transcribe

faster-whisper `large-v3-turbo`, `int8_float16` on the GPU (~3 GB VRAM, ~50× real time on an RTX 4070), with voice
activity detection, word timestamps and `condition_on_previous_text=False` (which stops Whisper's repetition loops).
Output: `{"language": "en", "words": [[start, end, word], ...]}`. The model stays loaded across a pass and is freed
after. cuBLAS/cuDNN come from the `nvidia-*` pip packages and are preloaded with ctypes, so nothing needs
`LD_LIBRARY_PATH`. Audio goes in as an array, because faster-whisper's own decoding breaks on newer PyAV releases.

### detect_llm: reading the transcript

The only detector that catches host-read ads, which sound exactly like the show.

1. Words become lines (sentence ends, pauses over 1 s, 40 words at most).
2. Lines are cut into 8-minute chunks overlapping by 1 minute (~2.5k tokens).
3. Each chunk goes to the model as numbered lines, never timestamps (small models are bad at times, good at "lines 52
   to 71"), with a prompt defining advertising and its look-alikes, asking for JSON only:
   `{"ads": [{"first": 52, "last": 71, "kind": "ad", "confidence": 0.98}]}`.
4. Each line takes the most confident verdict of any chunk; runs of flagged lines become spans.
5. **Verification** (`verify`): each span goes back alone, with ~400 characters of context either side, and one narrow
   question: an inserted ad (sponsor read, promo for another show, network tag), or the show's own talk, self-promotion,
   credits, a guest's plug? Rejected spans are dropped. The first pass is eager; the focused question is not. It cut the
   LLM's wrongly flagged show time from 938 s to 239 s while keeping 92% of the ad time it had found.

The default model is `qwen3.5:9b-q4_K_M` on Ollama (~8 s per chunk; a minute per hour of audio), with thinking off.
Ollama 0.30.7 ignores the `format` JSON schema when thinking is off, so replies are parsed tolerantly (`llm.parse` finds
the first JSON object past any chatter). `llm.py` speaks Ollama's `/api/chat` and the OpenAI-compatible
`/chat/completions`, so a bigger local or a hosted model is an `[llm]` change.

### fuse

1. A timeline at `res` (0.25 s). Per detector, `s_d(t)` is the highest `weight × kinds[kind] × confidence` of its spans
   covering `t`. Combined with noisy-OR: `p(t) = 1 − Π(1 − s_d(t))`. `intro`/`outro` votes set `p(t) = 0`.
2. **Cut** where `p ≥ cut_threshold`, the run is at most `max_length`, and it is corroborated: a `strong` detector
   (`repeat`) voted, or two detectors of any kind did. With the defaults loudness alone reaches 0.64, a voice hint 0.48,
   an LLM vote up to 1.0: alone, none of them cuts.
3. **Extend**: a span from an `extend` detector (`voice`) that touches a cut joins it, because ad breaks are
   contiguous. That is how a Vergecast break gets cut whole: a recurring house promo (repeat) opens it, unique announcer
   spots (voice) fill it.
4. **Mark** whatever else has `p ≥ mark_threshold` (gaps under `merge_gap` closed, at least `min_length`).
5. **Snap** each edge to the nearest silence midpoint within `snap_window`; distances into the span count double, so a
   second of ad left beats a word cut. With no silence near, the quietest momentary-loudness frame, if clearly quiet.
6. `keep` is the complement of the cuts; keeps under 1 s are absorbed. `plan.json` and `report.txt` are written.

### cut

One ffmpeg run from the original. Cut mode: `asplit` into one `atrim` per kept span, joined with 80 ms `acrossfade`s,
encoded with LAME VBR; marks are remapped through the shortened timeline into "Ad?" chapters. Mark mode: nothing cut,
mp3 audio stream-copied, cuts become "Ad" and marks "Ad?" chapters. Gaps between labelled chapters are "Show" chapters
(ID3 CHAP). Cover art (`-map 0:v? -c:v copy`) and tags survive. `out.json` records mode, duration and seconds removed.

### publish

Edits the stored original `feed.xml` in place (lxml), which keeps every namespace and tag. Processed items get the new
enclosure url/length/type, `itunes:duration`, and a line prepended to the description ("unadcast: removed 1:02 in 2
cuts, 1 marked."); their `guid` stays, so apps keep their played state. Unprocessed items keep the publisher's audio
(`unprocessed = "original"`) or are left out (`"hide"`). Items matching the show's `exclude` phrases are always left out. The channel title gets " (unadcast)", and `atom:link
rel="self"` and `itunes:new-feed-url` are removed, or apps may move the subscription back to the original.

Into `paths.public`: mp3s first (copied only when size or mtime changed, via a temp name), then the feed, then files the
feed no longer names are deleted. The public tree is static and self-contained.

## Formats

**Votes**, `votes/<detector>.json`, the same for every detector:

```json
{"duration": 3612.4, "segments": [
  {"start": 812.3, "end": 874.0, "confidence": 0.8, "kind": "ad", "evidence": "also in Episode 212"},
  {"start": 0.0, "end": 14.2, "confidence": 0.96, "kind": "intro", "evidence": "also in Episode 211, Episode 212"}]}
```

`confidence` is the belief that the span is unwanted. `kind`: `ad`, `promo`, `hint` (evidence that isn't "ad" by
itself), `intro`/`outro` (protect their span), `bumper` (no weight). `evidence` is for humans, in `report.txt`.

**Plan**, `plan.json`:

```json
{"duration": 3612.4, "keep": [[0.0, 812.1], [874.6, 3612.4]], "removed": 62.5, "marks": [],
 "cuts": [{"start": 812.1, "end": 874.6, "p": 0.94, "action": "cut", "kind": "ad",
           "scores": {"repeat": 0.8, "loudness": 0.6}, "evidence": ["repeat: also in Episode 212", "..."]}]}
```

**Status**, `status.json` with the published files, for the admin page: `{"started", "updated", "running", "feeds": {slug: {"title", "url",
"mode", "keep", "episodes": [{"id", "title", "published", "state": "done|waiting|failed", "error", "cuts", "marks",
"removed"}]}}, "current": {"stage", "feed", "title", "step", "of", "since"}}`, `current` only while a pass runs. The page
trusts `running` only while the PC's check-in also says busy: a killed pass can't update its own status.

The page has two places for text. **Status** is rebuilt from the API every 15 s and holds only what is true now, with
times. **Messages** is the only place responses to clicks appear: each entry has its time and, optionally, a test run
against every later refresh that says what came of it (a request is answered by a pass with `started` after it, a
change by such a pass ending, Stop by a check-in after it with nothing on the GPU). Answered and old entries are dimmed.

## Serving and the admin page

The `serve` profile in `docker-compose.yml`, on the server (`bin/install` copies the files it needs there over ssh
when that is another machine, and writes its `.env`):

- **web** (`nginx:1.27.5-alpine`, `serve/nginx.conf`) on `UNADCAST_PORT` (8765) serves the published files read-only:
  `application/rss+xml` feeds with `no-cache`, `audio/mpeg` with Range requests (podcast apps seek and resume with
  them), `.part` files and a Synology `#recycle` folder hidden. `/` redirects to `/admin/`.
- **admin** (`unadcast/admin.py` in `python:3.12-alpine`, `serve/admin.Dockerfile`, starlette + uvicorn + httpx)
  behind nginx at `/admin/`. The show list and the rest of its state are JSON files in the `admin` volume.

| Endpoint | |
|---|---|
| `GET /admin/` | the page |
| `GET /admin/pending.xml` | RSS of the episodes a pass would process (newest `keep`, minus `exclude`) that `status.json` doesn't call done; reads the shows' feeds itself, cached 15 min, as the PC may be off. Items link to `/admin/`, so nginx passes the `Host` with its port |
| `GET /admin/api/feeds` | the show list; the PC reads it as `feeds_url` |
| `POST /admin/api/feeds` `{"url"}` | add: the feed is fetched to validate it and take its title; slug from title; starts in mark mode, 10 episodes; requests a pass |
| `PATCH /admin/api/feeds/<slug>` `{"mode", "keep", "exclude"}` | `mark`/`cut`, 1–50, title phrases to skip |
| `DELETE /admin/api/feeds/<slug>` | stop processing it (its published files stay) |
| `GET /admin/api/search?q=` | Apple's podcast directory |
| `GET /admin/api/status` | `status.json` from the published files |
| `GET, POST /admin/api/run` | `{"requested": <time>, "stop": <time>}`; POST is "Process now" |
| `POST /admin/api/stop` | "Stop": sets `stop`; the PC stops its pass and ollama within a poll and skips older requests |
| `POST /admin/api/pause` | `{"hours": 0–24}`: sets `stop` and `pause` (now + hours); no pass starts before then. `0` resumes |
| `GET, POST /admin/api/pc` | the PC's last check-in: `{"host", "dir", "busy", "gpu", "postponed", "seen"}` |
| `GET, PUT /admin/api/settings` | `{"detect": {"repeat", "voice", "loudness", "llm": bool}}`; saving requests a pass |

**There is no login.** Anyone who reaches the port can change the show list, stop or pause processing, and post
check-ins; adding a show makes the server fetch the URL given. Keep it on a LAN or a VPN such as Tailscale, never on
the internet. Being plain HTTP, it has no `navigator.clipboard` (browsers allow that on HTTPS and localhost only), so
its copy buttons fall back to a hidden text field and `execCommand("copy")`.

**The published files** are `data/public` when the PC also serves. Otherwise they are on an SMB share both reach,
mounted as a Docker CIFS volume (`compose.nas.yml`, which `bin/install` adds to `COMPOSE_FILE` when `NAS_ADDR` is set),
with credentials only in each machine's gitignored `.env`; the server may use a read-only user (`NAS_SERVE_USER`). No
fstab, no root. A Docker volume keeps the options it was created with, so `bin/install` removes and recreates it. The
database and work files stay on the PC: SQLite over a network filesystem invites locking trouble.

## Configuration

`config.toml` (gitignored; template `config.example.toml`) overrides `DEFAULTS` in `unadcast/config.py`. Relative paths
resolve against the config file's directory.

| Key | Default | |
|---|---|---|
| `feeds_url` | `UNADCAST_SERVER/admin/api/feeds`, else `""` | the server's show list; shows from there are added to `[[feeds]]` |
| `[[feeds]]` `slug`, `url`, `keep`, `mode`, `exclude` | `keep = 10`, `mode` from `[publish]`, `exclude = []` | shows listed locally; `exclude`: title phrases (any case) whose episodes are skipped and left out of the feed |
| `paths.data` / `labels` / `models` / `public` | `data`, `labels`, `data/models`, `data/public` | |
| `serve.base_url` | `UNADCAST_SERVER`, else `http://localhost:8765` | where apps fetch feeds and audio: the server |
| `download.user_agent` / `timeout` | `unadcast/0.1` / 60 | |
| `prepare.silence_db` / `silence_min` | −35 / 0.25 | silence detection for cut points |
| `detect.repeat` | `weight 1.0, min_run 8, min_hits 4, max_bits 10, base 0.2, edge 90, theme_share 0.6, bumper 25` | |
| `detect.voice` | `weight 0.6, tau 0.4, cap 240, cap_share 0.1, min_length 15, merge_gap 5, voiced_lufs -45` | |
| `detect.loudness` | `weight 0.8, threshold_lu 3, min_length 10, max_length 180` | |
| `detect.llm` | `weight 1.0, chunk_minutes 8, overlap_minutes 1, verify true` | |
| `detect.<name>.enabled` | `true` | switch a detector off; the admin page's switches override it |
| `transcribe` | `enabled, model large-v3-turbo, device cuda, compute_type int8_float16, language en` | off also turns `detect_llm` off |
| `llm` | `backend ollama, base_url http://ollama:11434, model qwen3.5:9b-q4_K_M, api_key_env "", temperature 0, seed 1, num_ctx 8192, keep_alive 2m, timeout 600` | `backend = "openai"` for any OpenAI-compatible server |
| `fuse` | `res 0.25, cut_threshold 0.75, mark_threshold 0.45, merge_gap 4, min_length 8, max_length 600, snap_window 3, strong ["repeat"], extend ["voice"]` | |
| `fuse.kinds` | `ad 1, promo 1, hint 0.8, intro 0, outro 0` | weight per vote kind |
| `publish.mode` | `mark` | default for shows that don't set one |
| `publish.unprocessed` | `original` | or `hide` |
| `publish.crossfade` / `quality` | 0.08 s / 3 (LAME VBR) | |

`.env` (gitignored; template `.env.example`) describes the installation: `PROCESS`, `SERVE_HOST`, `UNADCAST_SERVER`,
`UNADCAST_PORT`, `NAS_*`, `UNADCAST_EVERY` (hours between scheduled passes), `UNADCAST_GPU_BUSY` (percent of GPU memory
in other use at which a pass is postponed by an hour). `bin/install` validates it and appends what follows from it for
compose: `COMPOSE_PROFILES`, `COMPOSE_FILE`, `NAS_IP` (the CIFS client wants an address), the derived `UNADCAST_SERVER`,
`DOCKER_GID`, `UID`, `GID`. `UNADCAST_CONFIG` names another config file.

## Evaluation

`labels/<feed>/<episode-id>.json` holds hand-marked ad spans, made by transcribing an episode and reading the text
around detected spans and around ad cues:

```json
{"title": "Part Two: Heinrich Schliemann", "ads": [[61, 237], [1318, 1659], [2541, 2872], [3496, null]]}
```

`null` runs to the end. Network tags ("This is an iHeart Podcast") belong to the ad block; the show's own intro and
credits don't. `unadcast eval` re-fuses every labelled episode in memory with the current config and prints, per
episode and in total, ad time cut / marked / missed and show time cut / marked; `--only <detector>` fuses one
detector's votes alone. Six episodes are labelled: three Vergecast, one Behind the Bastards, two Better Offline,
3706 s of ads.

| Stage of development | ad cut | missed | show cut | show marked |
|---|---|---|---|---|
| loudness + repeat | 85% | 14% | 30 s | |
| + voice, fuse extends along foreign voices | 96% | 4% | 172 s | 21 s |
| + transcript and LLM, LLM strong, no verification | 100% | 0% | 808 s | 232 s |
| + LLM verification pass | 100% | 0% | 394 s | 0 s |
| + LLM needs a second detector (current) | 100% | 0% | 286 s | 108 s |
| LLM alone (verified) | 71% | 29% | 335 s | |
| loudness alone | 0% (14% marked) | 86% | 0 s | |

What the numbers mean in practice:

- **iHeart shows** (Behind the Bastards, Better Offline) insert whole network ad blocks that recur across episodes and
  feeds; repeat detection finds them with edges within a second. 99–100% from the acoustic detectors alone.
- **Vergecast** mixes Vox house promos (recurring: found by repeat) with paid spots unique to each episode, mastered to
  the show's level. Announcer-read ones are found by voice extension, host-read ones only by the LLM.
- **Show time still cut** is mostly the shows' own credits next to post-rolls, a "90 Seconds on The Verge" news
  segment read by a staffer heard nowhere else and placed next to a promo, and intro stings next to ad blocks.

## Decisions and things tried

- **Acoustic first.** The target shows carry inserted, separately produced ads. Repeat and voice detection catch those
  with no GPU, and they remain the backbone.
- **Theme vs pre-roll.** Position alone misclassified repeated pre- and post-roll ads as intro/outro; feed share fixed
  it. Merging matches that merely touch swallowed an intro into the pre-roll before it; matches must now overlap by 2 s.
- **Cut only where `p` itself is high.** Cutting a whole span because its peak was high dragged a loudness fringe into
  Better Offline's intro. The fringe is now a mark.
- **Speaker talk time instead of clustering or diarization.** Clustering a four-hour episode needs ~100M pairwise
  distances; pyannote is heavy and gated. Talk time answers the only question asked, in one matrix product, with
  better precision than clustering on the labels.
- **No music detector.** The remaining errors were about who speaks and what is said, not music.
- **No download diffing.** Re-downloading a Vergecast episode with other user agents changed only two rotating promos,
  both already found by repeat.
- **LLM thinking off, verification on, not strong.** Thinking cost 40 s per toy prompt for worse answers. The second,
  focused pass halved the LLM's false cuts; requiring corroboration removed another 108 s of cut show.
- **ffmpeg 7.1** (Debian, in the image) aborts on a filter graph with copied cover art that ffmpeg 9 (the host) accepts,
  and a cover-art test passed on the host with a broken fixture. Tests run in both since.
- **SMB 3.** A Synology NAS may be limited to SMB 2; CIFS then reports "Dialect not supported by server" in the kernel
  log (`journalctl -k | grep CIFS`). Raise it in DSM, or set `NAS_SMB`.
- **On demand, not resident.** A permanent worker and Ollama held memory and GPU on a PC that is also a desktop. A
  systemd timer was the first replacement; a watcher container won because it needs no units in the host's systemd,
  only Docker, which starts at boot anyway. The price of on-demand: passes only happen while the PC is on.
- **Two compose versions.** The watcher's compose (v2.33) and the host's (v5.6) compute container configs differently,
  so a container created by one is recreated by the other. Let the watcher start ollama and passes; `bin/unadcast` from
  the host is for inspection and the occasional manual pass.

Compared with [Podly](https://github.com/podly-pure-podcasts/podly_pure_podcasts), the reference for the transcript →
LLM → cut → rewritten-feed loop: local by default, several independent detectors fused instead of LLM-only
classification, cross-episode fingerprinting, per-stage staleness so tuning is cheap, and a mark tier for doubt.

## Extending

**A new detector** is a module in `unadcast/stages/` with a `STAGE`:

```python
STAGE = Stage(
    "detect_music", run, inputs=("audio.wav",), outputs=("votes/music.json",),
    config=lambda cfg, ep: cfg["detect"]["music"], enabled=lambda cfg: cfg["detect"]["music"]["enabled"],
)
```

`run(ep)` reads the episode's files and saves `votes/music.json` in the vote format. Add defaults under
`DEFAULTS["detect"]["music"]` (at least `enabled` and `weight`), insert the stage in `stages.STAGES` before `fuse`, and
it votes. Decide in `[fuse]` whether it is `strong` or may `extend` cuts, and measure with `unadcast eval`
(`--only music` for it alone) before trusting it. Add tests with the synthetic episodes in `tests/conftest.py`.

**A new model or LLM**: `[llm]` for the classifier (any Ollama model, or an OpenAI-compatible endpoint), `[transcribe]
model` for Whisper. The prompts are `SYSTEM` and `VERIFY` in `stages/detect_llm.py`; changing them reruns the stage.

**Re-running after a code change**: `bin/unadcast stage <name> --force` reruns one stage for every pending episode;
later stages follow on the next pass because their inputs changed.

## Dependencies and hardware

Python 3.12 (`.python-version`; ML wheels lag new Python releases). The image is `python:3.12-slim` with Debian's
ffmpeg (which has `flite`, used by one test) and `libchromaprint-tools`; uv installs the venv with a cache mount, about
3.5 GB in total, most of it the CUDA libraries.

- `httpx`, `lxml`, `numpy`, `scipy`, `starlette`, `uvicorn`
- `onnxruntime` (speaker model), `faster-whisper` + `nvidia-cublas-cu12` + `nvidia-cudnn-cu12` (transcription)
- binaries: ffmpeg, ffprobe, fpcalc; Ollama as its own container (`ollama/ollama:0.30.7`, reusing `~/.ollama/models`)
- server: `nginx:1.27.5-alpine`, `python:3.12.11-alpine3.22` with starlette, uvicorn, httpx

Tests: `uv run pytest` on the host and `bin/test` in the image; both must pass, because the two ffmpegs differ.

Hardware, as developed: RTX 4070 (12 GB). Whisper large-v3-turbo int8 takes ~3 GB, the 9B LLM at Q4 ~7 GB; they fit one at a
time, which is why passes are stage-major and models are freed after their stage. A typical hour-long episode takes a
few minutes end to end; a four-hour compilation about fifteen.
