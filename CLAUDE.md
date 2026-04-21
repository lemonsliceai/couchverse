# Watch with Fox

AI comedian avatar that delivers real-time comedic commentary while users watch YouTube videos. MST3K meets AI.

## Stack

- **Frontend:** Next.js 16, React 19, TypeScript, Tailwind CSS v4, LiveKit client
- **API Server:** FastAPI on Fly.io, asyncpg + Neon PostgreSQL
- **AI Agent:** LiveKit Agents framework on LiveKit Cloud (Groq STT/LLM, ElevenLabs TTS, LemonSlice avatar)

## Running locally

Three terminals required — they connect: `web → api → LiveKit Cloud ← agent`

```bash
# One-time setup
cd server && uv sync && uv run python src/podcast_commentary/agent/main.py download-files
cd ../web && npm install

# Terminal 1: API (port 8080)
cd server && uv run uvicorn podcast_commentary.api.app:app --host 0.0.0.0 --port 8080 --reload

# Terminal 2: Agent
cd server && uv run python src/podcast_commentary/agent/main.py dev

# Terminal 3: Web (port 3000)
cd web && npm run dev
```

## Chrome Extension

The Chrome extension (`chrome_extension/`) provides an alternative frontend that captures tab audio directly, eliminating the server-side yt-dlp + ffmpeg + proxy pipeline.

```bash
# One-time setup
cd chrome_extension && npm install && npm run build

# Load in Chrome
# 1. Go to chrome://extensions
# 2. Enable "Developer mode"
# 3. Click "Load unpacked" → select chrome_extension/
# 4. Navigate to a YouTube video → click extension icon to open side panel
```

### How it works

1. Content script monitors the YouTube `<video>` element for play/pause/seek
2. Side panel connects to LiveKit and captures tab audio via `chrome.tabCapture`
3. Tab audio is published as a `podcast-audio` LiveKit track
4. Agent subscribes to this track for STT instead of running ffmpeg
5. Avatar + commentary render in the side panel

### Two audio paths

| | Web app (Next.js) | Chrome extension |
|---|---|---|
| **Audio source** | Agent extracts via yt-dlp → ffmpeg decodes | Extension captures tab audio directly |
| **Session creation** | `source: null` → `audio_source: "server"` | `source: "extension"` → `audio_source: "browser"` |
| **Agent behavior** | Runs yt-dlp + ffmpeg + proxy | Subscribes to `podcast-audio` LiveKit track |
| **IP pinning** | Required (sticky proxy) | Not needed |
| **CORS proxy** | Required (`/api/audio-stream/{id}`) | Not needed |

## Key architecture decisions

- **All npm deps live in `dependencies`, not `devDependencies`:** This project is developed and tested with `NODE_ENV=production` in the shell, which makes npm silently skip `devDependencies`. Anything needed to `npm install && npm run build` (esbuild, typescript, tailwind, eslint, @types, etc.) must be a regular `dependency`. Don't set or inject `NODE_ENV` in scripts or code to work around this — just put the dep in the right place.
- **Agent name isolation:** `AGENT_NAME` in `server/.env` must differ between local and production. Local uses `podcast-commentary-agent-local`; production uses `podcast-commentary-agent`. If both register the same name, LiveKit round-robins between them.
- **Audio proxy (web app only):** YouTube CDN doesn't send CORS headers. The API proxies audio through `GET /api/audio-stream/{id}` so the browser's Web Audio API can capture it. Not needed by the Chrome extension.
- **YouTube IP pinning (web app only):** YouTube signs audio URLs to the requester's IP. The agent (not the API) must extract the URL via yt-dlp so ffmpeg fetches from the same IP. When using a proxy, sticky sessions pin the exit IP for 30 minutes. The Chrome extension bypasses this entirely.
- **Database is optional:** If `DATABASE_URL` is unset, the app runs without persistence. Conversation logging silently no-ops.
- **Avatar URL must be public:** LemonSlice Cloud fetches the avatar image from its servers, so `localhost` URLs won't work. Use the deployed Fly.io URL or an ngrok tunnel.

## Environment

All env vars go in `server/.env` (see `server/.env.example`). The web app reads `NEXT_PUBLIC_API_URL` from `web/.env` (defaults to `http://localhost:8080`).

## Code style

- **Python:** Ruff, line-length 100. Full type annotations (Python 3.11+ union syntax).
- **TypeScript:** ESLint with Next.js config. Strict mode. Absolute imports via `@/` alias.
- Comments explain "why", not "what".
