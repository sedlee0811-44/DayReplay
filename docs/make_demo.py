"""生成英文版总账页面 —— 供 GitHub 展示与截图用。

为什么不是另写一个 HTML
------------------------
总账页面的样子（S 形轴、节点圆点、展开动画、搜索框）不是手写的，是
vdigest.ledger_to_html() 渲染出来的。手动仿一个 HTML，样式迟早和真实页面
对不上 —— 展示给别人的东西和真实产品长得不一样，那叫骗人。

所以这里的做法是：写一份**英文版总账 md**（和真实总账同一种格式），
丢给同一个渲染函数出页。结构唯一，样式必然一致。

    python docs/make_demo.py          # 出 docs/screenshots/demo_timeline.html
    python docs/make_demo.py --shot   # 顺带用 Edge 截成 PNG

英文版的触发方式：DAYREPLAY_LANG=en。vlang 负责所有文案、停用词表和
日期解析的切换，渲染管线本身不分叉。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
OUT_HTML = SRC / "docs" / "screenshots" / "demo_timeline.html"
OUT_PNG = SRC / "docs" / "screenshots" / "timeline.png"
OUT_PNG_SP = SRC / "docs" / "screenshots" / "social_preview.png"

# 英文版总账 md。和中文版同一种格式：
#   ## <日期> <星期> <起止> · <状态> · <时长>
#   一段总述
#   ### <时间段> · <标题>
#   **Screen** / **File Log** / **Voice** / **Details**
# 标签后必须跟全角空格"　"——解析器靠它把标签和正文分开。
LEDGER_MD = """# Timeline

## 2026-10-25 Sunday　00:00 onwards · Running · 12h 34m

Today was mostly about polishing and shipping DayReplay. I checked the timeline refresh logic and the date handling, cleaned up comments and the update scripts, verified the install guide and the version metadata, and finally finished the README, the release plan and the one-click installer. Twice the AI interrupted me with something I would never have noticed on my own.

### 09:47–09:48 · Two people arguing in another room
**Screen**　No screen activity — the argument was heard through the wall, not seen on screen.
**File Log**　Nothing was written or edited during this window.
**Voice**　At 09:47:32 the microphone picked up a heated argument about 8 meters away. Two distinct voices, raised tones, overlapping speech, 43 seconds long. The speaker could not be identified, so it was filed as a sound event rather than a conversation. The AI's note: "Sounds like people are arguing nearby — maybe don't take a shower right now if you'd rather not walk into that."
**Details**　[09:47 audio](ev/audio/2026-10-25/094732_114.wav) · [09:48 transcript](ev/voice/t1201.html) · [09:47 sound event](ev/voice/t1204.html)

### 10:23–10:35 · A chair moved — and the AI noticed I was coding
**Screen**　Visual Studio Code open on `voice_timeline.py`, the file that merges the audio transcript with the OCR text from screenshots so everything ends up on one searchable timeline. The cursor sat in the merge function.
**File Log**　Saved and edited `voice_timeline.py`, `vdigest.py` and `vsearch.py` during this window.
**Voice**　At 10:23:07 a chair scraped across the floor — someone sat down at the desk. From 10:23:45 I was talking through the design out loud: "...and then the timeline merges the audio transcript with the OCR text from screenshots, so you can search everything in one place..." The AI classified this as a focused development session and offered to summarise it.
**Details**　[10:23 audio](ev/audio/2026-10-25/102307_221.wav) · [10:30 transcript](ev/voice/t2820.html) · [10:30 screen](ev/shot/2026-10-25/10-00.jpg) · [voice_timeline.py](ev/doc/voice_timeline-docs.html)

### 10:41–10:43 · Running water
**Screen**　No screen activity recorded in this window.
**File Log**　Nothing was written or edited.
**Voice**　Sustained broadband noise consistent with a kitchen faucet or a bathroom sink, 2 minutes 47 seconds. The AI logged it as a water event — useful for the timeline, and a handy cue that the flat was empty.
**Details**　[10:41 audio](ev/audio/2026-10-25/104103_556.wav) · [10:43 sound event](ev/voice/t3011.html)

### 11:02–11:08 · Phone call about the release
**Screen**　Slack in the background, an unfinished message about the model download size.
**File Log**　Saved `README.md` and the release plan.
**Voice**　Speaker B, on the phone: "...yeah the deployment script is ready, just run install.bat and it pulls the models automatically ... about 1.3 gigabytes total ... no, it doesn't need internet after that ..."
**Details**　[11:02 audio](ev/audio/2026-10-25/110218_884.wav) · [11:05 transcript](ev/voice/t3455.html) · [11:00 screen](ev/shot/2026-10-25/11-00.jpg)

### 11:15–11:28 · Typing, and the docs went out
**Screen**　GitHub — the `DayReplay/README.md` file, in the browser.
**File Log**　Saved and edited `README.md`, `CONTRIBUTING.md` and `CHANGELOG.md`.
**Voice**　Rhythmic keystrokes for about 12 minutes, with occasional short phrases read aloud while editing.
**Details**　[11:15 audio](ev/audio/2026-10-25/111544_309.wav) · [11:20 transcript](ev/voice/t3777.html) · [11:00 screen](ev/shot/2026-10-25/11-00.jpg)

---

## 2026-10-24 Saturday　20:48 onwards · Finished · 3h 12m

A shorter evening session focused on debugging: I worked through the startup scripts, fixed the model download and the local network configuration, adjusted the responsive layout, then waited for the timeline page to finish saving. Two people in the house were talking about AI progress and a trillion-dollar market, but the words never came through clearly enough to transcribe.

### 23:55–07:14 · Tuning the screenshot settings and checking the interface
**Screen**　A page describing a change to the screenshot method, noting that setting the foreground flag to false leaves the right half of the desktop blank.
**File Log**　Saved and edited the user guide and the code archive.
**Voice**　At 23:57 someone was speaking; the content was roughly that the recording and the screenshot don't necessarily contain the same dynamic information, and there is no evidence for that claim either way.
**Details**　[23:57 audio](ev/audio/2026-10-24/235716_813.wav) · [0:00 screen](ev/shot/2026-10-25/00-00.jpg) · [user guide](ev/doc/user-guide-docs.html)

---

## 2026-10-23 Friday　09:14 onwards · Finished · 6h 05m

A long Friday session. The bulk of it was spent on the screen-recording side: I set up the optional screen capture, tuned the crop so the timeline shows the active window instead of the empty desktop, and then went through the whole day's audio looking for the moments worth keeping. Late in the afternoon the AI noticed a long stretch of typing with almost no speech and flagged it as a silent working block — useful, because I would have forgotten that hour existed.

### 14:20–16:05 · Silent working block, caught by the sound classifier
**Screen**　Video editing and the project folder open side by side.
**File Log**　Saved and edited the crop and layout parameters.
**Voice**　Almost no speech for 105 minutes — only keyboard clicks, a chair shifting twice, and one short "hmm" at 15:41. The AI classified the whole stretch as a focused work block rather than silence.
**Details**　[14:20 audio](ev/audio/2026-10-23/142011_455.wav) · [15:41 transcript](ev/voice/t1120.html) · [15:00 screen](ev/shot/2026-10-23/15-00.jpg)

### 09:14–11:02 · Setting up optional screen capture
**Screen**　Screen-capture settings — frame rate, crop region, and the toggle for ignoring the wallpaper.
**File Log**　Created `vsnap.py` and `vcrop.py`, then tuned the crop margin.
**Voice**　At 09:31 I said, out loud, "...okay so the crop should ignore everything that isn't the window, otherwise the timeline is just a picture of my wallpaper..." A door closed at 10:12.
**Details**　[09:31 audio](ev/audio/2026-10-23/093114_220.wav) · [09:35 transcript](ev/voice/t401.html) · [10:00 screen](ev/shot/2026-10-23/10-00.jpg)
"""

# 页面顶部的"正在记录"提示条，和真实实时模式一样。
HINT = ('<p class="hint"><b>● Recording</b>　Last update 12:29:43　'
        'Auto-refreshes every 5 min, no need to press F5</p>')


def main() -> int:
    # 必须在 import vdigest 之前设好语言 —— vlang 在 import 时就读它。
    os.environ["DAYREPLAY_LANG"] = "en"
    sys.path.insert(0, str(SRC))
    import vdigest

    html = vdigest.ledger_to_html(LEDGER_MD, refresh_meta="", hint=HINT,
                                  open_days=("2026-10-25",))
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"wrote {OUT_HTML}  ({len(html)//1024} KB)")

    if "--shot" in sys.argv:
        edge = None
        for c in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
            if Path(c).exists():
                edge = c
                break
        if not edge:
            print("no browser found for screenshot")
            return 1
        url = "file:///" + str(OUT_HTML).replace("\\", "/")
        subprocess.run([edge, "--headless=new", "--disable-gpu",
                        f"--screenshot={OUT_PNG}", "--window-size=1400,1000", url],
                       check=False)
        print(f"wrote {OUT_PNG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
