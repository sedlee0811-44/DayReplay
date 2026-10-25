# Contributing to DayReplay

**Thank you for wanting to contribute!** Whether you found a bug, want to add a feature,
or just want to help translate — every contribution matters.

---

## Table of Contents

- [I'm a user — I found a bug!](#im-a-user--i-found-a-bug)
- [I'm a developer — I want to fix/add something](#im-a-developer--i-want-to-fixadd-something)
- [Development setup](#development-setup)
- [Code style & rules](#code-style--rules)
- [Ways to help (quick list)](#ways-to-help-quick-list)

---

## I'm a user — I found a bug!

### How to report a bug

1. **Go to:** https://github.com/sedlee0811-44/DayReplay/issues
2. Click **"New Issue"**
3. Fill in this template:

```text
## Bug description
[What went wrong? One or two sentences.]

## Steps to reproduce
1. I did this...
2. Then this happened...
3. And then...

## Expected behavior
[What should have happened?]

## Actual behavior
[What actually happened?]

## System info
- Windows version:  [e.g., Windows 11 23H2]
- DayReplay version: [e.g., v1.1.1 — see VERSION file or Releases page]
- Install type:     [lite ZIP / from source]

## Log excerpt (if any)
[Paste the last 20 lines from logs/daemon.log]
[⚠️ NEVER paste real transcript text or audio file names — those are private!]
```

4. Click **"Submit new issue"** — done!

> **Don't have a GitHub account?** It's free and takes 30 seconds:
> Go to https://github.com/signup — you only need an email address.

### What happens next?

- Someone will look at your report within a few days
- We might ask follow-up questions
- When it's fixed, you'll get a notification — just download the new version

---

## I'm a developer — I want to fix/add something!

Here's the complete workflow. It looks long but each step is simple:

```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│   Fork   │ → │  Clone   │ → │   Fix    │ → │  Push    │ → │   PR     │
│          │    │          │    │          │    │          │    │          │
│ (1 click)│    │(1 command│    │ (edit in │    │(1 command│    │(fill form│
│          │    │  + enter)│    │  VS Code)│    │  + enter)│    │  + submit)│
└──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘
   ~10 sec         ~30 sec        varies           ~10 sec         ~2 min
```

### Step 1: Fork (make your own copy)

1. Open: https://github.com/sedlee0811-44/DayReplay
2. Click the **"Fork"** button (top-right corner)
3. This creates YOUR copy of the repo at `https://github.com/YOUR-USERNAME/DayReplay`

### Step 2: Clone (download to your computer)

Open a terminal (PowerShell, Git Bash, or CMD) and run:

```bash
git clone https://github.com/YOUR-USERNAME/DayReplay.git
cd DayReplay
```

This downloads the code to your computer.

### Step 3: Create a branch

Always make changes on a new branch (never on `main`):

```bash
git checkout -b fix/your-bug-name        # for bug fixes
# OR
git checkout -b feature/your-feature-name # for new features
```

Examples:
- `git checkout -b fix/crash-on-startup`
- `git checkout -b feature/dark-mode-ui`
- `git checkout -b docs/translate-readme`

### Step 4: Make your changes

Edit files in your favorite editor (VS Code recommended):

```bash
code .    # opens VS Code in this folder
```

Test your changes before proceeding (see [Development setup](#development-setup)).

### Step 5: Commit & Push

When your changes are ready:

```bash
# See what files changed
git status

# Add the files you changed
git add path/to/changed/file.py

# Write a commit message (describe WHAT and WHY)
git commit -m "Fix: describe what you fixed"

# Send to your GitHub fork
git push origin fix/your-bug-name
```

### Step 6: Pull Request (PR)

1. Open your fork in browser: `https://github.com/YOUR-USERNAME/DayReplay`
2. You'll see a banner saying **"This branch is ahead... Compare & pull request"**
3. Click that button
4. Fill in the PR form:

```text
## Title
Fix/Feature: short description (e.g., "Fix: crash when microphone is disconnected")

## What does this PR do?
[Briefly explain what you changed and why]

## How to test?
[Steps for someone else to verify your fix works]

## Screenshots (if UI changes)
[paste or attach screenshots]
```

5. Click **"Create pull request"** — done!

### What happens after you submit a PR?

- Automated checks run (tests, linting)
- A maintainer reviews your code (might ask for changes)
- Once approved, your code gets merged into the main project
- 🎉 Your name appears in the contribution graph!

---

## Development setup

### Prerequisites

| What | Version | Why |
|---|---|---|
| Windows | 10 or 11, 64-bit | Target platform |
| Python | 3.11 | Runtime |
| Git | latest | Version control |

### One-time setup

```bash
# 1. Clone the repo
git clone https://github.com/sedlee0811-44/DayReplay.git
cd DayReplay

# 2. Create virtual environment
python -m venv .venv

# 3. Activate it
.venv\Scripts\activate      # PowerShell/CMD

# 4. Install dependencies
pip install -r requirements.txt
```

### Running the app

```bash
# Terminal 1: Start the recorder daemon
python voice_daemon.py

# Terminal 2: Build today's timeline page
python vdigest.py

# Terminal 3: Search / explore data
python vsearch_ui.py
```

### Running tests

**Before submitting a PR, always run these:**

```bash
# Fast tests (no models needed, ~30 seconds)
python test_segment.py
python test_search.py
python test_update.py
python test_e2e_events.py

# Slow test (needs ASR model loaded, ~2 minutes)
python test_events.py
```

All tests must pass before we can merge your PR.

---

## Code style & rules

These are non-negotiable. PRs that break these will not be merged:

### 🔒 Privacy first

- **No cloud calls. No telemetry. Ever.**
- Any new network request MUST be:
  - Off by default AND
  - Clearly documented AND
  - Easy to disable
- If your PR adds a hidden network call, it will be rejected

### 🔄 Backward compatible

- New features must be opt-in (don't change existing users' experience)
- New config options must have sensible defaults
- Database schema changes need migration logic

### 📝 Documentation

- Public functions need docstrings
- New config options need comments explaining them
- UI text goes through `vlang.py` (i18n) — no hardcoded strings

### 🧪 Tests

- Bug fixes need a regression test
- New features need unit tests
- If you can't write a test, explain why in the PR

---

## Ways to help (quick list)

| Type | What | Difficulty | Good for beginners? |
|---|---|---|---|
| 🐛 **Bug report** | Found something broken? Tell us! | trivial | ✅ anyone |
| 🌍 **Translation** | Docs are Chinese → English | easy | ✅ great first PR |
| 🖥️ **Testing** | Run installer, report issues | easy | ✅ any Windows PC |
| 🎨 **UI work** | Timeline HTML/CSS/JS (`vdigest.py`) | medium | ✅ HTML/CSS skills enough |
| 🔊 **Sound tuning** | Add sound event labels | medium | needs domain knowledge |
| 🧩 **Core dev** | New features, refactoring | hard | needs Python + audio |

---

## Questions?

- **Bug reports:** https://github.com/sedlee0811-44/DayReplay/issues
- **Discussions / questions:** https://github.com/sedlee0811-44/DayReplay/discussions
- **Email (private only):** `sedlee0811` at gmail dot com
- **Security issues:** please DO NOT open a public issue — email directly

---

*Thank you for making DayReplay better!* 🙌
