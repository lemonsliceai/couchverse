# Contributing to Watch with Fox

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

See [README.md](README.md#quick-start) for full setup instructions. In short:

```bash
# Server (Python 3.11+, uv)
cd server
uv sync
uv run python src/podcast_commentary/agent/main.py download-files
cp .env.example .env       # fill in your API keys

# Chrome extension
cd ../chrome_extension
npm install && npm run build
```

Then run two terminals and load the extension:

1. **API server** — `uv run uvicorn podcast_commentary.api.app:app --host 0.0.0.0 --port 8080 --reload`
2. **Agent worker** — `uv run python src/podcast_commentary/agent/main.py dev`
3. **Extension** — `chrome://extensions` → Developer mode → Load unpacked → select `chrome_extension/`

See [`server/README.md`](server/README.md) and [`chrome_extension/README.md`](chrome_extension/README.md) for deeper dives.

## Making Changes

1. Fork the repo and create a branch from `main`.
2. Make your changes. Follow the existing code style:
   - **Python:** Ruff, 100-char line length, full type annotations (Python 3.11+ `X | Y` syntax).
   - **JavaScript (extension):** Plain ES modules bundled via esbuild. No TypeScript, no framework.
3. Test locally end-to-end: API + agent running, extension loaded, a real YouTube video open.
4. For Python changes, run `uv run ruff check src/` and `uv run ruff format --check src/` before pushing.
5. Open a pull request against `main`.

## Pull Requests

- Keep PRs focused — one feature or fix per PR.
- Describe what changed and why in the PR description.
- Include steps to test if applicable.

## Reporting Bugs

Open a [GitHub issue](https://github.com/lemonsliceai/watch-with-fox/issues) with:

- Steps to reproduce
- Expected vs. actual behavior
- Browser/OS and relevant environment details

## Security

To report a security vulnerability, see [SECURITY.md](SECURITY.md).
