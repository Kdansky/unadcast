# unadcast

Ad-free versions of your podcasts, made at home. unadcast downloads new episodes, finds the ads, cuts them out (or
marks them as chapters you can skip), and publishes a copy of each feed that your podcast app subscribes to instead of
the original. Nothing leaves your network except the downloads themselves: no cloud services, no accounts.

How it works inside, and why: [ARCHITECTURE.md](ARCHITECTURE.md).

- [Everyday use](#everyday-use)
- [How it works, briefly](#how-it-works-briefly)
- [Setting it up](#setting-it-up)
- [Running it](#running-it)
- [Settings](#settings)
- [Checking and tuning results](#checking-and-tuning-results)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

## Everyday use

Everything day to day happens on the admin page, `http://<serving machine>:8765/admin/`; `bin/install` prints the
address at the end.

- **Add a show**: search by name, or paste its feed URL. It starts in **mark** mode.
- **Subscribe**: "Copy address" under a show gives its rewritten feed, `http://<serving machine>:8765/<show>.xml`.
- **Mark or cut**: in mark mode the audio is untouched and ads become chapters ("Ad", and "Ad?" where unsure), so you
  can check by ear and skip with one tap. Switch a show to **cut** once its chapters look right.
- **Episodes**: how many of the newest episodes to process, 10 by default, newest first. Older ones stay in the feed with
  the publisher's original audio.
- **Waiting episodes**: `bin/pending install`, on a Linux desktop, sets up a notification for when new episodes wait
  for a pass, with buttons to start one or open the admin page. It checks every 15 minutes and announces each episode
  once; `bin/pending uninstall` removes it. It reads `http://<serving machine>:8765/admin/pending.xml`, a feed of the
  episodes a pass would process that aren't done yet, failed ones included. The serving machine reads the shows' feeds
  itself for it, so it knows while the processing machine is off. A cloud feed reader can't reach it on your LAN.
- **Process now**: starts a pass on the processing machine within half a minute, if it's on; otherwise as soon as it is. Adding a show,
  or changing one's mode, episodes or skipped titles, presses it for you. Without clicking, a pass also runs when the
  last one is more than 12 hours old. It counts the episodes waiting, and is greyed out when there are none, or when the
  NAS is unreachable, as a pass couldn't publish anything then.
- **Stop**: stops a running pass within about 10 seconds, and cancels a "Process now" that hasn't started yet. The step
  it was on is simply redone next time; nothing half-finished is kept.
- **GPU**: the status says whether anything of unadcast is on the processing machine's graphics card right now: Ollama (the language
  model, most of the memory) and a pass (Whisper). Before gaming, Stop frees it.
- **Pause 3 h / 6 h**: stops what runs, like Stop, and starts no pass until then; **Resume** ends it early. A "Process
  now" clicked meanwhile waits and runs when the pause is over.
- **Busy GPU**: if, when a pass is due, something else already uses half of the GPU's memory (a game), the
  processing machine waits an hour and checks again. The page says so. Games started *during* a pass aren't noticed: pause or stop then.
- **Skip episodes**: per show, phrases whose episodes you never want, comma-separated, any case. "It Could Happen
  Here" on Behind the Bastards drops that show's episodes, the Weeklies included. They are neither processed nor
  counted towards the episode number, and they disappear from your feed on the next pass.
- **Status**, at the top, is what is true now ("as of" the time it was last fetched, every 15 s): whether the
  processing PC is on, what it is doing ("Transcribing …, episode 3 of 12, this step since 21:04") or when the last
  pass ended, a pause, a postponement or a waiting request, the GPU, and the command that shows the same on the
  processing machine, `bin/now`. Below it, the pass's last 40 lines of output, sent every 10 s while it runs and
  greyed out once it ends. Per show and episode: done (how much was cut), waiting, or failed.
- **Messages**, pinned to the bottom, are the answers to what you clicked, with the time. Each says what it is waiting
  for and, once that has happened, what came of it ("Pass requested → pass started 21:04", "cut mode → applied by the
  pass that ended 21:30"); those, and anything older than ten minutes, are greyed out as history. × clears them.
- **Detectors**: switch the four ways of finding ads on or off for all shows (see below for what each does). Switching
  the transcript off also frees the GPU. Saving starts a pass, which redoes only what the change affects.

### Switching Podcast Addict without losing played episodes

The rewritten feed keeps each episode's identity (its GUID) and only changes where the audio comes from. So change the
address of the subscription you already have rather than subscribing anew:

1. Long-press the podcast → **Custom Settings** → **Edit podcast URL**.
2. Paste the address from the admin page's "Copy address".
3. Refresh. Played episodes stay played; processed ones play the ad-free file (the app may offer to download them again).

Try it with one show first. Any app that lets you edit a subscription's URL works the same way.

## How it works, briefly

unadcast has two parts, which can share one machine or not:

```
 processing (NVIDIA GPU, may be off)                   serving (ideally always on)
 watcher ── "anything to do?" ───────────────────────►  admin page: shows, "Process now", status
 when yes: download, analyse, cut ──► files ◄────────── nginx: feeds and audio for your phone
                                      (on the processing machine, or a NAS share both reach)
```

For each new episode, several detectors look for ads independently:

- **repeat**: audio that also occurs in other episodes of any show. Ad networks insert the same spots everywhere.
- **voice**: voices the episode barely uses. Hosts talk for an hour; an ad's announcer for thirty seconds.
- **loudness**: stretches mastered louder or quieter than the show.
- **llm**: Whisper transcribes, and a local language model reads the transcript for sponsor reads, promo codes and
  promos, including ads the hosts read themselves.

Where they agree, the ad is cut; where only one is unsure, it becomes an "Ad?" chapter. An intro or theme that every
episode shares is recognised and kept. On six hand-checked episodes this cut all of the ad time, and lost under a minute
of actual show per episode on average (under two at worst), mostly closing credits next to an ad block.

The processing machine does the heavy work only when asked or due; between passes it runs one tiny container. If
serving runs elsewhere, your feeds keep working while the processing machine is off; new episodes wait for it.

## Setting it up

You need a Linux machine with an NVIDIA GPU (about 10 GB of memory), its driver, Docker with Compose v2, and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
That is enough. Optionally:

- **an always-on machine for serving**, such as a Raspberry Pi with Docker, so feeds work while the GPU machine is off.
  `bin/install` sets it up over ssh, so key login to it should work (`ssh-copy-id <host>`).
- **a NAS share** for the published files. Required when serving runs on another machine, since both must reach the
  files: any SMB share, such as a Synology shared folder with a user that may write to it (and, ideally, a second user
  that may only read, for serving). On Synology, set the SMB maximum protocol to SMB3 (Control Panel → File Services →
  SMB → Advanced Settings).

Then, on the GPU machine:

```sh
git clone https://github.com/Kdansky/unadcast.git && cd unadcast
cp .env.example .env          # and fill it in: which machine does what, and the NAS if you use one
bin/install
```

`.env` is the whole installation. The common shapes:

| | `PROCESS` | `SERVE_HOST` | `NAS_*` |
|---|---|---|---|
| one machine does everything | `yes` | empty | empty |
| one machine, files on a NAS | `yes` | empty | set |
| GPU machine + always-on server + NAS | `yes` | the server's ssh host | set |
| a machine that only serves | `no` | empty | set |

`bin/install` checks everything it can first (the settings, Docker, the GPU, the NAS, ssh to the server) and stops
with a message saying what to fix. Then it builds the processing image (about 3.5 GB), downloads the language model,
checks that the published files are writable, starts serving here or on the server, starts the watcher, and waits until
the watcher has checked in with the admin page. Rerun it after changing `.env`, `config.toml` or the code; it is safe
to run again at any time.

`config.toml` holds processing settings; `bin/install` makes it from `config.example.toml`, whose defaults are fine to
start with.

**Keep the admin page off the internet.** It has no login: anyone who reaches its port can change your shows and stop
processing, and adding a show makes the server fetch whatever address it is given. A LAN or a VPN such as Tailscale is
fine.

## Running it

Normally nothing to do: the watcher starts passes when you click "Process now", or every 12 hours while the processing
machine is on. Docker starts everything again after a reboot.

```sh
docker compose up -d                   # start this machine's part again, after stopping it
docker compose stop watcher            # stop it; no passes until started again
docker compose logs -f watcher         # the watcher's own log: which passes it started
docker compose ps                      # what is running: just the watcher, or a pass and ollama too
```

What runs when:

| | between passes | during a pass |
|---|---|---|
| processing | `watcher` (a shell loop, a few MB) | + `ollama` (GPU) and a `cli` pass container |
| serving | `web` and `files` (nginx), `admin`, `remount` (a shell loop) | same |

A pass takes a few minutes per hour-long new episode (the first pass for a new show does `keep` episodes at once);
passes with nothing new take seconds. To change how often passes run without clicking, set `UNADCAST_EVERY` (hours,
`0` for never) in `.env`. `UNADCAST_GPU_BUSY` (default 50) is the percentage of GPU memory in use by other programs at
which a pass is postponed by an hour. Then `bin/install` again.

The command line, for looking closer:

```sh
bin/now                                # is the GPU in use, is a pass running, which step, then its live output
bin/stop                               # free the GPU: stop any running pass and ollama, right now
bin/unadcast status                    # every show's episodes: progress, failures, what was found
bin/unadcast report <episode>          # each cut and mark with its time, scores and evidence
bin/play <episode> <n>                 # listen to span #n of a report, with 5 s either side (needs ffplay)
bin/unadcast run                       # a pass right now, by hand
bin/unadcast stage <name> --force      # redo one step for every episode, e.g. after changing code
```

Episode ids can be shortened to any unique prefix.

## Settings

`config.toml` holds only what differs from the defaults; [`config.example.toml`](config.example.toml) shows the common
ones with explanations, and ARCHITECTURE.md lists every key. Changing a setting reruns exactly the steps it affects on
the next pass. The most useful:

- `[publish] mode`: the default for shows that don't set their own (`mark`).
- `[publish] unprocessed`: `original` keeps unprocessed episodes in the feed with the publisher's audio; `hide`
  leaves them out.
- Detectors are switched on the admin page. `[detect.repeat]`, `[detect.voice]`, `[detect.loudness]`, `[detect.llm]`
  `enabled = false` in `config.toml` does the same; the admin page's switches win when both are set.
- `[llm]`: another Ollama model, or `backend = "openai"` with `base_url`, `model` and `api_key_env` for any
  OpenAI-compatible server.

## Checking and tuning results

- **By ear**: shows in mark mode get chapters; listen around them. `bin/play` plays a span from the command line.
- **Why something was cut**: `bin/unadcast report <episode>` lists every span with each detector's score and evidence
  ("also in Episode 212", "a voice heard for 31 s in the whole episode", the transcript lines the LLM objected to).
- **Measured**: `labels/<show>/<release date> <title>.json` holds hand-marked ad times, and the episode's `id`; `bin/unadcast label <episode>` starts one. `bin/unadcast eval` scores the current
  settings against them (ad cut, marked, missed; show cut, marked), in seconds, without reprocessing anything.
  `--only voice` scores one detector alone. Label a few episodes of a new show before trusting cut mode with it.

## Troubleshooting

- **`bin/install` stops with an error**: it says what to fix; fix it and run it again.
- **The page says the processing PC hasn't checked in**: is the watcher running (`docker compose ps`)? Is
  `UNADCAST_SERVER` in `.env` the serving machine's address? `docker compose logs watcher` shows its checks.
- **"Process now" does nothing**: the processing machine is off or the watcher stopped; the request waits and runs
  when it's back.
- **An episode failed**: the admin page and `bin/unadcast status` show the first line of the error; the full error is
  in `docker compose logs watcher`. The next pass retries it.
- **The NAS mount fails**: `journalctl -k | grep CIFS`. "Dialect not supported by server" means the NAS's SMB maximum
  is below 3; raise it (on Synology: DSM, File Services, SMB, Advanced) or set `NAS_SMB=2.0` in `.env`, then
  `bin/install`.
- **The admin page says "Storage unreachable"**: the NAS is off or unreachable. Feeds and audio are offline until it is
  back, and follow within a minute; the admin page keeps working.
- **A show plays the original audio**: its episodes aren't processed yet, or are older than its episode count.
- **Something was cut that shouldn't have been**: switch the show to mark, look at `bin/unadcast report`, and consider
  labelling the episode so `eval` measures any change you make.
- **Moved the repo**: run `bin/install` again from the new folder; the watcher remembers where it was started.

## Development

```sh
uv run pytest                          # on the host
bin/test                               # in the processing image; both must pass (the two ffmpegs differ)
```

Code layout, the pass, every stage, the formats, and how to add a detector are in [ARCHITECTURE.md](ARCHITECTURE.md).

## License

MIT, see [LICENSE](LICENSE). By [Kajetan Abt](https://github.com/Kdansky).
