# DayReplay

> **A diary that writes itself.** Everything you say, the sounds around you, and the
> apps you use — on one timeline you can search and replay. Fully offline. Your day
> never leaves your PC.

[中文说明](README.zh-CN.md) · [Roadmap](#roadmap) · [Contributing](#contributing)

![DayReplay daily timeline](docs/screenshots/timeline.png)

It brings your **microphone**, your **screen**, and your **apps** together into one
aligned timeline — a true, complete record of your day. Missed something? Play it
back anytime, **as clear as the moment it happened**.

Ask *"what did I do last Tuesday?"* — get the answer in seconds, with links back to
the original audio and screen shots.

<!-- Social preview image for sharing -->
<meta name="og:image" content="docs/screenshots/social_preview.png">

---

## Why another tracker?

[ActivityWatch](https://github.com/ActivityWatch/activitywatch) writes down window
titles. Rewind and Recall want a Mac or a cloud account. The part they all miss:
**sound**.

| | ActivityWatch | Rewind / Recall | DayReplay |
|---|---|---|---|
| What you **said** (searchable text) | — | paid plan | ✅ |
| Sounds around you (footsteps, doors, water, a baby crying) | — | — | ✅ |
| What was on your screen | window titles only | ✅ (Mac / cloud) | ✅ optional, local |
| Works with no internet at all | ✅ | ❌ | ✅ core installer; models from HuggingFace (one-time) |
| Price | free | subscription | free, open source |

## What it catches

This is the part trackers miss. DayReplay listens to **everything**, not just
app windows — and then it tells you what it means.

| What happened | What DayReplay writes down |
|---|---|
| Two people arguing in another room | *"Heard a heated argument about 8 m away, 43 seconds. Maybe don't take a shower right now if you'd rather not walk into that."* |
| A chair scraping in the next room | *"Chair moved — someone sat down. Your screen shows `voice_timeline.py` — this looks like a focused dev session."* |
| Water running for three minutes | *"Running water, 2 min 47 s — logged as a water event."* |
| Two hours of typing, no talking | *"Silent working block, 105 min. Would have forgotten this hour existed."* |
| A phone call about the release | *"Speaker B, 6 min: the installer pulls the models automatically, ~1.3 GB."* |

Every one of those is a real row in the timeline: a time range, the sound
evidence, the screen evidence, and a plain-English note — all clickable back
to the original audio and screenshots.

## What you get

- **Your words, written down** — it listens for speech and turns it into text
  (Mandarin + English), running on a normal CPU, no GPU needed
- **Sounds around you** — footsteps, a chair moving, running water, a door, a baby
  crying. Two-stage filtering keeps quiet hours actually quiet
- **Who said it** — speaker tagging by voice, so the timeline shows who was talking
- **Your screen, optional** — pairs with local screen capture; every entry can link
  to the screen proof of that moment
- **One timeline for the whole day** — a daily page that weaves voice, screen, and
  events together; opens in any browser, nothing to install
- **Ask in plain words** — "did I ever talk about the Vietnam trip?" searches weeks
  of audio and screen history
- **USB-stick install** — copy to a stick, double-click on any Windows 10/11
  64-bit PC. No admin rights, no Python, no internet needed after the
  one-time model download (~330 MB core, models ~1.3 GB)
- **Self-updating** — version numbers, small incremental updates, a test after every
  update, and automatic rollback if anything goes wrong
- **One-click stop** — the mic is yours: tray icon, kill switch, everything in
  plain sight

## Quick start

1. Download `DayReplay-<version>-lite-win64.zip` from
   [Releases](../../releases) — **~330 MB**, unzip it anywhere.
2. On the target PC, double-click **`install.bat`** (one-click install, no
   admin rights, no Python needed).
3. The installer will offer to **download the AI models** from HuggingFace
   (free, open-source, ~1.3 GB) — or skip and run `download_models.py` later.
   This is the only time you need internet.
4. Pick your microphone, set up autostart, run a self-test, then open your
   first daily timeline.

First timeline appears within minutes. After the model download, DayReplay
works fully offline forever.

> **Prefer zero internet?** A "full" package (~3.4 GB) with the models
> included is available on request — copy it on a USB stick and never
> connect.

First timeline appears within minutes.

<details>
<summary><b>Run from source</b> (developers)</summary>

```text
Windows 10/11 64-bit, Python 3.11
git clone https://github.com/sedlee0811-44/DayReplay.git
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
python voice_daemon.py        # recorder + speech-to-text + screen watcher
python vdigest.py             # build today's timeline page
```

Note: docs under `docs/` are in Chinese (English translation welcome — a great
first contribution).

</details>

## How it works

```text
 microphone ──► voice detect ──► speech-to-text (SenseVoice) ──► text (zh/en)
     │               │                        │
     │               ├─ DSP gate ──► sound classifier (AudioSet, 527 kinds) ──► events
     │               └─ voice print (CAM++) ──► who is speaking
 screen (optional) ──► OCR / key frames ──┐
                                          ▼
                      SQLite (all local) ──► daily HTML timeline
                                          ▼
                      plain-language search ──► transcript pages
```

- Speech-to-text: [FunASR / SenseVoice-Small](https://github.com/FunAudioLLM/SenseVoice)
  (Chinese + English, real time on CPU)
- Sound events: [AST AudioSet](https://github.com/YuanGongND/ast) classifier over a
  hand-tuned DSP pre-filter
- Speaker tagging: CAM++ voice embeddings
- Everything else: plain Python + SQLite, no framework

## Privacy & consent

- **All data stays on your machine.** No telemetry, no cloud calls, no accounts.
  The only network access ever performed is *you* checking for updates from a
  source you configured.
- The microphone records **your own device, with your knowledge** — a tray icon,
  a running indicator, and a one-click stop are built in.
- Recording other people without their consent is against the law in many places.
  This tool is made for your own computer and your own day. Please check your
  local laws and respect the people around you.
- Delete anything anytime: audio, transcripts, and timelines are plain files plus
  one SQLite database.

## Roadmap

- [ ] English UI — the i18n layer is in (`vlang.py`, switch with
      `DAYREPLAY_LANG=en`), but Chinese is still the default and only the
      timeline page is translated so far. **This is a great first contribution.**
- [ ] Pause schedules and meeting mode
- [ ] macOS / Linux support
- [ ] More spoken languages (zh + en only for now, by design)
- [ ] Plugin API for custom watchers

## Contributing

Issues and pull requests are welcome — especially from English speakers. Right now
the fastest ways to help:

1. **Translate docs** (`docs/` is Chinese; READMEs are bilingual)
2. **Test the installer on your machine** and report what breaks
3. Grab a `good first issue`

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT for the code. Bundled models (SenseVoice, CAM++, AST) keep their upstream
licenses — see `models/LICENSES.md`.
