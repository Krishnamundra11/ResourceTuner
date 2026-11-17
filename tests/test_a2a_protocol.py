"""Tests for the official A2A (Agent2Agent) Protocol implementation.

Verifies the 5-step A2A Interaction Lifecycle from Dr. Shaikh's lecture:
1. Agent Discovery: GET /a2a/crew/.well-known/agent-card.json returns valid AgentCard.
2. Task Lifecycle: SUBMITTED -> WORKING -> COMPLETED state transitions.
3. Interoperability: Google ADK Supervisor invokes CrewAI via the A2A Bridge.
4. Backward Compatibility: make_crew_diagnose preserves its exact (resources, evidence) contract.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from resourcetuner.a2a.agent_card import get_crewai_agent_card, get_crewai_agent_card_dict
from resourcetuner.a2a.protocol import A2AInteroperabilityBridge, clear_a2a_history, get_a2a_history
from resourcetuner.agents.adk.crew_bridge import make_crew_diagnose
from resourcetuner.dashboard.main import create_app


@pytest.fixture(autouse=True)
def clean_history():
    clear_a2a_history()
    yield
    clear_a2a_history()


def test_crewai_agent_card_structure():
    """Verify CrewAI AgentCard adheres to the A2A Canonical Data Model (Slides 96, 103-104)."""
    card = get_crewai_agent_card()
    assert card.name == "ResourceTuner CrewAI Diagnostic Crew"
    assert card.version == "1.0.0"
    assert len(card.skills) >= 4

    skill_ids = [s.id for s in card.skills]
    assert "diagnose_resource_contention" in skill_ids
    assert "cpu_contention_diagnosis" in skill_ids
    assert "ram_saturation_diagnosis" in skill_ids
    assert "io_bandwidth_diagnosis" in skill_ids

    # Verify JSON dict conversion for well-known endpoint
    d = get_crewai_agent_card_dict()
    assert d["name"] == card.name
    assert "skills" in d
    assert len(d["skills"]) >= 4


def test_a2a_discovery_endpoints():
    """Verify HTTP GET /.well-known/agent-card.json returns the digital business card (Slide 92)."""
    app = create_app()
    client = TestClient(app)

    # Test root-level A2A mount
    resp1 = client.get("/a2a/crew/.well-known/agent-card.json")
    assert resp1.status_code == 200
    card1 = resp1.json()
    assert card1["name"] == "ResourceTuner CrewAI Diagnostic Crew"
    assert len(card1["skills"]) >= 4

    # Test API-level A2A mount
    resp2 = client.get("/api/a2a/crew/.well-known/agent-card.json")
    assert resp2.status_code == 200
    assert resp2.json()["name"] == card1["name"]


def test_a2a_bridge_task_lifecycle():
    """Verify A2A task lifecycle: SUBMITTED -> WORKING -> COMPLETED (Slides 96, 106)."""
    bridge = A2AInteroperabilityBridge()

    def fake_diagnose(resources, evidence):
        return [{"agent": "cpu", "diagnosis": {"contention": True}, "actionable": True}]

    task, findings = bridge.execute_a2a_diagnosis(
        resources=["cpu"],
        evidence={"host": {"cpu_psi": 50.0}},
        diagnose_fn=fake_diagnose,
        context_id="test_cycle_10",
        client_id="adk:supervisor",
    )

    # Verify Task completed
    from a2a.types import TaskState
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert task.id.startswith("a2a_task_")
    assert task.context_id == "test_cycle_10"
    assert len(findings) == 1
    assert findings[0]["agent"] == "cpu"

    # Verify history recorded
    hist = get_a2a_history()
    assert len(hist) == 1
    assert hist[0]["task_id"] == task.id
    assert hist[0]["client_id"] == "adk:supervisor"
    assert hist[0]["state"] == "COMPLETED"
    assert hist[0]["findings_count"] == 1


def test_make_crew_diagnose_backward_compatibility():
    """Verify make_crew_diagnose executes through A2A without breaking caller contract."""
    mock_finding = MagicMock()
    mock_finding.to_dict.return_value = {
        "agent": "cpu",
        "diagnosis": {"contention": True, "culprit_target_ids": ["container:test"]},
        "actionable": True,
    }

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(
            "resourcetuner.agents.adk.crew_bridge.run_diagnostic_crew",
            lambda evidence, router, agents, gap_s, sleep, project: [mock_finding],
        )

        diagnose_fn = make_crew_diagnose()
        res = diagnose_fn(["cpu"], {"evidence_key": "val"})

        assert isinstance(res, list)
        assert len(res) == 1
        assert res[0]["agent"] == "cpu"

        # Verify A2A task was recorded
        hist = get_a2a_history()
        assert len(hist) == 1
        assert hist[0]["state"] == "COMPLETED"
        assert hist[0]["client_id"] == "adk:supervisor"
        assert hist[0]["resources"] == ["cpu"]


def test_a2a_send_message_api():
    """Verify POST /api/a2a/crew/message:send endpoint (Slide 94)."""
    app = create_app()
    client = TestClient(app)

    mock_finding = MagicMock()
    mock_finding.to_dict.return_value = {
        "agent": "ram",
        "diagnosis": {"contention": False},
        "actionable": False,
    }

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(
            "resourcetuner.agents.adk.crew_bridge.run_diagnostic_crew",
            lambda evidence, router, agents, gap_s, sleep, project: [mock_finding],
        )

        resp = client.post(
            "/api/a2a/crew/message:send",
            json={
                "client_id": "test_client",
                "resources": ["ram"],
                "evidence": {"mem_avail_pct": 25.0},
                "context_id": "session_99",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "COMPLETED"
        assert data["task_id"].startswith("a2a_task_")
        assert len(data["findings"]) == 1
        assert data["findings"][0]["agent"] == "ram"
