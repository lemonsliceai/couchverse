# Contributing to Couchverse

Thanks for your interest in contributing — PRs are genuinely welcome.

## Table of contents

- [Development setup](#development-setup)
- [Making changes](#making-changes)
- [Pull requests](#pull-requests)
- [Reporting bugs](#reporting-bugs)
- [Security](#security)
- [Code of conduct](#code-of-conduct)

## Development setup

See the root [README.md](README.md#quick-start) for the full walkthrough. Short version:

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

Then two terminals plus the loaded extension:

1. **API server** — `uv run uvicorn podcast_commentary.api.app:app --host 0.0.0.0 --port 8080 --reload`
2. **Agent worker** — `uv run python src/podcast_commentary/agent/main.py dev`
3. **Extension** — `chrome://extensions` → Developer mode → Load unpacked → select `chrome_extension/`

For deeper docs, see [`server/README.md`](server/README.md) and [`chrome_extension/README.md`](chrome_extension/README.md).

## Making changes

1. Fork the repo and create a branch from `main`.
2. Follow the project's code style:

   | Area | Style |
   |---|---|
   | Python | Ruff, 100-char line length, full type annotations using Python 3.11+ union syntax (`X \| Y`) |
   | JavaScript (extension) | Plain ES modules bundled via esbuild. No TypeScript, no framework |
   | Comments | Explain **why**, not **what** |

3. Test end-to-end: API and agent running, extension loaded, a real tab playing audio.
4. For Python changes, run before pushing:

   ```bash
   uv run ruff check src/
   uv run ruff format --check src/
   ```

5. Open a pull request against `main`.

## Pull requests

- Keep PRs focused — one feature or fix per PR.
- Describe **what** changed and **why** in the PR description.
- Include test steps if applicable.

## Reporting bugs

Open a [GitHub issue](https://github.com/lemonsliceai/couchverse/issues) with:

- Steps to reproduce
- Expected vs. actual behavior
- Browser / OS / environment details

## Security

> [!IMPORTANT]
> Please **don't** file security issues in public. See [SECURITY.md](SECURITY.md) for the private disclosure channel.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you agree to uphold it.
