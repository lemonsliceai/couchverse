"""Persona manifest endpoint.

Single source of truth for the active persona lineup the Chrome
extension renders. The server resolves ``PERSONAS`` and reads each
preset's ``PersonaConfig`` (label, descriptor, preview filename); the
client renders DOM slots from that — no persona names baked into HTML.

Reused by ``POST /api/sessions`` so the in-session avatar stack
re-renders from the *authoritative* per-session lineup, not a stale
cached copy.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from podcast_commentary.agent.fox_config import _resolve_persona_names, load_config

router = APIRouter()


class PersonaManifestEntry(BaseModel):
    name: str
    label: str
    descriptor: str
    preview_filename: str
    role: Literal["primary", "secondary"]


class PersonaManifestResponse(BaseModel):
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
    for name in names:
        cfg = load_config(name)
        entries.append(
            PersonaManifestEntry(
                name=name,
                label=cfg.persona.speaker_label or name,
                descriptor=cfg.persona.descriptor,
                preview_filename=cfg.persona.preview_filename or f"{name}_2x3.png",
                role="primary" if name == primary else "secondary",
            )
        )
    return entries


@router.get("/api/personas", response_model=PersonaManifestResponse)
async def get_personas_route() -> PersonaManifestResponse:
    return PersonaManifestResponse(personas=build_persona_manifest())
