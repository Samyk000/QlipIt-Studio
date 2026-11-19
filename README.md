<div align="center">

<img src="public/app-icon.png" alt="QlipIt Studio Logo" width="128" height="128" style="border-radius: 24px;" />

# QlipIt Studio

**Turn long videos and live streams into viral, captioned vertical shorts in seconds.**  
*Free, open-source, and privacy-first — run locally on your own machine or self-host in the cloud.*

[![Release](https://img.shields.io/badge/release-v1.0.0-blue.svg?style=for-the-badge)](https://github.com/Samyk000/QlipIt-Studio/releases)
[![Python](https://img.shields.io/badge/python-3.10+-3776AB.svg?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg?style=for-the-badge)]()

[Download Windows App](#-windows-app) • [Quick Start](#-quick-start) • [Features](#-core-capabilities) • [Supported AI](#-bring-your-own-ai)

---

</div>

## ⚡ Why QlipIt Studio?

Creating vertical content manually is tedious: finding key moments, cropping to 9:16, adding captions, recording voiceovers, and preparing exports eats hours of creator time.

**QlipIt Studio automates the entire pipeline:**
1. **Paste any link** (YouTube, Twitch, Kick, or local video file).
2. **AI detects high-retention moments** with hook scoring and virality analysis.
3. **Smart auto-crop to 9:16** with animated word-by-word kinetic captions.
4. **Add neural voiceover narration** with automatic background audio ducking.
5. **Download finished clips** with generated titles, descriptions, and hashtags.

---

## ✨ Core Capabilities

| Feature | Description |
| :--- | :--- |
| 🎯 **AI Moment Discovery** | Transcribes audio and prompts LLMs to isolate punchy, standalone highlights with viral hook scoring. |
| 🎙️ **Neural Voiceover Studio** | Audition multi-actor voices, generate AI narration scripts, and burn synced voiceovers with audio ducking. |
| 🔴 **Live Stream Clipping** | Clip live Twitch, Kick, or YouTube streams in real time as the action unfolds using chat activity and loudness. |
| 💬 **Kinetic Word Captions** | 3 animated caption styles (*Bounce*, *Clean*, *Hype*) with syllable timing and multilingual support. |
| 📱 **9:16 Smart Reframing** | Automatically crops and centers streamer cameras, gameplay, and video subjects for vertical screens. |
| 📦 **Ready-to-Use Exports** | Download finished clips and copy their generated titles, descriptions, and hashtags. |
| 🔒 **100% Private & Local** | No sign-in required on your personal desktop. Your keys, videos, and data never leave your computer. |

---

## 🖥️ Windows App

Get started immediately with the standalone Windows installer — no Python, Git, or command prompt required:

1. Download **`QlipIt-Setup-1.0.0.exe`** from [Releases](https://github.com/Samyk000/QlipIt-Studio/releases).
2. Run the installer and launch **QlipIt Studio** from your Start Menu or Desktop.
3. Everything (including FFmpeg) is pre-bundled and ready out-of-the-box.

> **Developer Note:** To build the Windows installer yourself from source:
> ```powershell
> powershell -ExecutionPolicy Bypass -File desktop\build.ps1
> ```

---

## 🚀 Quick Start (CLI / Self-Host)

### Prerequisites
- **Python 3.10+**
- **FFmpeg** (`winget install Gyan.FFmpeg` on Windows, `brew install ffmpeg` on macOS, or `apt install ffmpeg` on Linux)

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/Samyk000/QlipIt-Studio.git
cd QlipIt-Studio

# 2. Set up virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On macOS/Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the studio
python -m studio.server
```

Open **`http://localhost:8765`** in your browser to start clipping!

---

## 🐳 Docker Deployment

For headless servers or cloud hosting:

```bash
docker compose up -d --build
```

Access the studio at `http://localhost:8765`. For multi-user setups with authentication, set `QLIPIT_ACCOUNTS=1` in your `.env`.

---

## 🤖 Bring Your Own AI

QlipIt Studio is vendor-neutral. Connect any provider or use local offline intelligence:

- **Cloud Models:** Google Gemini, OpenAI (GPT-4o / GPT-4o-mini), Grok (xAI), Groq (Llama 3 ultra-fast), OpenRouter, Anthropic Claude.
- **Local & Offline:** Run local models via [Ollama](https://ollama.com) (e.g., `llama3.2`, `mistral`, `qwen2.5`) with zero API costs.
- **No AI Key?** QlipIt seamlessly falls back to acoustic loudness and audio energy algorithms to identify peak hype moments.

---

## 📄 License

QlipIt Studio is licensed under the [MIT License](LICENSE). Built for creators, editors, and developers worldwide.
