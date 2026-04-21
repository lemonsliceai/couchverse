@AGENTS.md

# Web Frontend

Next.js 16, React 19, TypeScript 5.9, Tailwind CSS v4, LiveKit client.

## Commands

```bash
npm install           # install deps
npm run dev           # dev server on :3000 (reads API URL from web/.env)
npm run build         # production build
npx eslint .          # lint
```

## Structure

```
src/
├── app/
│   ├── layout.tsx          # Root layout (metadata, fonts)
│   ├── page.tsx            # Home — YouTube URL input → createSession → redirect to /watch
│   └── watch/page.tsx      # Main player — LiveKitRoom wrapper, video + avatar + controls
├── components/
│   ├── VideoPlayer.tsx     # YouTube iframe + hidden <audio> for Web Audio API capture
│   ├── AvatarSidebar.tsx   # LemonSlice avatar video track + live captions
│   └── CommentaryControls.tsx  # Volume sliders, hold-to-talk, disconnect
└── lib/
    └── api.ts              # createSession() API client
```

## Key patterns

- **LiveKit room** wraps the watch page. Audio/video tracks come from LiveKit, not direct WebSocket.
- **Audio ducking:** When Fox speaks, video volume reduces automatically. Controlled by WebRTC data channel signals (`commentary_start` / `commentary_end`).
- **Audio capture:** A hidden `<audio>` element streams YouTube audio through the API's CORS proxy (`/api/audio-stream/{id}`). Web Audio API captures it and publishes to LiveKit so the agent can hear the podcast.
- **Data channel commands:** VideoPlayer sends `{type:"play", t:number}` and `{type:"pause"}` to the agent via LiveKit data channel to sync playback.

## Gotchas

- **`NEXT_PUBLIC_API_URL`** is read from `web/.env` (defaults to `http://localhost:8080`). Baked at build time.
- **All deps live in `dependencies`**, not `devDependencies`. This project is developed and tested with `NODE_ENV=production`, which makes npm skip `devDependencies`. Never split build tooling out — if it's needed to `npm run build`, it's a runtime dep here.
- **Imports:** Use `@/` alias for absolute imports (configured in tsconfig.json).
- **Styling:** Tailwind v4 via `@tailwindcss/postcss` plugin — no `tailwind.config.js` file.
