"""End-to-end integration tests for the dual-room architecture.

These tests are GATED on ``RUN_DUAL_ROOM_INTEGRATION=1`` and require a
local Docker daemon, a free LiveKit server port (7880), and outbound
network for any non-stubbed services. Default-skip in PR CI; nightly
CI exports the env var to actually run them.
"""
