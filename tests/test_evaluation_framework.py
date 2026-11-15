"""Tests for the Multi-Agent Evaluation Framework.

Verifies:
- MetricsEngine calculations (individual agent and system-level metrics)
- CommunicationEvaluator composite score formula and per-hop stats
- EndToEndAccuracyEvaluator rubric-based scoring (out of 100)
- CostEvaluator calculations and monthly projections
- FastAPI evaluation endpoints (/api/eval/*)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from resourcetuner.dashboard.main import create_app
from resourcetuner.evaluation.communication import CommunicationEvaluator
from resourcetuner.evaluation.costs import CostEvaluator
from resourcetuner.evaluation.e2e_accuracy import EndToEndAccuracyEvaluator
from resourcetuner.evaluation.metrics import MetricsEngine


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


def test_metrics_engine_agent_metrics():
    engine = MetricsEngine()
    metrics = engine.compute_agent_metrics()

    assert "supervisor" in metrics
    assert "cpu_specialist" in metrics
    assert "ram_specialist" in metrics
    assert "io_specialist" in metrics
    assert "decision" in metrics
    assert "meta" in metrics

    # Supervisor assertions
    sup = metrics["supervisor"]
    assert sup["framework"] == "Google ADK"
    assert sup["model"] == "gemini-3.5-flash-lite"
    assert 0.0 <= sup["task_success_rate"] <= 100.0

    # CPU Specialist assertions
    cpu = metrics["cpu_specialist"]
    assert cpu["framework"] == "CrewAI"
    assert cpu["model"] == "groq/openai/gpt-oss-120b"
    assert cpu["hallucination_rate"] == 0.0


def test_metrics_engine_system_metrics():
    engine = MetricsEngine()
    sys_metrics = engine.compute_system_metrics()

    assert "total_runs" in sys_metrics
    assert "task_completion_rate" in sys_metrics
    assert "safety_gate_pass_rate" in sys_metrics
    assert 0.0 <= sys_metrics["task_completion_rate"] <= 100.0
    assert 0.0 <= sys_metrics["safety_gate_pass_rate"] <= 100.0


def test_communication_composite_formula():
    evaluator = CommunicationEvaluator()
    comm = evaluator.evaluate()

    assert "composite_communication_score" in comm
    score = comm["composite_communication_score"]
    assert 0.0 <= score <= 100.0

    # Verify component rates
    assert "overall_delivery_rate" in comm
    assert "overall_parsing_rate" in comm
    assert "overall_preservation_rate" in comm
    assert "overall_consistency_rate" in comm

    # Verify hops list and graph
    assert len(comm["hops"]) >= 4
    assert "graph" in comm
    assert len(comm["graph"]["nodes"]) >= 5
    assert len(comm["graph"]["edges"]) >= 5


def test_e2e_accuracy_rubric():
    evaluator = EndToEndAccuracyEvaluator()
    acc = evaluator.compute_accuracy()

    assert "end_to_end_accuracy" in acc
    assert 0.0 <= acc["end_to_end_accuracy"] <= 100.0
    assert "rubric_averages" in acc

    rubric = acc["rubric_averages"]
    for dim in ("contention_detection", "action_appropriateness", "post_action_outcome", "safety_compliance"):
        assert dim in rubric
        assert 0.0 <= rubric[dim] <= 100.0

    assert acc["total_evaluated_executions"] > 0
    assert len(acc["benchmark_evaluations"]) == 5


def test_cost_evaluator_projections():
    evaluator = CostEvaluator()
    costs = evaluator.compute_workflow_cost()

    assert costs["per_cycle_cost_usd"] > 0.0
    assert costs["per_cycle_cost_inr"] > 0.0
    assert len(costs["agent_cost_breakdown"]) >= 5
    assert len(costs["monthly_projections"]) == 5

    # Check projection for 100 executions
    p100 = costs["monthly_projections"][0]
    assert p100["volume"] == 100
    assert p100["total_cost_usd"] > 0.0

    # Check MCP tools audit
    assert len(costs["mcp_tools_cost_audit"]) >= 4
    for tool in costs["mcp_tools_cost_audit"]:
        assert tool["cost"] == "$0.00"


def test_evaluation_api_endpoints(client):
    endpoints = [
        "/api/eval/summary",
        "/api/eval/agents",
        "/api/eval/communication",
        "/api/eval/accuracy",
        "/api/eval/costs",
        "/api/eval/history",
    ]
    for ep in endpoints:
        res = client.get(ep)
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, dict)

    # Benchmark test run
    bench_res = client.post("/api/eval/benchmark")
    assert bench_res.status_code == 200
    bench_data = bench_res.json()
    assert bench_data["test_cases_count"] == 5
    assert bench_data["benchmark_score"] >= 80.0
