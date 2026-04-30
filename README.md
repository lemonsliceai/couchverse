<div align="center">

# Couchverse

### Live AI commentary on whatever you're tuned into.

Two AI co-hosts, **Cat girl** and **Alien**, react in real time to any audio playing in your browser tab. Think MST3K, except the hecklers live in your Chrome side panel and they'll cover a podcast or a TikTok feed as happily as a movie.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Chrome MV3](https://img.shields.io/badge/Chrome-MV3-4285F4?logo=googlechrome&logoColor=white)](chrome_extension/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg?logo=python&logoColor=white)](server/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LiveKit Agents](https://img.shields.io/badge/LiveKit-Agents-FF5722)](https://livekit.io/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**[Quick start](#quick-start)** · **[Architecture](#architecture)** · **[The hosts](#the-hosts)** · **[Contributing](CONTRIBUTING.md)** · **[Security](SECURITY.md)**

<img src="docs/screenshot.png" alt="Couchverse side panel with Cat girl and Alien reacting to a tab" width="820" />

</div>

---

## What it does

Couchverse works on **any** website with audio, not just `youtube.com`. Anything a tab can play is fair game:

- YouTube videos, podcasts in a web player, Spotify, SoundCloud mixes
- livestreams, webinars, talking heads on TikTok, lecture replays

The hosts don't know or care what site the audio came from. They only hear it.

## Highlights

- **Zero server-side audio extraction.** The extension captures the tab with `chrome.tabCapture` and publishes it to LiveKit. The agent subscribes to the track. No scraping, no per-site hacks.
- **Two personas out of the box.** Cat girl (the moody emo deadpan) and Alien (the sniper one-liner machine) share one `FoxConfig` schema, so swapping or adding personalities is a single file drop.
- **Optional persistence.** Plug in Neon PostgreSQL to log conversations, or leave `DATABASE_URL` blank and run ephemerally.
- **Portable.** Frontend runs anywhere Chrome does; the server runs locally with `uv` or on Fly.io with two processes.

## Architecture

```
┌──────────────────────┐       ┌──────────────────┐       ┌──────────────────────┐
│  Chrome extension    │──────▶│  FastAPI server  │       │   LiveKit Agent      │
│  (chrome_extension/) │       │  (server/)       │       │   (server/)          │
│                      │       │                  │       │                      │
│  - Tab audio via     │       │  - Session mgmt  │       │  - Groq Whisper STT  │
│    tabCapture        │       │  - Token gen     │       │  - Llama Scout LLM   │
│  - LiveKit publish   │       │  - Neon Postgres │       │  - ElevenLabs TTS    │
│  - Side panel UI     │       │                  │       │  - LemonSlice avatar │
└──────────┬───────────┘       └──────────────────┘       └──────────┬───────────┘
           │                                                         │
           └──────────────── LiveKit Cloud (WebRTC) ─────────────────┘
```

The Chrome extension is the only frontend. It captures the active tab's audio via `chrome.tabCapture` and publishes it as a LiveKit track. The agent subscribes to that track for STT. No server-side audio extraction, no per-site scraping.

### Tech stack

| Layer       | Stack                                                                    |
|-------------|--------------------------------------------------------------------------|
| Frontend    | Chrome MV3 extension, esbuild, `livekit-client`                          |
| API         | FastAPI, asyncpg, Neon PostgreSQL, Fly.io                                |
| Agent       | LiveKit Agents, Groq (STT + LLM), ElevenLabs TTS, LemonSlice avatars     |
| Transport   | LiveKit Cloud (WebRTC)                                                   |

## Quick start

> [!NOTE]
> You'll need API keys for [LiveKit Cloud](https://cloud.livekit.io/), [Groq](https://console.groq.com/), [ElevenLabs](https://elevenlabs.io/), and [LemonSlice](https://www.lemonslice.com/). [Neon](https://neon.tech/) is optional — without `DATABASE_URL`, the app runs without persistence.

> [!NOTE]
> The GitHub repo is `lemonsliceai/watch-with-fox` for historical reasons — the project was renamed to Couchverse. Same code, same product.

> [!IMPORTANT]
> `AVATAR_BASE_URL` must be reachable from LemonSlice Cloud's servers — `localhost` won't work. Either deploy the server, host the avatars on a public CDN/bucket, or expose your local server with `ngrok http 8080`.

```bash
# 1. Clone
git clone https://github.com/lemonsliceai/watch-with-fox.git
cd watch-with-fox

# 2. Install and configure the server
cd server
uv sync
uv run python src/podcast_commentary/agent/main.py download-files
cp .env.example .env       # then fill in your API keys (incl. AVATAR_BASE_URL)

# 3. Build the extension
cd ../chrome_extension
npm install && npm run build

# 4. Start the API (terminal 1)
cd ../server
uv run uvicorn podcast_commentary.api.app:app --host 0.0.0.0 --port 8080 --reload

# 5. Start the agent (terminal 2)
cd server
uv run python src/podcast_commentary/agent/main.py dev

# 6. Load the extension
#    chrome://extensions → enable Developer mode → Load unpacked → chrome_extension/
#    Open a tab with something playing, click the Couchverse icon in the toolbar.
```

### Go deeper

- **[`chrome_extension/README.md`](chrome_extension/README.md)** — build, load, and debug the extension
- **[`server/README.md`](server/README.md)** — server commands, preset tuning, deployment

## The hosts

<table>
<tr>
<td width="50%" valign="top">

### Cat girl — the emo deadpan

Moody, slightly sarcastic riffs in a flat voice — secretly attentive and protective once someone's honest with you. The stock primary when you don't configure anything.

</td>
<td width="50%" valign="top">

### Alien — the sniper

Cat girl's foil. Dry one-liner machine — anchors on a specific thing the speakers just said and snaps it shut.

</td>
</tr>
</table>

Both are driven by the same `FoxConfig` schema in `server/src/podcast_commentary/agent/`. Drop a new file in `fox_configs/` and add it to `PERSONAS` to load it. See the [server README](server/README.md#foxconfig--tuning-host-behaviour) for the full walkthrough.

## Project layout

```
.
├── chrome_extension/   # The frontend. MV3 extension with side panel UI.
├── server/             # FastAPI HTTP server plus LiveKit AI agent.
│   ├── src/podcast_commentary/api/    # Session and token endpoints
│   ├── src/podcast_commentary/agent/  # Agent pipeline: STT, LLM, TTS, avatar
│   └── migrations/                    # PostgreSQL schema
└── docs/               # Screenshots and supplementary docs
```

## Community

- **Bugs & feature requests** — [open an issue](https://github.com/lemonsliceai/watch-with-fox/issues)
- **Contributing** — read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR
- **Code of conduct** — [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- **Security** — disclose privately per [SECURITY.md](SECURITY.md)

## License

Released under the [MIT License](LICENSE).

<div align="center">

Built with [LiveKit](https://livekit.io/) · [Groq](https://groq.com/) · [ElevenLabs](https://elevenlabs.io/) · [LemonSlice](https://www.lemonslice.com/)

</div>
