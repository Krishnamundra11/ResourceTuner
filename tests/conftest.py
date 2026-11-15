"""Pytest configuration and environment fixtures for ResourceTuner tests."""

import os
from pathlib import Path

# Ensure CrewAI and user data directories point inside the workspace (writable sandbox)
repo_root = Path(__file__).resolve().parents[1]
local_share = repo_root / ".local-share"
local_share.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("XDG_DATA_HOME", str(local_share))
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
