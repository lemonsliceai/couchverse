"""Persona manifest endpoint.

Single source of truth for the active persona lineup the Chrome
extension renders. The server resolves ``PERSONAS`` and reads each
preset's ``CharacterConfig`` (label, descriptor, preview filename) plus
``DisplayConfig`` (accent colors, TTS trim gain); the client renders
DOM slots from that — no persona names or accent colors baked into HTML.

Reused by ``POST /api/sessions`` so the in-session avatar stack
re-renders from the *authoritative* per-session lineup, not a stale
cached copy.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from podcast_commentary.agent.persona_config import _resolve_persona_names, load_config

router = APIRouter()


# Bumped when the manifest wire shape changes in a way the extension
# must opt into. The client validates this before parsing the rest of
# the payload so an old extension talking to a newer server can fail
# loudly instead of silently misrendering.
PERSONA_MANIFEST_SCHEMA_VERSION = 1


class PersonaManifestEntry(BaseModel):
    name: str
    label: str
    descriptor: str
    preview_filename: str
    role: Literal["primary", "secondary"]
    accent_color: str
    accent_color_deep: str
    trim_gain: float = 1.0
    slot_order: int


class PersonaManifestResponse(BaseModel):
    schema_version: int = PERSONA_MANIFEST_SCHEMA_VERSION
    personas: list[PersonaManifestEntry]


def build_persona_manifest() -> list[PersonaManifestEntry]:
    """Resolve the active persona lineup into the wire shape.

    Order matches ``PERSONAS`` (first entry is primary). The fallback for
    ``preview_filename`` keeps a persona usable in the UI even when its
    config file forgets to set it — the client's ``onerror`` swap covers
    a genuinely missing asset.
    """
    names = _resolve_persona_names()
    primary = names[0] if names else None
    entries: list[PersonaManifestEntry] = []
    for index, name in enumerate(names):
        cfg = load_config(name)
        entries.append(
            PersonaManifestEntry(
                name=name,
                label=cfg.character.speaker_label or name,
                descriptor=cfg.character.descriptor,
                preview_filename=cfg.character.preview_filename or f"{name}_2x3.png",
                role="primary" if name == primary else "secondary",
                accent_color=cfg.display.accent_color,
                accent_color_deep=cfg.display.accent_color_deep,
                trim_gain=cfg.display.trim_gain,
                slot_order=index,
            )
        )
    return entries


@router.get("/api/personas", response_model=PersonaManifestResponse)
async def get_personas_route() -> PersonaManifestResponse:
    return PersonaManifestResponse(
        schema_version=PERSONA_MANIFEST_SCHEMA_VERSION,
        personas=build_persona_manifest(),
    )
