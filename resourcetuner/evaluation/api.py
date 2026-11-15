"""FastAPI router for Multi-Agent Evaluation Framework endpoints.

Endpoints:
- GET /api/eval/summary
- GET /api/eval/agents
- GET /api/eval/communication
- GET /api/eval/accuracy
- GET /api/eval/costs
- GET /api/eval/history
- POST /api/eval/benchmark
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from resourcetuner.evaluation.communication import CommunicationEvaluator
from resourcetuner.evaluation.costs import CostEvaluator
from resourcetuner.evaluation.e2e_accuracy import EndToEndAccuracyEvaluator
from resourcetuner.evaluation.metrics import MetricsEngine


def build_evaluation_router() -> APIRouter:
    router = APIRouter(prefix="/eval", tags=["evaluation"])

    metrics_engine = MetricsEngine()
    comm_evaluator = CommunicationEvaluator()
    accuracy_evaluator = EndToEndAccuracyEvaluator()
    cost_evaluator = CostEvaluator()

    @router.get("/summary")
    def get_summary() -> dict:
        """Top-level executive summary of multi-agent system performance."""
        sys_metrics = metrics_engine.compute_system_metrics()
        acc = accuracy_evaluator.compute_accuracy()
        comm = comm_evaluator.evaluate()
        costs = cost_evaluator.compute_workflow_cost()

        # Overall composite system score out of 100
        overall_score = round(
            (
                acc["end_to_end_accuracy"] * 0.40
                + comm["composite_communication_score"] * 0.30
                + sys_metrics["task_completion_rate"] * 0.20
                + sys_metrics["run_success_rate"] * 0.10
            ),
            1,
        )

        return {
            "overall_system_score": overall_score,
            "end_to_end_accuracy": acc["end_to_end_accuracy"],
            "communication_success_rate": comm["composite_communication_score"],
            "task_completion_rate": sys_metrics["task_completion_rate"],
            "total_executions_evaluated": acc["total_evaluated_executions"],
            "average_workflow_latency_s": sys_metrics["average_workflow_latency_s"],
            "average_cost_per_execution_inr": costs["per_cycle_cost_inr"],
            "average_cost_per_execution_usd": costs["per_cycle_cost_usd"],
            "failure_propagation_rate": sys_metrics["failure_propagation_rate"],
            "recovery_rate": sys_metrics["recovery_rate"],
            "safety_gate_pass_rate": sys_metrics["safety_gate_pass_rate"],
        }

    @router.get("/agents")
    def get_agents() -> dict:
        """Individual agent performance metrics."""
        return {"agents": metrics_engine.compute_agent_metrics()}

    @router.get("/communication")
    def get_communication() -> dict:
        """Inter-agent communication analytics, composite scores, and network graph."""
        return comm_evaluator.evaluate()

    @router.get("/accuracy")
    def get_accuracy() -> dict:
        """End-to-End accuracy evaluation out of 100 with rubric details."""
        return accuracy_evaluator.compute_accuracy()

    @router.get("/costs")
    def get_costs() -> dict:
        """Comprehensive financial cost evaluation, breakdowns, and monthly projections."""
        return cost_evaluator.compute_workflow_cost()

    @router.get("/history")
    def get_history() -> dict:
        """Historical accuracy and performance trends across cycles."""
        evals = accuracy_evaluator.evaluate_live_cycles()
        trends = []
        for e in evals:
            trends.append({
                "cycle": e.cycle,
                "score": e.total_score,
                "passed": e.passed,
                "verdict": e.details.get("verdict"),
                "improvement_pct": e.details.get("improvement_pct"),
            })
        return {
            "total_cycles": len(trends),
            "trend_data": trends,
        }

    @router.post("/benchmark")
    def run_benchmark() -> dict:
        """Run evaluation against the predefined benchmark test dataset."""
        suite = accuracy_evaluator.evaluate_benchmark_suite()
        avg_score = round(sum(s.total_score for s in suite) / len(suite), 1) if suite else 100.0
        return {
            "benchmark_score": avg_score,
            "test_cases_count": len(suite),
            "passed_count": sum(1 for s in suite if s.passed),
            "results": [s.to_dict() for s in suite],
        }

    return router
