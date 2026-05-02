import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from podcast_commentary.agent.persona_config import _resolve_persona_names, load_config
from podcast_commentary.api.routes import personas, sessions
from podcast_commentary.core.db import ensure_schema, warm_pool

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")

logger = logging.getLogger("podcast-commentary.api.app")


_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_persona_lineup() -> None:
    """Fail fast at boot if the active persona lineup is unusable.

    A misconfigured ``PERSONAS`` env var or a malformed ``DisplayConfig``
    would otherwise only surface when the extension hits ``/api/personas``
    or a session is minted — by which point the server is already taking
    traffic. Fail loudly during lifespan instead.
    """
    try:
        names = _resolve_persona_names()
    except Exception as exc:
        raise RuntimeError(f"failed to resolve persona lineup: {exc}") from exc

    if not names:
        raise RuntimeError("persona lineup is empty — set PERSONAS or add a preset")

    for name in names:
        try:
            cfg = load_config(name)
        except Exception as exc:
            raise RuntimeError(f"persona {name!r} failed to load: {exc}") from exc

        for field_name in ("accent_color", "accent_color_deep"):
            value = getattr(cfg.display, field_name)
            if not _HEX_COLOR_RE.match(value):
                raise RuntimeError(
                    f"persona {name!r} has invalid display.{field_name}={value!r} — "
                    f"expected a 6-digit hex color like '#7ed4c2'"
                )

    logger.info("Active persona lineup: %s (primary=%s)", names, names[0])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await warm_pool()
    await ensure_schema()
    validate_persona_lineup()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Podcast Commentary API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(sessions.router)
    app.include_router(personas.router)

    # Self-hosted fallback for persona avatar images: when
    # AVATAR_BASE_URL points at the API (vs. the marketing site at
    # couchverse.tv), LemonSlice fetches portraits from this mount.
    # The path mirrors the marketing site's /characters/ convention so
    # AvatarConfig.avatar_url is host-agnostic. fox_2x3.jpg is retained
    # until the in-review Chrome Web Store build is approved.
    characters_dir = Path(__file__).parent.parent / "core" / "data"
    app.mount(
        "/characters",
        StaticFiles(directory=str(characters_dir)),
        name="characters",
    )

    return app


app = create_app()
