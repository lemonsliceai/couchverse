"""Fixtures for the dual-room end-to-end integration test.

The harness has three movers, each gated behind ``RUN_DUAL_ROOM_INTEGRATION``:

1. ``livekit_server`` — Docker-launched LiveKit OSS server on port 7880.
   Uses dev API key/secret well-known to LiveKit's official examples
   (devkey/secret) so we don't have to thread credentials through.

2. ``mock_lemonslice_service`` — uvicorn subprocess hosting
   :class:`MockLemonSliceService`. Most tests use the in-process
   :class:`MockAvatarSession` patch instead, but the HTTP service is
   here for cases where we want to assert on a non-Python wire.

3. ``agent_worker`` — the real agent in a subprocess, with the
   :class:`MockAvatarSession` patch applied via a startup hook before
   ``cli.run_app`` runs. Configured via env vars so the test can swap
   models and agent name without touching the source.

PR CI sets neither env nor docker, so all three fixtures skip cleanly
and the rest of the test session is unaffected.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

INTEGRATION_ENV = "RUN_DUAL_ROOM_INTEGRATION"

# LiveKit's well-known dev keys — see ``livekit.yaml`` in the official
# server repo. Hardcoded here intentionally: nothing in the test points
# at any production deployment, and using the documented dev keys keeps
# the test reproducible across CI environments.
DEV_API_KEY = "devkey"
DEV_API_SECRET = "secret"  # noqa: S105 — well-known dev secret, not a credential
DEV_LIVEKIT_PORT = 7880
DEV_LIVEKIT_URL = f"ws://127.0.0.1:{DEV_LIVEKIT_PORT}"


def _running_in_ci_nightly() -> bool:
    return os.environ.get(INTEGRATION_ENV, "").strip() in ("1", "true", "yes", "on")


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def _wait_for_port(host: str, port: int, *, deadline: float) -> None:
    """Block until ``host:port`` accepts TCP, or raise ``TimeoutError``."""
    while time.monotonic() < deadline:
        if _port_open(host, port):
            return
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {host}:{port}")


@pytest.fixture(scope="session", autouse=True)
def _gate_integration_tests() -> None:
    """Skip the entire integration package unless explicitly enabled.

    Autouse + scope=session means pytest evaluates this once before any
    test in this directory runs. If the env gate is off we skip BEFORE
    any other fixture incurs Docker / subprocess overhead.
    """
    if not _running_in_ci_nightly():
        pytest.skip(
            f"Set {INTEGRATION_ENV}=1 to run dual-room end-to-end tests "
            "(default-skipped on PR CI; nightly only).",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
def livekit_server() -> Iterator[str]:
    """Boot a Dockerized LiveKit OSS server and yield its WebSocket URL.

    The container is started with ``--rm`` so it cleans itself up if the
    test crashes hard. Image tag is pinned (LiveKit Cloud's matching
    server release) so a server-side breaking change doesn't silently
    break this nightly test.
    """
    if not _docker_available():
        pytest.skip("Docker is not available; skipping LiveKit-backed integration test.")

    if _port_open("127.0.0.1", DEV_LIVEKIT_PORT):
        # Someone (or a previous test) already has a server up. Reuse it
        # so a hung container doesn't break this run, and so a developer
        # can ``docker run`` LiveKit themselves and iterate locally.
        yield DEV_LIVEKIT_URL
        return

    container_name = f"couchverse-it-livekit-{os.getpid()}"
    proc = subprocess.Popen(  # noqa: S603 — args list, no shell
        [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "-p",
            f"{DEV_LIVEKIT_PORT}:7880",
            "-e",
            f"LIVEKIT_KEYS={DEV_API_KEY}: {DEV_API_SECRET}",
            "livekit/livekit-server:v1.7",
            "--dev",
            "--bind",
            "0.0.0.0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    try:
        _wait_for_port("127.0.0.1", DEV_LIVEKIT_PORT, deadline=time.monotonic() + 30.0)
        yield DEV_LIVEKIT_URL
    finally:
        subprocess.run(  # noqa: S603,S607 — kill is well-bounded
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            check=False,
        )
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


@pytest.fixture(scope="session")
def livekit_credentials(livekit_server: str) -> dict[str, str]:
    """Env vars every other process in this test needs."""
    return {
        "LIVEKIT_URL": livekit_server,
        "LIVEKIT_API_KEY": DEV_API_KEY,
        "LIVEKIT_API_SECRET": DEV_API_SECRET,
    }


@pytest.fixture(scope="session")
def integration_env(livekit_credentials: dict[str, str]) -> dict[str, str]:
    """Common env block for the agent and API subprocesses.

    Test-specific overrides:
      * ``AGENT_NAME`` — collision-free name per test PID so a stale
        worker can't steal a dispatch.
      * ``DATABASE_URL`` is intentionally unset so the agent runs
        without persistence (CLAUDE.md: DB is optional).
    """
    return {
        **livekit_credentials,
        "AGENT_NAME": f"couchverse-it-agent-{os.getpid()}",
        # Empty PERSONAS triggers fox_configs/ auto-discovery — keeps the
        # integration suite from coupling to a specific shipped lineup.
        "PERSONAS": "",
        # Required by the LiveKit plugins even though the integration
        # test stubs them — the plugin constructors verify the env var
        # is present at import time.
        "GROQ_API_KEY": os.environ.get("GROQ_API_KEY", "test-groq-key"),
        "ELEVEN_API_KEY": os.environ.get("ELEVEN_API_KEY", "test-eleven-key"),
        "LEMONSLICE_API_KEY": "mock-lemonslice-key",
        "AVATAR_BASE_URL": "http://127.0.0.1:0/static",
    }


@pytest.fixture
def agent_worker(integration_env: dict[str, str], tmp_path: Path) -> Iterator[subprocess.Popen]:
    """Spawn the agent worker in dev mode with the LemonSlice patch applied.

    Wraps ``main.py`` in a generated bootstrap that runs
    :func:`apply_lemonslice_patch` BEFORE the agent's
    ``from livekit.plugins import lemonslice`` resolves. Without that
    ordering the agent would import the real plugin and the patch would
    only take effect for code paths that re-import — which the agent
    deliberately doesn't do.
    """
    bootstrap = tmp_path / "agent_bootstrap.py"
    bootstrap.write_text(
        textwrap.dedent(
            """
            import sys
            from pathlib import Path

            here = Path(__file__).resolve().parent
            sys.path.insert(0, str(here.parent))

            from tests.integration.mock_lemonslice import apply_lemonslice_patch
            apply_lemonslice_patch()

            from podcast_commentary.agent.main import server
            from livekit.agents import cli
            cli.run_app(server)
            """
        ).strip()
    )

    server_root = Path(__file__).resolve().parents[2]
    env = {**os.environ, **integration_env}
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [env.get("PYTHONPATH"), str(server_root / "src"), str(server_root)])
    )

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(bootstrap), "dev"],
        env=env,
        cwd=str(server_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        # Wait for the worker to register with LiveKit. The worker logs
        # ``registered worker id=...`` once registration completes — the
        # cheapest way to detect it without a control channel is to
        # poll the LiveKit server-side ``ListAgents`` API. Even a 3 s
        # buffer is enough for nightly CI; we cap at 30 s for headroom.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stdout = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
                pytest.fail(f"agent worker exited prematurely (rc={proc.returncode}):\n{stdout}")
            time.sleep(0.5)
            # We can't actually probe the worker over LiveKit's
            # registration endpoint without a server-side admin call.
            # Tests that need worker readiness assert on dispatch
            # delivery (avatar joins) instead.
            break
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.fixture
def mock_lemonslice_service(tmp_path: Path) -> Iterator[str]:
    """Run the in-process FastAPI mock as a uvicorn subprocess.

    Yields the base URL. Most tests don't need this — they use the
    in-process :class:`MockAvatarSession` patch directly — but a few
    smoke-tests want to see the HTTP wire shape. Kept session-scoped so
    repeated parametrised tests don't pay the boot cost each time.
    """
    port = _free_port()
    bootstrap = tmp_path / "mock_lemonslice_run.py"
    bootstrap.write_text(
        textwrap.dedent(
            f"""
            import sys
            from pathlib import Path
            here = Path(__file__).resolve().parent
            sys.path.insert(0, str(here.parent.parent))

            import uvicorn
            from tests.integration.mock_lemonslice import MockLemonSliceService

            service = MockLemonSliceService()
            uvicorn.run(service.app, host="127.0.0.1", port={port}, log_level="warning")
            """
        ).strip()
    )

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(bootstrap)],
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    try:
        _wait_for_port("127.0.0.1", port, deadline=time.monotonic() + 10.0)
        yield f"http://127.0.0.1:{port}"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _free_port() -> int:
    """Return a port the OS just confirmed is free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
