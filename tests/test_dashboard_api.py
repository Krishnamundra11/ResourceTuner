"""Step 1 (backend event layer): event store, run registry, API, SSE, and read-only history.

Nothing here starts a container, calls an LLM or touches a cgroup: `RunManager` is given a fake
slice factory, so the registry/event/SSE behaviour is tested on its own. The real pipeline's
safety pathway is covered by tests/test_step9_vertical_slice.py and tests/test_safety_gate.py;
what this file adds is the proof that the only way the API can change anything is through that
pathway.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resourcetuner.dashboard import history as H
from resourcetuner.dashboard.main import create_app
from resourcetuner.events.store import EventStore, RunEventSink, scrub
from resourcetuner.pipeline import stages as S
from resourcetuner.pipeline.runner import (
    MAX_CYCLES, InvalidRunRequest, RunConflict, RunManager, RunNotFound, available_scenarios,
)


# --- fakes -------------------------------------------------------------------------------------

class FakeSlice:
    """Emits a deterministic stage sequence; honours cancellation; records cleanup."""

    def __init__(self, cfg, sink, cancel, *, fail: bool = False, hang: bool = False):
        self.cfg, self.sink, self.cancel = cfg, sink, cancel
        self.fail, self.hang = fail, hang
        self.cleaned = False

    async def arun(self) -> dict:
        summary = {"cycles_done": 0, "status": S.RUN_COMPLETED, "cycles": []}
        try:
            self.sink.stage_start(S.SCENARIO_START, summary="starting")
            self.sink.stage_done(S.SCENARIO_START, summary="2 containers")
            for cycle in range(1, self.cfg.cycles + 1):
                while self.hang and not self.cancel.is_set():
                    await asyncio.sleep(0.01)
                if self.cancel.is_set():
                    raise _Cancelled()
                if self.fail:
                    raise RuntimeError("pipeline blew up")
                for stage in (S.BASELINE, S.TRIGGER, S.SUPERVISOR, S.DECISION, S.SAFETY_GATE):
                    self.sink.stage_start(stage, cycle=cycle)
                    self.sink.stage_done(stage, cycle=cycle, summary=f"{stage} ok")
                self.sink.emit(S.CYCLE_SUMMARY, S.COMPLETED, cycle=cycle, summary=f"cycle {cycle}: completed")
                summary["cycles_done"] = cycle
        except _Cancelled:
            summary["status"] = S.RUN_CANCELLED
            summary["error"] = "stop requested"
        except Exception as e:  # noqa: BLE001
            summary["status"] = S.RUN_FAILED
            summary["error"] = f"{type(e).__name__}: {e}"
        finally:
            self.cleaned = True
            summary["cleanup"] = {"rolled_back": [], "containers_removed": 2}
            status = {S.RUN_COMPLETED: S.COMPLETED, S.RUN_CANCELLED: S.SKIPPED,
                      S.RUN_FAILED: S.FAILED}[summary["status"]]
            self.sink.emit(S.RUN_SUMMARY, status, summary=summary["status"], payload=summary)
        return summary


class _Cancelled(Exception):
    pass


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "events.db")


def make_manager(store: EventStore, **slice_kw) -> RunManager:
    made: list[FakeSlice] = []

    def factory(cfg, sink, cancel):
        sl = FakeSlice(cfg, sink, cancel, **slice_kw)
        made.append(sl)
        return sl

    m = RunManager(store, slice_factory=factory)
    m.made = made           # type: ignore[attr-defined]
    return m


def client_for(store: EventStore, manager: RunManager) -> TestClient:
    return TestClient(create_app(None, store=store, manager=manager))


def wait_until(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --- event store --------------------------------------------------------------------------------

def test_events_are_appended_in_order_with_a_monotonic_cursor(store: EventStore):
    run = store.create_run("cli", scenario="build_vs_indexer")
    seqs = [store.append(run, s, S.COMPLETED, cycle=1, summary=s) for s in S.CYCLE_STAGES]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    got = store.events(run_id=run)
    assert [e["stage"] for e in got] == list(S.CYCLE_STAGES)
    assert [e["seq"] for e in got] == seqs
    assert store.last_seq() == seqs[-1]


def test_after_seq_is_exclusive_so_a_reader_never_sees_an_event_twice(store: EventStore):
    run = store.create_run("cli")
    for i in range(10):
        store.append(run, S.BASELINE, S.COMPLETED, cycle=i)
    first = store.events(run_id=run, limit=4)
    second = store.events(run_id=run, after_seq=first[-1]["seq"], limit=4)
    third = store.events(run_id=run, after_seq=second[-1]["seq"], limit=100)
    seen = [e["seq"] for e in first + second + third]
    assert len(seen) == 10 and seen == sorted(set(seen))


def test_unknown_stage_or_status_is_refused(store: EventStore):
    run = store.create_run("cli")
    with pytest.raises(ValueError, match="unknown stage"):
        store.append(run, "not_a_stage", S.COMPLETED)
    with pytest.raises(ValueError, match="unknown status"):
        store.append(run, S.BASELINE, "not_a_status")


def test_payloads_are_scrubbed_of_secrets(store: EventStore):
    run = store.create_run("cli", params={"GROQ_API_KEY": "gsk_realkey", "scenario": "x"})
    store.append(run, S.SUPERVISOR, S.COMPLETED,
                 payload={"provider": "groq", "api_key": "gsk_secret",
                          "nested": [{"Authorization": "Bearer abc", "model": "gpt-oss"}]})
    ev = store.events(run_id=run)[0]
    blob = json.dumps(ev["payload"])
    assert "gsk_secret" not in blob and "Bearer abc" not in blob
    assert ev["payload"]["provider"] == "groq" and ev["payload"]["nested"][0]["model"] == "gpt-oss"
    assert "gsk_realkey" not in json.dumps(store.get_run(run)["params"])


def test_scrub_keeps_shape_and_bounds_size():
    out = scrub({"token": "x", "keep": 1, "big": "y" * 20000, "deep": {"api-key": "k", "ok": True}})
    assert out["token"] == "[redacted]" and out["keep"] == 1 and out["deep"]["ok"] is True
    assert out["deep"]["api-key"] == "[redacted]" and len(out["big"]) < 20000


def test_oversized_payload_is_replaced_not_dropped(store: EventStore):
    run = store.create_run("cli")
    store.append(run, S.DIAGNOSTICS, S.COMPLETED, payload={"findings": [{"x": "y" * 7000} for _ in range(40)]})
    ev = store.events(run_id=run)[0]
    assert ev["payload"].get("_truncated") is True and ev["status"] == S.COMPLETED


def test_sink_records_duration_only_after_the_stage_finishes(store: EventStore):
    sink = RunEventSink(store, store.create_run("cli"))
    sink.stage_start(S.BASELINE, cycle=1)
    time.sleep(0.02)
    sink.stage_done(S.BASELINE, cycle=1)
    start, done = store.events(run_id=sink.run_id)
    assert start["status"] == S.RUNNING and start["duration_ms"] is None
    assert done["status"] == S.COMPLETED and done["duration_ms"] >= 10


# --- scenarios ----------------------------------------------------------------------------------

def test_scenario_catalogue_matches_the_repo_and_flags_intervention_support():
    scenarios = {s["name"]: s for s in available_scenarios()}
    repo_names = {p.stem for p in (Path(__file__).resolve().parents[1] / "docker" / "scenarios").glob("*.yaml")}
    assert set(scenarios) == repo_names
    # All-foreground scenarios cannot be acted on: the gate protects every container.
    assert scenarios["build_only"]["intervention"] == "none"
    assert scenarios["build_and_test"]["intervention"] == "none"
    assert scenarios["ram_pressure"]["intervention"] == "observation_only"
    assert scenarios["io_pressure"]["intervention"] == "pause_only"
    assert scenarios["build_vs_indexer"]["intervention"] == "supported"
    assert "high" in scenarios["build_vs_indexer"]["levels"]


def test_scenarios_endpoint_schema(store: EventStore):
    c = client_for(store, make_manager(store))
    body = c.get("/api/scenarios").json()
    assert body["max_cycles"] == MAX_CYCLES
    for s in body["scenarios"]:
        assert {"name", "description", "levels", "containers", "intervention", "intervention_note"} <= set(s)


# --- run lifecycle ------------------------------------------------------------------------------

def test_run_completes_and_records_status_and_events(store: EventStore):
    m = make_manager(store)
    c = client_for(store, m)
    r = c.post("/api/runs", json={"scenario": "build_vs_indexer", "level": "high", "cycles": 2})
    assert r.status_code == 201
    run_id = r.json()["run_id"]
    assert wait_until(lambda: c.get(f"/api/runs/{run_id}").json()["status"] == S.RUN_COMPLETED)

    run = c.get(f"/api/runs/{run_id}").json()
    assert run["cycles_done"] == 2 and run["cycles_requested"] == 2 and run["cycles_seen"] == [1, 2]
    events = c.get(f"/api/runs/{run_id}/events").json()["events"]
    assert [e["stage"] for e in events][:3] == [S.SCENARIO_START, S.SCENARIO_START, S.BASELINE]
    assert events[-1]["stage"] == S.RUN_SUMMARY and events[-1]["status"] == S.COMPLETED
    assert m.made[0].cleaned is True


def test_second_start_while_one_is_active_is_a_conflict(store: EventStore):
    m = make_manager(store, hang=True)
    c = client_for(store, m)
    first = c.post("/api/runs", json={"cycles": 1}).json()["run_id"]
    assert wait_until(lambda: m.store.get_run(first)["status"] == S.RUN_RUNNING)

    second = c.post("/api/runs", json={"cycles": 1})
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "run_in_progress" and detail["active"]["run_id"] == first
    assert len(c.get("/api/runs").json()["runs"]) == 1          # the rejected start created nothing

    c.post(f"/api/runs/{first}/stop")
    assert wait_until(lambda: m.store.get_run(first)["status"] == S.RUN_CANCELLED)


def test_concurrent_starts_from_threads_produce_exactly_one_run(store: EventStore):
    m = make_manager(store, hang=True)
    results: list[object] = []
    barrier = threading.Barrier(6)

    def attempt():
        barrier.wait()
        try:
            results.append(m.start(scenario="build_vs_indexer", level="high", cycles=1)["run_id"])
        except RunConflict as e:
            results.append(e)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(timeout=20) for t in threads]

    started = [r for r in results if isinstance(r, str)]
    conflicts = [r for r in results if isinstance(r, RunConflict)]
    assert len(started) == 1 and len(conflicts) == 5
    assert len(store.list_runs(limit=50)) == 1
    m.stop(started[0])
    assert wait_until(lambda: store.get_run(started[0])["status"] == S.RUN_CANCELLED)


def test_stop_cancels_cleans_up_and_is_idempotent(store: EventStore):
    m = make_manager(store, hang=True)
    c = client_for(store, m)
    run_id = c.post("/api/runs", json={"cycles": 3}).json()["run_id"]
    assert wait_until(lambda: store.get_run(run_id)["status"] == S.RUN_RUNNING)

    stop = c.post(f"/api/runs/{run_id}/stop").json()
    assert stop["stop_requested"] is True
    assert wait_until(lambda: store.get_run(run_id)["status"] == S.RUN_CANCELLED)

    run = store.get_run(run_id)
    assert run["cycles_done"] == 0 and run["error"] == "stop requested" and run["ended_at"]
    assert m.made[0].cleaned is True                                  # cleanup ran on the cancel path
    last = store.events(run_id=run_id)[-1]
    assert last["stage"] == S.RUN_SUMMARY and last["status"] == S.SKIPPED
    assert last["payload"]["cleanup"]["containers_removed"] == 2

    again = c.post(f"/api/runs/{run_id}/stop").json()
    assert again["stop_requested"] is False and "already finished" in again["detail"]


def test_a_failing_pipeline_is_recorded_as_failed_with_the_error(store: EventStore):
    m = make_manager(store, fail=True)
    c = client_for(store, m)
    run_id = c.post("/api/runs", json={"cycles": 1}).json()["run_id"]
    assert wait_until(lambda: store.get_run(run_id)["status"] == S.RUN_FAILED)
    run = store.get_run(run_id)
    assert "pipeline blew up" in run["error"] and run["cycles_done"] == 0
    assert store.events(run_id=run_id)[-1]["status"] == S.FAILED
    assert m.made[0].cleaned is True                                  # cleanup ran on the failure path
    # A finished run must not block the next one.
    assert c.post("/api/runs", json={"cycles": 1}).status_code == 201


def test_a_crash_outside_the_pipeline_still_marks_the_run_failed(store: EventStore):
    def boom(cfg, sink, cancel):
        raise OSError("docker socket missing")

    m = RunManager(store, slice_factory=boom)
    run_id = m.start(cycles=1)["run_id"]
    assert wait_until(lambda: store.get_run(run_id)["status"] == S.RUN_FAILED)
    assert "docker socket missing" in store.get_run(run_id)["error"]


def test_runs_interrupted_by_a_restart_are_reaped_not_left_active(store: EventStore):
    orphan = store.create_run("dashboard", scenario="build_vs_indexer")
    store.update_run(orphan, status=S.RUN_RUNNING)
    assert store.active_run()["run_id"] == orphan

    m = RunManager(store, slice_factory=lambda *a: FakeSlice(*a))   # a fresh process
    assert store.get_run(orphan)["status"] == S.RUN_FAILED
    assert "interrupted" in store.get_run(orphan)["error"]
    assert store.active_run() is None
    assert m.start(cycles=1)["run_id"] != orphan                    # the orphan does not block new runs


@pytest.mark.parametrize("body,field", [
    ({"scenario": "no_such_scenario"}, "unknown scenario"),
    ({"scenario": "build_vs_indexer", "level": "extreme"}, "no level"),
])
def test_invalid_run_requests_are_rejected_with_422(store: EventStore, body, field):
    c = client_for(store, make_manager(store))
    r = c.post("/api/runs", json=body)
    assert r.status_code == 422 and field in r.json()["detail"]["message"]
    assert store.list_runs() == []


def test_cycles_outside_the_guard_are_rejected(store: EventStore):
    c = client_for(store, make_manager(store))
    assert c.post("/api/runs", json={"cycles": MAX_CYCLES + 1}).status_code == 422
    assert c.post("/api/runs", json={"cycles": 0}).status_code == 422
    with pytest.raises(InvalidRunRequest):
        make_manager(store).start(cycles=MAX_CYCLES + 1)


def test_missing_run_is_404(store: EventStore):
    c = client_for(store, make_manager(store))
    assert c.get("/api/runs/nope").status_code == 404
    assert c.post("/api/runs/nope/stop").status_code == 404
    assert c.get("/api/runs/nope/events").status_code == 404
    with pytest.raises(RunNotFound):
        make_manager(store).get("nope")


def test_run_list_is_paginated(store: EventStore):
    for i in range(7):
        store.create_run("cli", scenario=f"s{i}")
    c = client_for(store, make_manager(store))
    page = c.get("/api/runs?limit=3&offset=0").json()
    assert page["total"] == 7 and len(page["runs"]) == 3 and page["limit"] == 3
    assert [r["run_id"] for r in page["runs"]] != [r["run_id"] for r in
                                                    c.get("/api/runs?limit=3&offset=3").json()["runs"]]


def test_run_events_can_be_filtered_by_cycle_and_cursor(store: EventStore):
    m = make_manager(store)
    c = client_for(store, m)
    run_id = c.post("/api/runs", json={"cycles": 2}).json()["run_id"]
    assert wait_until(lambda: store.get_run(run_id)["status"] == S.RUN_COMPLETED)

    cycle2 = c.get(f"/api/runs/{run_id}/events?cycle=2").json()["events"]
    assert cycle2 and {e["cycle"] for e in cycle2} == {2}
    first = c.get(f"/api/runs/{run_id}/events?limit=3").json()
    rest = c.get(f"/api/runs/{run_id}/events?after_seq={first['next_seq']}").json()
    assert all(e["seq"] > first["next_seq"] for e in rest["events"])


# --- SSE ----------------------------------------------------------------------------------------

def read_sse(client: TestClient, url: str, want: int, headers: dict | None = None, timeout: float = 20.0):
    """Collect `want` SSE data frames, then disconnect."""
    frames, comments = [], []
    with client.stream("GET", url, headers=headers or {}, timeout=timeout) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        last_id = None
        for line in r.iter_lines():
            if line.startswith(":"):
                comments.append(line)
            elif line.startswith("id: "):
                last_id = int(line[4:])
            elif line.startswith("data: "):
                frames.append((last_id, json.loads(line[6:])))
                if len(frames) >= want:
                    break
    return frames, comments


def test_sse_streams_recorded_events_in_order_with_seq_as_the_event_id(store: EventStore):
    run_id = store.create_run("cli")
    for stage in (S.SCENARIO_START, S.BASELINE, S.TRIGGER):
        store.append(run_id, stage, S.COMPLETED, cycle=1, summary=stage)
    c = client_for(store, make_manager(store))

    frames, comments = read_sse(c, "/api/events/stream", want=3)
    assert [f[1]["stage"] for f in frames] == [S.SCENARIO_START, S.BASELINE, S.TRIGGER]
    assert [f[0] for f in frames] == [f[1]["seq"] for f in frames]      # id: == seq
    assert [f[0] for f in frames] == sorted(f[0] for f in frames)
    assert any("connected" in x for x in comments)


def test_sse_reconnect_with_last_event_id_resumes_without_gaps_or_duplicates(store: EventStore):
    run_id = store.create_run("cli")
    seqs = [store.append(run_id, S.BASELINE, S.COMPLETED, cycle=i, summary=f"c{i}") for i in range(1, 6)]
    c = client_for(store, make_manager(store))

    first, _ = read_sse(c, "/api/events/stream", want=2)
    resumed, _ = read_sse(c, "/api/events/stream", want=3,
                          headers={"Last-Event-ID": str(first[-1][0])})
    delivered = [f[0] for f in first] + [f[0] for f in resumed]
    assert delivered == seqs                                            # every event exactly once
    assert len(set(delivered)) == len(delivered)


def test_sse_after_seq_query_param_also_resumes(store: EventStore):
    run_id = store.create_run("cli")
    seqs = [store.append(run_id, S.BASELINE, S.COMPLETED, cycle=i) for i in range(1, 4)]
    c = client_for(store, make_manager(store))
    frames, _ = read_sse(c, f"/api/events/stream?after_seq={seqs[0]}", want=2)
    assert [f[0] for f in frames] == seqs[1:]


def test_sse_can_be_scoped_to_one_run(store: EventStore):
    a, b = store.create_run("cli", scenario="a"), store.create_run("cli", scenario="b")
    store.append(a, S.BASELINE, S.COMPLETED, cycle=1, summary="from-a")
    store.append(b, S.BASELINE, S.COMPLETED, cycle=1, summary="from-b")
    store.append(a, S.TRIGGER, S.COMPLETED, cycle=1, summary="also-a")
    c = client_for(store, make_manager(store))
    frames, _ = read_sse(c, f"/api/events/stream?run_id={a}", want=2)
    assert [f[1]["summary"] for f in frames] == ["from-a", "also-a"]


def test_sse_delivers_events_produced_after_the_client_connected(store: EventStore):
    m = make_manager(store)
    c = client_for(store, m)
    started: dict[str, str] = {}

    def start_later():
        time.sleep(0.4)
        started["run_id"] = m.start(cycles=1)["run_id"]

    threading.Thread(target=start_later, daemon=True).start()
    frames, _ = read_sse(c, "/api/events/stream", want=4, timeout=25.0)
    assert [f[1]["stage"] for f in frames][:2] == [S.SCENARIO_START, S.SCENARIO_START]
    assert wait_until(lambda: started.get("run_id") and
                      store.get_run(started["run_id"])["status"] == S.RUN_COMPLETED)


def test_sse_bad_last_event_id_falls_back_instead_of_failing(store: EventStore):
    run_id = store.create_run("cli")
    store.append(run_id, S.BASELINE, S.COMPLETED, cycle=1)
    c = client_for(store, make_manager(store))
    frames, _ = read_sse(c, "/api/events/stream", want=1, headers={"Last-Event-ID": "not-a-number"})
    assert len(frames) == 1


# --- history is read-only ------------------------------------------------------------------------

def imported_modules(module) -> set[str]:
    """Every module name this file imports, from its AST (comments and docstrings ignored)."""
    import ast
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


ACTING_MODULES = ("resourcetuner.pipeline.runner", "resourcetuner.pipeline.slice",
                  "resourcetuner.controller", "resourcetuner.mcp_server", "resourcetuner.agents",
                  "resourcetuner.safety", "docker", "subprocess", "os")


def test_history_module_cannot_act_by_construction():
    """Its imports contain nothing that can start a run, change a cgroup or call an LLM."""
    imported = imported_modules(H)
    offenders = [m for m in imported if any(m == a or m.startswith(a + ".") for a in ACTING_MODULES)]
    assert offenders == [], f"history.py must not import {offenders}"
    assert imported <= {"__future__", "__future__.annotations", "json", "sqlite3", "pathlib",
                        "pathlib.Path", "typing", "typing.Any",
                        "resourcetuner.pipeline", "resourcetuner.pipeline.stages"}


def test_reading_history_changes_nothing(store: EventStore):
    m = make_manager(store)
    c = client_for(store, m)
    before = (store.list_runs(limit=100), store.last_seq(), H.policies(), H.actions())
    for path in ("/api/history/cycles", "/api/history/cycles?limit=1&offset=1",
                 "/api/history/actions", "/api/history/policies"):
        assert c.get(path).status_code == 200
    cycles = c.get("/api/history/cycles").json()["cycles"]
    if cycles:
        assert c.get(f"/api/history/cycles/{cycles[0]['cycle']}").status_code == 200
    after = (store.list_runs(limit=100), store.last_seq(), H.policies(), H.actions())
    assert before == after                                   # no runs, no events, no policy writes


def test_history_endpoints_report_read_only_and_paginate(store: EventStore):
    c = client_for(store, make_manager(store))
    for path in ("/api/history/cycles", "/api/history/actions", "/api/history/policies"):
        body = c.get(path).json()
        assert body["read_only"] is True and "total" in body
    page = c.get("/api/history/cycles?limit=1").json()
    assert page["limit"] == 1 and len(page["cycles"]) <= 1


def test_missing_historical_cycle_is_404(store: EventStore):
    c = client_for(store, make_manager(store))
    assert c.get("/api/history/cycles/999999").status_code == 404


def test_legacy_rows_are_mapped_without_inventing_evidence():
    """A summary-only row must not pretend it has per-stage evidence."""
    row = {
        "cycle": 1, "timestamp": 1.0, "duration_s": 131.0,
        "trigger": {"metric": "host.cpu_psi", "value": 64.5, "threshold": 15.6},
        "warm_up_build": {"duration_s": 64.9, "iteration": 1},
        "before_build": {"duration_s": 38.97, "iteration": 2},
        "after_build": {"duration_s": 32.92, "iteration": 3},
        "delegation": {"resources": ["cpu"], "reason": "psi 64.5"},
        "candidates": [{"tool": "set_container_cpu_weight"}],
        "decision": {"chosen_action": {"tool": "set_container_cpu_weight",
                                       "target_id": "container:rt-x-indexer-1", "weight": 50},
                     "reasoning": "severe contention"},
        "execution": {"status": "applied", "knob": "cpu.weight", "old_value": 100, "new_value": 50},
        "evaluation": {"verdict": "IMPROVED", "improvement_pct": 15.52, "rolled_back": False},
        "meta": {"promote": False, "reasoning": "sample_count=1 < 10"},
        "indexer_post_action_weights": {"container:rt-x-indexer-1": 50},
    }
    by_stage = {s["stage"]: s for s in H.legacy_cycle_stages(row)}
    # Never persisted by the Step 9 logger -> must say so, not guess.
    assert by_stage[S.CLASSIFY]["status"] == S.EVIDENCE_UNAVAILABLE
    assert by_stage[S.DIAGNOSTICS]["status"] == S.EVIDENCE_UNAVAILABLE
    assert "not persisted" in by_stage[S.DIAGNOSTICS]["summary"]
    assert by_stage[S.POLICY]["status"] == S.EVIDENCE_UNAVAILABLE
    # Proven by the row -> reported as recorded, with no fabricated timing.
    assert by_stage[S.BASELINE]["status"] == S.COMPLETED and "38.97" in by_stage[S.BASELINE]["summary"]
    assert by_stage[S.SAFETY_GATE]["status"] == S.COMPLETED
    assert by_stage[S.CONTROLLER]["status"] == S.COMPLETED and "100 -> 50" in by_stage[S.CONTROLLER]["summary"]
    assert by_stage[S.EVALUATION]["status"] == S.COMPLETED and "IMPROVED" in by_stage[S.EVALUATION]["summary"]
    assert all(s["ts"] is None and s["duration_ms"] is None for s in by_stage.values())
    assert all(s["derived_from"] == "cycle_summary" for s in by_stage.values())


def test_legacy_row_with_findings_shows_them_instead_of_evidence_unavailable():
    """Reported finding: rows predating 2026-10-25's fix never wrote `findings`, so
    `legacy_cycle_stages` always claimed diagnostics evidence_unavailable — even once the
    pipeline started persisting `findings` on every new row. A row that actually HAS findings
    must show them as real evidence, never blanket-pessimism."""
    row_with_findings = {
        "cycle": 6,
        "findings": [
            {"agent": "cpu", "diagnosis": {"contention": True}, "check_ok": True,
             "check_issues": [], "actionable": True, "provenance": {"provider": "groq"}},
            {"agent": "ram", "diagnosis": {"contention": False}, "check_ok": True,
             "check_issues": [], "actionable": False, "provenance": {"provider": "groq"}},
        ],
    }
    diag = {s["stage"]: s for s in H.legacy_cycle_stages(row_with_findings)}[S.DIAGNOSTICS]
    assert diag["status"] == S.COMPLETED
    assert diag["payload"]["findings"] == row_with_findings["findings"]
    assert "cpu" in diag["summary"] and "contention" in diag["summary"]

    row_with_no_specialist = {"cycle": 7, "findings": []}
    diag2 = {s["stage"]: s for s in H.legacy_cycle_stages(row_with_no_specialist)}[S.DIAGNOSTICS]
    assert diag2["status"] == S.SKIPPED            # key present (empty list), not "unavailable"
    assert diag2["status"] != S.EVIDENCE_UNAVAILABLE

    row_missing_key = {"cycle": 8}                 # a true pre-fix row: no "findings" key at all
    diag3 = {s["stage"]: s for s in H.legacy_cycle_stages(row_missing_key)}[S.DIAGNOSTICS]
    assert diag3["status"] == S.EVIDENCE_UNAVAILABLE


def test_legacy_rejected_and_no_action_rows_are_represented():
    rejected = H.legacy_cycle_stages({
        "cycle": 4, "decision": {"chosen_action": {"tool": "set_container_cpu_weight",
                                                   "target_id": "container:x", "weight": 1}},
        "execution": {"status": "rejected", "reasons": ["bounds: cpu.weight 1 outside [10, 1000]"]}})
    by_stage = {s["stage"]: s for s in rejected}
    assert by_stage[S.SAFETY_GATE]["status"] == S.REJECTED and "bounds" in by_stage[S.SAFETY_GATE]["summary"]
    assert by_stage[S.CONTROLLER]["status"] == S.SKIPPED
    assert by_stage[S.POST_MEASURE]["status"] == S.SKIPPED
    assert by_stage[S.EVALUATION]["status"] == S.SKIPPED

    no_action = {s["stage"]: s for s in H.legacy_cycle_stages({
        "cycle": 2, "decision": {"chosen_action": None, "reasoning": "no candidates"},
        "execution": {"status": "no_action"}, "candidates": []})}
    assert no_action[S.DECISION]["status"] == S.SKIPPED
    assert no_action[S.SAFETY_GATE]["status"] == S.SKIPPED


def test_real_legacy_log_maps_cleanly_if_present():
    """The five recorded Step 9 cycles must render without inventing anything."""
    body = H.legacy_cycles(limit=100)
    if body["total"] == 0:
        pytest.skip("no historical cycles recorded on this machine")
    for summary in body["cycles"]:
        detail = H.legacy_cycle_detail(summary["cycle"])
        assert detail["read_only"] is True
        stages = {s["stage"]: s["status"] for s in detail["stages"]}
        assert stages[S.DIAGNOSTICS] == S.EVIDENCE_UNAVAILABLE      # never persisted in Step 9
        assert set(stages) <= set(S.ALL_STAGES)
        assert all(st in S.STATUSES for st in stages.values())


# --- the API cannot bypass the safety pathway ------------------------------------------------------

def test_no_endpoint_can_change_resources_except_through_the_pipeline():
    """Every mutating route is a run operation; nothing exposes a tool, a cgroup or a shell."""
    app = create_app(None)
    mutating = []
    for route in app.routes:
        for method in getattr(route, "methods", set()) or set():
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                mutating.append(f"{method} {route.path}")
    # Included routers are lazy in FastAPI 0.141, so check the router we build directly too.
    from resourcetuner.dashboard.api import build_router
    from resourcetuner.events.store import EventStore as ES
    import tempfile
    st = ES(Path(tempfile.mkdtemp()) / "e.db")
    for route in build_router(st, RunManager(st), None).routes:
        for method in getattr(route, "methods", set()) or set():
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                mutating.append(f"{method} {route.path}")
    assert sorted(set(mutating)) == ["POST /api/runs", "POST /api/runs/{run_id}/stop"]


def test_api_module_never_imports_the_controller_or_tool_layer():
    """The API may start a run; it may not reach a container, a cgroup or a shell itself."""
    from resourcetuner.dashboard import api
    imported = imported_modules(api)
    forbidden = ("resourcetuner.controller", "resourcetuner.mcp_server", "resourcetuner.agents",
                 "docker", "subprocess")
    offenders = [m for m in imported if any(m == f or m.startswith(f + ".") for f in forbidden)]
    assert offenders == [], f"api.py must not import {offenders}"
    assert "resourcetuner.pipeline.runner" in imported      # it acts only through the run manager


def test_the_pipeline_the_manager_runs_is_the_one_with_the_safety_gate():
    """The default factory builds the real VerticalSlice, which wires ToolHandlers with a gate."""
    import inspect

    from resourcetuner.pipeline.slice import VerticalSlice
    factory_src = inspect.getsource(RunManager._default_slice)
    assert "return VerticalSlice(config, sink=sink, cancel=cancel)" in factory_src
    setup = inspect.getsource(VerticalSlice._setup)
    assert "ToolHandlers(self.target_source, self.controller, gate=self.gate)" in setup
    assert "SafetyGate(" in setup


# --- serving the built control centre ------------------------------------------------------
#
# The SPA is static files; the risk is not the app but the route that serves it, which takes a
# path from the URL. These tests pin down that it cannot be walked out of the build directory,
# that it never shadows the API, and that an unbuilt frontend fails loudly instead of 404ing.

def test_api_routes_are_not_shadowed_by_the_spa_catch_all(store: EventStore):
    manager = make_manager(store)
    with client_for(store, manager) as c:
        assert c.get("/api/scenarios").status_code == 200
        assert c.get("/api/runs/active").json() == {"active": None}


def test_root_leads_to_the_control_centre_and_the_old_viewer_stays_reachable(store: EventStore):
    from resourcetuner.dashboard.main import SPA
    manager = make_manager(store)
    with client_for(store, manager) as c:
        assert c.get("/legacy").status_code == 200
        root = c.get("/", follow_redirects=False)
        if (SPA / "index.html").is_file():
            assert root.status_code == 307 and root.headers["location"] == "/ui/"
            assert c.get("/ui/").status_code == 200
        else:
            # Not built: the root still serves the developer viewer rather than failing.
            assert root.status_code == 200


@pytest.mark.parametrize("path", [
    "../dashboard.html", "../../__init__.py", "..%2f..%2fmain.py", "/etc/passwd",
    "assets/../../main.py",
])
def test_spa_route_cannot_serve_a_file_outside_the_build_directory(store: EventStore, path: str):
    from resourcetuner.dashboard.main import SPA
    if not (SPA / "index.html").is_file():
        pytest.skip("frontend not built")
    manager = make_manager(store)
    index = (SPA / "index.html").read_bytes()
    with client_for(store, manager) as c:
        r = c.get(f"/ui/{path}")
        # Either the request never resolves to a file, or it falls back to index.html — never
        # to a file from outside the build output.
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            assert r.content == index


def test_unbuilt_ui_says_so_instead_of_pretending(store: EventStore, monkeypatch):
    import resourcetuner.dashboard.main as M
    monkeypatch.setattr(M, "SPA", Path("/nonexistent/app"))
    manager = make_manager(store)
    app = M.create_app(None, store=store, manager=manager)
    with TestClient(app) as c:
        r = c.get("/ui/")
        assert r.status_code == 503
        assert r.json()["detail"]["error"] == "ui_not_built"
        # The API and the old viewer must keep working without the built frontend.
        assert c.get("/api/scenarios").status_code == 200
        assert c.get("/").status_code == 200
