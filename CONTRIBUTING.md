# Contributing to Couchverse

Thanks for your interest in contributing — PRs are genuinely welcome.

> [!IMPORTANT]
> Couchverse is released under the [PolyForm Noncommercial License 1.0.0](LICENSE). LemonSlice also offers Couchverse under separate commercial terms. By submitting a contribution, you agree that LemonSlice may distribute your contribution under both the PolyForm Noncommercial License and any commercial license LemonSlice offers, and you represent that you have the right to grant that permission. If your employer has rights to your work, please make sure they are okay with this before contributing.

## Table of contents

- [Development setup](#development-setup)
- [Making changes](#making-changes)
- [Pull requests](#pull-requests)
- [Reporting bugs](#reporting-bugs)
- [Security](#security)
- [Code of conduct](#code-of-conduct)
- [License of contributions](#license-of-contributions)

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

## License of contributions

Couchverse is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE) and is also offered by LemonSlice under separate commercial terms.

By opening a pull request, you confirm that:

1. You have the right to submit the contribution (it's your own work, or your employer has authorized it).
2. You license your contribution to LemonSlice under the PolyForm Noncommercial License 1.0.0 **and** grant LemonSlice a perpetual, worldwide, royalty-free right to also distribute your contribution under any other license LemonSlice uses for the project, including commercial licenses.

This dual-licensing arrangement is what lets LemonSlice offer paid commercial licenses without needing to track down every contributor for sign-off later. If you're not comfortable with that, please open an issue to discuss before submitting code.
