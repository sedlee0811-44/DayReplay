# Contributing / 参与贡献

Thanks for your interest in making DayReplay better! / 感谢你愿意让 DayReplay 变得更好！

## Ways to help / 能帮上忙的事

| Help wanted / 需要的帮助 | Detail / 说明 |
|---|---|
| 🌍 **Translate docs** | `docs/` manuals are Chinese. Translating any part into English is a huge win. / 手册目前是中文，翻译任何一部分都是大帮助 |
| 🖥️ **Test on your machine** | Run the installer on your Windows PC and report what breaks. / 在你的 Windows 机器上实测安装器，报告问题 |
| 🧩 **good first issue** | Small, well-scoped tasks labeled in the issue tracker. / 见 issue 列表中带此标签的小任务 |
| 🔊 **Sound event tuning** | Add or recalibrate activity-sound labels (AudioSet mapping). / 补充或校准活动声标签 |
| 🎨 **UI** | The daily timeline page (`vdigest.py`) is self-contained HTML/SVG/JS. / 时间轴页面是自包含的 HTML/SVG/JS，改起来很直接 |

## Dev setup / 开发环境

```text
Windows 10/11 64-bit, Python 3.11
git clone https://github.com/sedlee0811-44/DayReplay.git
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
python voice_daemon.py        # recorder + speech-to-text + screen watcher
python vdigest.py             # build today's timeline page
```

Run the test suites before submitting a PR / 提 PR 前请先跑测试：

```text
python test_segment.py
python test_search.py
python test_update.py
python test_events.py     # needs the ASR model, slower / 需要加载模型，较慢
```

## Ground rules / 基本约定

- **No cloud. No telemetry. Ever.** Any PR that adds a network call will be
  rejected unless it is strictly optional and off by default.
  / 不上云、不埋点。任何新增网络请求的 PR，若非默认关闭的可选项，一律拒绝。
- Keep the default behavior byte-compatible: new code paths must not change
  what existing users get. / 新分支不许改变现有用户的默认行为。
- Docs in `docs/` are Chinese-first; English translations live alongside
  (`.en.md` suffix or the same file, bilingual). / 文档中文优先，可中英同页。

## Reporting bugs / 报告问题

Open an issue with: Windows version, what you did, what you expected, what
happened, and the relevant lines from `logs/daemon.log`
(**redact any personal content** — never paste transcript text or audio paths
of real recordings). / 报 bug 请附系统版本、复现步骤、`logs/daemon.log` 相关行，
**务必隐去真实转写内容等隐私**。
