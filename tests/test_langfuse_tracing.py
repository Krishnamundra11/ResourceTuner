"""Unit tests for Langfuse tracing integration."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from resourcetuner.telemetry.langfuse_tracer import (
    flush_traces,
    get_langfuse_client,
    is_langfuse_configured,
    observe_agent,
    observe_cycle,
    observe_tool,
    record_llm_generation,
)


def test_langfuse_unconfigured_by_default():
    """Verify safe fallback to disabled/no-op state when no keys are in env."""
    with patch.dict(os.environ, {}, clear=True):
        assert not is_langfuse_configured()
        # Functions should execute safely without throwing exceptions
        record_llm_generation(
            name="test_call",
            provider="groq",
            model="openai/gpt-oss-120b",
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            latency_ms=120,
            outcome="ok",
        )
        flush_traces()


def test_observe_decorators_passthrough():
    """Verify decorators wrap and execute functions transparently."""
    @observe_agent("mock_supervisor")
    def run_supervisor(workload: str) -> dict:
        return {"workload": workload, "delegated": ["cpu"]}

    @observe_tool("mock_gate")
    def run_gate(action: dict) -> bool:
        return True

    @observe_cycle("mock_cycle")
    def run_cycle() -> str:
        return "completed"

    assert run_supervisor("build") == {"workload": "build", "delegated": ["cpu"]}
    assert run_gate({"tool": "cpu_weight"}) is True
    assert run_cycle() == "completed"


def test_record_llm_generation_with_mock_client():
    """Verify record_llm_generation creates valid observation parameters."""
    mock_client = MagicMock()
    mock_obs = MagicMock()
    mock_client.start_observation.return_value = mock_obs

    with patch("resourcetuner.telemetry.langfuse_tracer.get_langfuse_client", return_value=mock_client):
        record_llm_generation(
            name="crew:cpu:gpt-oss",
            provider="groq",
            model="openai/gpt-oss-120b",
            prompt={"messages": [{"role": "user", "content": "analyze"}]},
            output={"contention": True},
            usage={"prompt_tokens": 500, "completion_tokens": 150},
            latency_ms=350,
            outcome="ok",
            degraded=False,
            metadata={"agent": "cpu_specialist"},
        )

        mock_client.start_observation.assert_called_once()
        args, kwargs = mock_client.start_observation.call_args
        assert kwargs["name"] == "crew:cpu:gpt-oss"
        assert kwargs["as_type"] == "generation"
        assert kwargs["model"] == "openai/gpt-oss-120b"
        assert kwargs["usage_details"]["input"] == 500
        assert kwargs["usage_details"]["output"] == 150
        assert kwargs["usage_details"]["total"] == 650
        assert kwargs["metadata"]["provider"] == "groq"
        assert kwargs["metadata"]["degraded"] is False

        mock_obs.end.assert_called_once()
