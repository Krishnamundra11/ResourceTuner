"""Run registry: starts, tracks and stops pipeline runs, one at a time.

The dashboard never touches containers or cgroups itself — it asks this manager to run the same
`VerticalSlice` the CLI runs, and watches the events that come out. Guarantees:

* **one run at a time** — a second start returns a conflict, so two experiments can never fight
  over the same containers (`run_scenario.py` enforces the same rule for scenarios);
* **cancellable** — `stop()` sets a flag the pipeline checks between stages; cleanup then rolls
  back any unresolved action and removes the scenario containers;
* **no zombie runs** — runs left `running` by a server restart are reaped as `failed`, because
  their thread is gone and nothing will ever finish them.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

from resourcetuner.events.store import EventStore, RunEventSink, new_run_id
from resourcetuner.pipeline import stages as S
from resourcetuner.pipeline.slice import SliceConfig, VerticalSlice

REPO = Path(__file__).resolve().parents[2]
log = logging.getLogger("resourcetuner.runner")

MAX_CYCLES = 10          # a guard against accidentally launching a very long benchmark
DEFAULT_CYCLES = 1


class RunConflict(RuntimeError):
    """Another run is already active."""

    def __init__(self, active: dict):
        super().__init__(f"run {active['run_id']} is already {active['status']}")
        self.active = active


class RunNotFound(KeyError):
    pass


class InvalidRunRequest(ValueError):
    pass


def available_scenarios() -> list[dict]:
    """Scenarios that actually exist in the repo, with what the UI needs to describe them."""
    import yaml
    sys.path.insert(0, str(REPO / "scripts"))
    import run_scenario as rs

    from resourcetuner.mcp_server.tools import DISABLED_TOOLS

    out = []
    for path in sorted(rs.SCENARIO_DIR.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        try:
            sc = rs.load_scenario(path.stem)
        except rs.ScenarioError as e:
            out.append({"name": path.stem, "error": str(e), "intervention": "unknown"})
            continue
        images = [spec["image"] for spec in sc["containers"].values()]
        workloads = [rs.IMAGES[i]["workload"] for i in images]
        background = [w for w in workloads if w not in ("build", "testing")]
        # What a cycle could actually DO here, given the enabled tool surface.
        if not background:
            intervention, note = "none", "every container is a foreground workload: the Safety Gate protects all of them"
        elif set(background) == {"ram_hog"}:
            intervention, note = "observation_only", "no memory tool exists (this VM has no swap), so RAM pressure is observed, not acted on"
        elif set(background) == {"io_hog"} and "set_container_io_weight" in DISABLED_TOOLS:
            intervention, note = "pause_only", "io.weight is disabled on this host, so the only I/O action is pausing the writer"
        else:
            intervention, note = "supported", "a background target can be deprioritised, throttled or paused"
        out.append({
            "name": path.stem,
            "description": " ".join((raw.get("description") or "").split()),
            "levels": list(raw.get("levels", {})),
            "default_level": raw.get("default_level"),
            "containers": {n: spec["image"] for n, spec in sc["containers"].items()},
            "workloads": sorted(set(workloads)),
            "resource_dimensions": sorted({_dimension(i) for i in images} - {None}),
            "intervention": intervention,
            "intervention_note": note,
        })
    return out


def _dimension(image: str) -> str | None:
    return {"build-sim": "cpu", "test-sim": "cpu", "cpu-hog": "cpu", "indexer-sim": "io",
            "io-hog": "io", "ram-hog": "memory", "idle-sim": None}.get(image)


class RunManager:
    def __init__(self, store: EventStore | None = None, slice_factory=None):
        self.store = store or EventStore()
        self._slice_factory = slice_factory or self._default_slice
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancels: dict[str, threading.Event] = {}
        self.reap_stale_runs()

    # --- startup hygiene ------------------------------------------------------------------

    def reap_stale_runs(self) -> list[str]:
        """Runs still marked active but with no thread here cannot finish: mark them failed."""
        reaped = []
        for run in self.store.list_runs(limit=200):
            if run["status"] in S.RUN_ACTIVE_STATUSES and run["run_id"] not in self._threads:
                self.store.update_run(run["run_id"], status=S.RUN_FAILED, ended_at=time.time(),
                                      error="interrupted (process restarted while the run was active)")
                self.store.append(run["run_id"], S.RUN_SUMMARY, S.FAILED,
                                  summary="interrupted: the server restarted while this run was active")
                reaped.append(run["run_id"])
        if reaped:
            log.warning("Reaped %d stale run(s): %s", len(reaped), ", ".join(reaped))
        return reaped

    # --- queries --------------------------------------------------------------------------

    def active(self) -> dict | None:
        return self.store.active_run()

    def get(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFound(run_id)
        run["thread_alive"] = (t := self._threads.get(run_id)) is not None and t.is_alive()
        run["stop_requested"] = run_id in self._cancels and self._cancels[run_id].is_set()
        return run

    def list(self, limit: int = 50, offset: int = 0, status: str | None = None) -> dict:
        return {"runs": self.store.list_runs(limit=limit, offset=offset, status=status),
                "total": self.store.count_runs(status), "limit": limit, "offset": offset}

    # --- start / stop ---------------------------------------------------------------------

    def _validate(self, scenario: str, level: str | None, cycles: int) -> tuple[str, str | None, int]:
        names = {s["name"]: s for s in available_scenarios()}
        if scenario not in names:
            raise InvalidRunRequest(f"unknown scenario {scenario!r}; known: {sorted(names)}")
        meta = names[scenario]
        if level is not None and level not in meta["levels"]:
            raise InvalidRunRequest(
                f"scenario {scenario!r} has no level {level!r}; levels: {meta['levels'] or 'none'}")
        if level is None and meta["levels"]:
            level = meta["default_level"] or meta["levels"][0]
        if not isinstance(cycles, int) or not 1 <= cycles <= MAX_CYCLES:
            raise InvalidRunRequest(f"cycles must be an integer in [1, {MAX_CYCLES}]")
        return scenario, level, cycles

    def start(self, scenario: str = "build_vs_indexer", level: str | None = None,
              cycles: int = DEFAULT_CYCLES, **extra) -> dict:
        scenario, level, cycles = self._validate(scenario, level, cycles)
        with self._lock:
            active = self.store.active_run()
            if active is not None:
                # A run whose thread died (crash) must not block new runs forever.
                t = self._threads.get(active["run_id"])
                if t is not None and t.is_alive():
                    raise RunConflict(active)
                self.reap_stale_runs()
            run_id = new_run_id("dashboard")
            params = {"scenario": scenario, "level": level, "cycles": cycles, **extra}
            self.store.create_run("dashboard", scenario=scenario, level=level,
                                  cycles_requested=cycles, params=params, run_id=run_id)
            cancel = threading.Event()
            self._cancels[run_id] = cancel
            thread = threading.Thread(target=self._run_thread, args=(run_id, params, cancel),
                                      name=f"run-{run_id}", daemon=True)
            self._threads[run_id] = thread
            thread.start()
        log.info("Started run %s (%s/%s, %d cycle(s))", run_id, scenario, level, cycles)
        return self.get(run_id)

    def stop(self, run_id: str) -> dict:
        run = self.get(run_id)
        if run["status"] not in S.RUN_ACTIVE_STATUSES:
            return {"run_id": run_id, "status": run["status"], "stop_requested": False,
                    "detail": "run is already finished"}
        cancel = self._cancels.get(run_id)
        if cancel is None:                       # active in the DB but not ours: reap it
            self.reap_stale_runs()
            return {"run_id": run_id, "status": self.get(run_id)["status"], "stop_requested": False,
                    "detail": "no live thread for this run; marked failed"}
        cancel.set()
        self.store.append(run_id, S.RUN_SUMMARY, S.RUNNING, summary="stop requested; cleaning up")
        log.info("Stop requested for run %s", run_id)
        return {"run_id": run_id, "status": self.get(run_id)["status"], "stop_requested": True,
                "detail": "the pipeline stops after the current stage, then rolls back and cleans up"}

    # --- the worker -----------------------------------------------------------------------

    def _default_slice(self, config: SliceConfig, sink, cancel) -> VerticalSlice:
        return VerticalSlice(config, sink=sink, cancel=cancel)

    def _run_thread(self, run_id: str, params: dict, cancel: threading.Event) -> None:
        sink = RunEventSink(self.store, run_id)
        self.store.update_run(run_id, status=S.RUN_RUNNING, started_at=time.time())
        summary: dict[str, Any] = {}
        try:
            cfg = SliceConfig(
                cycles=params["cycles"], scenario=params["scenario"], level=params["level"],
                start_scenario=bool(params.get("start_scenario", True)),
                cooldown_s=float(params.get("cooldown_s", 20.0)),
                baseline_duration=float(params.get("baseline_duration", 37.38)),
                trigger_wait_timeout_s=float(params.get("trigger_wait_timeout_s", 600.0)),
                params=params,
            )
            sl = self._slice_factory(cfg, sink, cancel)
            summary = asyncio.run(sl.arun())
            status = summary.get("status", S.RUN_COMPLETED)
            self.store.update_run(run_id, status=status, ended_at=time.time(),
                                  cycles_done=summary.get("cycles_done", 0),
                                  error=summary.get("error"))
        except Exception as e:                                        # noqa: BLE001
            log.exception("Run %s crashed", run_id)
            self.store.update_run(run_id, status=S.RUN_FAILED, ended_at=time.time(),
                                  error=f"{type(e).__name__}: {e}")
            try:
                sink.emit(S.RUN_SUMMARY, S.FAILED, summary=f"run crashed: {type(e).__name__}: {e}")
            except Exception:                                         # noqa: BLE001
                pass
        finally:
            self._cancels.pop(run_id, None)
            log.info("Run %s finished with status %s", run_id, self.store.get_run(run_id)["status"])
