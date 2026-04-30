"""Unit tests for ``mint_agent_token``.

The helper just composes ``livekit.api.AccessToken`` calls, so the tests
decode the resulting JWT (without signature verification — the secret is
a test fixture, not a real LiveKit key) and assert the claims and grants
are exactly what the spike validated against LiveKit Cloud.
"""

from __future__ import annotations

import datetime

import jwt
import pytest

from podcast_commentary.api import livekit_tokens
from podcast_commentary.api.livekit_tokens import (
    AGENT_TOKEN_TTL,
    SESSION_MAX_DURATION,
    mint_agent_token,
)


@pytest.fixture(autouse=True)
def _livekit_credentials(monkeypatch):
    """Stub credentials so AccessToken doesn't fall through to env vars."""
    monkeypatch.setattr(livekit_tokens.settings, "LIVEKIT_API_KEY", "test-api-key")
    monkeypatch.setattr(livekit_tokens.settings, "LIVEKIT_API_SECRET", "test-api-secret")


def _decode(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


def test_token_includes_identity_and_room():
    token = mint_agent_token("couch-bravo", "agent-persona_a-sess123")
    claims = _decode(token)

    assert claims["sub"] == "agent-persona_a-sess123"
    assert claims["name"] == "agent-persona_a-sess123"
    assert claims["video"]["room"] == "couch-bravo"


def test_token_grants_required_flags():
    token = mint_agent_token("couch-bravo", "agent-persona_a-sess123")
    video = _decode(token)["video"]

    # Acceptance criteria: roomJoin, canPublish, canSubscribe,
    # canPublishData, agent:true.
    assert video["roomJoin"] is True
    assert video["canPublish"] is True
    assert video["canSubscribe"] is True
    assert video["canPublishData"] is True
    assert video["agent"] is True


def test_token_kind_is_agent():
    """``kind=agent`` makes the participant register as
    ``PARTICIPANT_KIND_AGENT``. This is what lets the dispatch dashboard
    / RPC layer correlate the connection with the dispatched job."""
    token = mint_agent_token("couch-bravo", "agent-persona_a-sess123")
    claims = _decode(token)

    assert claims["kind"] == "agent"


def test_token_ttl_exceeds_session_max_with_buffer():
    """TTL must be ≥ session max with a buffer (acceptance criterion)."""
    before = datetime.datetime.now(datetime.timezone.utc)
    token = mint_agent_token("couch-bravo", "agent-persona_a-sess123")
    after = datetime.datetime.now(datetime.timezone.utc)

    claims = _decode(token)
    nbf = datetime.datetime.fromtimestamp(claims["nbf"], tz=datetime.timezone.utc)
    exp = datetime.datetime.fromtimestamp(claims["exp"], tz=datetime.timezone.utc)

    # JWT timestamps are second-precision; allow a 2 s window on each end.
    assert abs((nbf - before).total_seconds()) <= 2
    expected_exp = before + AGENT_TOKEN_TTL
    assert abs((exp - expected_exp).total_seconds()) <= 2

    actual_ttl = exp - nbf
    assert actual_ttl >= SESSION_MAX_DURATION
    assert actual_ttl - SESSION_MAX_DURATION >= datetime.timedelta(minutes=1), (
        "TTL must include a non-trivial buffer over SESSION_MAX_DURATION"
    )

    # Sanity: nbf bracketed by clock samples taken around the call.
    assert before - datetime.timedelta(seconds=2) <= nbf <= after + datetime.timedelta(seconds=2)


def test_token_room_claim_scoped_per_call():
    """Each call must produce a token scoped to the requested room — the
    helper must not leak state between calls."""
    a = _decode(mint_agent_token("couch-alpha", "agent-persona_a-1"))
    b = _decode(mint_agent_token("couch-bravo", "agent-persona_b-1"))

    assert a["video"]["room"] == "couch-alpha"
    assert b["video"]["room"] == "couch-bravo"
    assert a["sub"] == "agent-persona_a-1"
    assert b["sub"] == "agent-persona_b-1"


def test_token_signed_with_configured_secret():
    """Smoke test that the token is actually signable + verifiable with
    the configured secret — guards against an accidental rewrite that
    drops ``with_grants`` or breaks the ``AccessToken`` chain."""
    token = mint_agent_token("couch-bravo", "agent-persona_a-sess123")
    decoded = jwt.decode(
        token,
        key="test-api-secret",
        algorithms=["HS256"],
        options={"verify_signature": True, "verify_exp": True},
    )
    assert decoded["iss"] == "test-api-key"
    assert decoded["video"]["room"] == "couch-bravo"
