# Server

Python 3.11+ backend with two processes: a FastAPI HTTP server and a LiveKit AI agent (Fox).

See the repo root [`README.md`](../README.md) for the big picture and [`CLAUDE.md`](CLAUDE.md) for architecture, gotchas, and code style.

## Quick start

```bash
cp .env.example .env          # fill in API keys
uv sync
uv run uvicorn podcast_commentary.api.app:app --host 0.0.0.0 --port 8080 --reload   # API
uv run python src/podcast_commentary/agent/main.py dev                              # agent
```

Linting / tests:

```bash
uv run ruff check src/
uv run ruff format --check src/
uv run pytest
```

## FoxConfig — tuning Fox's behaviour

Every knob that shapes Fox — the system prompt, comedic angles, response CTAs, timing/cadence, LLM/STT/TTS/VAD/avatar settings — lives in a single dataclass loaded once per agent process.

### Layout

```
src/podcast_commentary/agent/
├── fox_config.py              # FoxConfig schema + loader + CONFIG export
└── fox_configs/               # Preset bank — one file per personality
    ├── __init__.py
    └── default.py             # Stock production values
```

`fox_config.py` defines the `FoxConfig` dataclass with nine nested sub-configs:

| Sub-config | What it governs |
|---|---|
| `persona` | `system_prompt`, `intro_prompt`, `comedic_angles`, `angle_lookback`, `commentary_cta`, `user_reply_cta` |
| `timing` | `min_silence_between_jokes_s`, `burst_window_s`, `max_jokes_per_burst`, `burst_cooldown_s`, `sentences_before_joke`, `silence_fallback_s`, `post_speech_safety_s`, `user_turn_grace_s`, `transcript_chunk_s` |
| `context` | `comment_memory_size`, `comments_shown_in_prompt` |
| `llm` | `model`, `max_tokens` |
| `stt` | `model` |
| `tts` | `voice_id`, `model`, `stability`, `similarity_boost`, `speed` |
| `vad` | `activation_threshold` |
| `avatar` | `active_prompt`, `idle_prompt`, `startup_timeout_s` |
| `playout` | `intro_timeout_s`, `commentary_timeout_s` |

Every module (`prompts.py`, `angles.py`, `commentary.py`, `comedian.py`, `user_turn.py`, `podcast_pipeline.py`, `main.py`) reads from the module-level `CONFIG` — no other file hardcodes behaviour knobs.

### Switching presets

The active preset is selected by the `FOX_CONFIG` env var in `server/.env` (defaults to `default`). Its value must match a filename in `fox_configs/` (without the `.py` extension).

**To create and test a new preset:**

```bash
# 1. Copy the default as a starting point
cp src/podcast_commentary/agent/fox_configs/default.py \
   src/podcast_commentary/agent/fox_configs/spicy.py

# 2. Edit spicy.py — tweak anything in the FoxConfig(...) block.
#    Be sure to update `name="spicy"` so logs show which preset loaded.

# 3. Point the agent at the new preset
echo "FOX_CONFIG=spicy" >> .env

# 4. Restart the agent
uv run python src/podcast_commentary/agent/main.py dev
```

On startup the agent logs the active preset:

```
Loaded FoxConfig preset 'spicy' (FOX_CONFIG=spicy)
```

If `FOX_CONFIG` points at a file that doesn't exist, the agent fails fast with a clear error — no silent fallback.

### Notes

- **Frozen dataclasses.** Every sub-config is `@dataclass(frozen=True)` — presets are read-only snapshots so nothing mutates Fox's behaviour mid-session.
- **Loaded once per process.** `CONFIG` is evaluated at import time. To switch presets, change `FOX_CONFIG` in `.env` and restart the agent; hot-reload is not supported.
- **Keep `default.py` as ground truth.** When adding new knobs, update the `FoxConfig` schema in `fox_config.py`, add the value to `default.py`, and reference it from the module that needs it.
- **Don't hardcode new knobs.** If you find yourself about to drop a new magic number or prompt string into a module, add it to `FoxConfig` first.
