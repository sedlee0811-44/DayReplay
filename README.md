# DayReplay

> **A diary that writes itself.** Everything you say, the sounds around you, and the
> apps you use — on one timeline you can search and replay. Fully offline. Your day
> never leaves your PC.

[中文说明](README.zh-CN.md) · [Roadmap](#roadmap) · [Contributing](#contributing)

![DayReplay daily timeline](docs/screenshots/timeline.png)

---

## 📥 Download & Install (3 minutes)

### Step 1: Download

**Click this button to go to the download page:**

<p align="center">
<a href="../../releases"><img src="https://img.shields.io/badge/⬇️_Download-v1.1.1_-blue?style=for-the-badge&logo=github" alt="Download DayReplay"></a>
</p>

**Or go directly:** https://github.com/sedlee0811-44/DayReplay/releases

Download: **`DayReplay-1.1.1-lite-win64.zip`** (~330 MB)

### Step 2: Extract & Install

```
1. Right-click the ZIP → "Extract All..." → pick a folder
   (e.g., C:\Users\YourName\DayReplay\)

2. Open that folder, double-click:  install.bat

3. A window pops up and does everything automatically:
   ✓ Creates desktop shortcuts
   ✓ Offers to download AI models (~1.3 GB, one-time only)
   ✓ Runs a quick self-test
```

### Step 3: Start Using

Look at your Desktop — two new shortcuts appear:

| Shortcut | What it does |
|---|---|
| **DayReplay** | Double-click to START recording |
| **Stop DayReplay** | Double-click to STOP recording |

That's it! Within minutes, open your first daily timeline.

> **Never used GitHub before?** Here's what you need to know:
> - GitHub is where we host the download file — you don't need an account to download
> - The "Releases" page is just a file download page (like any other download site)
> - You get a normal ZIP file — extract it like any other ZIP
> - No registration, no account, no tracking — just download and run

<details>
<summary><b>📸 What happens after I double-click install.bat?</b></summary>

The installer window shows:

```
  ============================================
    DayReplay  -  A diary that writes itself
  ============================================

  [1/4] Creating desktop shortcuts...
         OK: Desktop shortcut created

  [2/4] Checking AI models...
         AI MODELS NOT FOUND (~1.3 GB)
         Download models now? (Y/n)  ← type Y and press Enter

  Starting model download... (this may take a while)
  ████████████████████████████████ 100%

  [3/4] Running quick self-test...
         OK: Self-test passed!

  [4/4] Installation complete!
  ============================================

  HOW TO USE:
    Desktop "DayReplay"      → START recording
    Desktop "Stop DayReplay" → STOP recording
```

</details>

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

## Quick start (summary)

| Step | What to do | Time |
|---|---|---|
| **1. Download** | Get `DayReplay-*-lite-win64.zip` from [Releases](../../releases) | ~1 min |
| **2. Extract** | Right-click ZIP → "Extract All" → pick a folder | 10 sec |
| **3. Install** | Open folder → double-click **`install.bat`** | 2 min (first run downloads ~1.3 GB models) |
| **4. Use** | Double-click **DayReplay** on Desktop to start | instant |

**Requirements:** Windows 10/11 64-bit. No admin rights. No Python. No GPU.
Internet only needed once (for AI model download).

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

Want to help? Great! Here's how:

**Found a bug?** → [Open an Issue](../../issues) (tell us your Windows version + what happened)

**Want to add a feature?** → See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide

**Quick ways to contribute:**
1. 🌍 **Translate docs** — `docs/` is in Chinese; any English translation helps
2. 🖥️ **Test on your PC** — run the installer and tell us what breaks
3. 🐛 **Report bugs** — [Issues page](../../issues) — screenshot + error log = <3

## Contact / 联系方式

| Method | When to use | Link |
|---|---|---|
| **GitHub Issues** | Bug reports, feature requests, public discussion | [Open an Issue](../../issues) |
| **GitHub Discussions** | Questions, ideas, casual chat | [Discussions page](../../discussions) |
| **Email** | Private / business inquiries only | `sedlee0811` **at** `gmail` **dot** `com` |

> **Prefer Issues or Discussions** when possible — it helps others with the same question,
> and keeps the conversation public and searchable.
> Email is reserved for private matters (collaboration, licensing, security issues).

## License

MIT for the code. Bundled models (SenseVoice, CAM++, AST) keep their upstream
licenses — see `models/LICENSES.md`.
