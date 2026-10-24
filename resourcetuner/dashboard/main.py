"""Live telemetry + classifier dashboard (developer viewer).

    ./venv/bin/python -m resourcetuner.dashboard [--host 127.0.0.1] [--port 8000]

A background thread samples host + container telemetry once per second and classifies the
recent window; GET /api/snapshot returns the latest result plus the last 60 s of history, and
GET / serves static/dashboard.html, which polls that endpoint every second.

Sampling happens in ONE thread, not per request: telemetry rates are deltas between samples
and the classifier needs a contiguous window, so N browser tabs must not create N samplers.

This is a read-only viewer. The §3 pipeline dashboard (SSE, per-stage streaming, manual
"Analyze & Optimize") is a later step and will build on this.
"""

from __future__ import annotations

import argparse
import threading
import time
from collections import deque
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from resourcetuner.classifier.rules import ClassifierConfig, Classification, classify
from resourcetuner.telemetry.containers import ContainersSnapshot, ContainerTelemetry
from resourcetuner.telemetry.host import HostSnapshot, HostTelemetry

STATIC = Path(__file__).resolve().parent / "static"
HISTORY_S = 60


def build_payload(host: HostSnapshot, ctrs: ContainersSnapshot, cls: Classification | None,
                  history: list[dict]) -> dict:
    """Shape one tick for the page. Pure function, so it is unit-testable without a server."""
    mib = 2 ** 20
    cores = host.cpu.core_count or 1
    psi = lambda p: None if p is None else p.some_avg10  # noqa: E731
    containers = []
    for c in ctrs.containers:
        containers.append({
            "name": c.name,
            "image": c.image,
            "cpu_percent": c.cpu_percent,
            "memory_mib": round(c.memory_working_set_bytes / mib, 1) if c.memory_working_set_bytes is not None else None,
            "memory_limit_mib": round(c.limits.memory_max_bytes / mib) if c.limits and c.limits.memory_max_bytes else None,
            "io_read_mib_s": round((c.io_read_bytes_per_s or 0) / mib, 2),
            "io_write_mib_s": round((c.io_write_bytes_per_s or 0) / mib, 2),
            "classified_as": cls.containers.get(c.name) if cls else None,
            # Shown for visual validation only; the classifier never sees this field.
            "ground_truth_label": c.ground_truth_label,
        })
    return {
        "timestamp": host.timestamp,
        "host": {
            "cpu_percent": host.cpu.percent,
            "cores": cores,
            "iowait_percent": host.cpu.iowait,
            "load_1m": host.cpu.load_avg_1m,
            "memory_percent": host.memory.percent,
            "memory_available_mib": round(host.memory.available_bytes / mib),
            "memory_total_mib": round(host.memory.total_bytes / mib),
            "disk_read_mib_s": round((host.disk.read_bytes_per_s or 0) / mib, 2),
            "disk_write_mib_s": round((host.disk.write_bytes_per_s or 0) / mib, 2),
            "disk_util_percent": host.disk.max_util_percent,
            "delayacct_enabled": host.delayacct_enabled,
            "psi_cpu": psi(host.cpu.psi), "psi_memory": psi(host.memory.psi), "psi_io": psi(host.disk.psi),
        },
        "classification": None if cls is None else {
            "workload": cls.workload,
            "confidence": cls.confidence,
            "dominance": cls.dominance,
            "persistence": cls.persistence,
            "needs_llm_fallback": cls.needs_llm_fallback,
            "fallback_reasons": cls.fallback_reasons,
            "background_activity": cls.background_activity,
            "unattributed_activity_percent": cls.unattributed_activity_percent,
            "unattributed_processes": cls.unattributed_processes,
            "class_activity_percent": {k: v.activity_percent for k, v in cls.evidence.items() if v.activity_percent > 0},
            "class_io_wait_percent": {k: v.io_wait_percent for k, v in cls.evidence.items() if v.io_wait_percent > 0},
            "window": cls.window,
        },
        "containers": sorted(containers, key=lambda c: c["name"]),
        "docker": {"available": ctrs.available, "error": ctrs.error},
        "history": history,
    }


class Sampler:
    def __init__(self, interval_s: float = 1.0):
        self.interval_s = interval_s
        self.config = ClassifierConfig.load()
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._error: str | None = None
        self._stop = threading.Event()
        self._window: deque[HostSnapshot] = deque(maxlen=self.config.window_samples)
        self._history: deque[dict] = deque(maxlen=HISTORY_S)
        self._thread = threading.Thread(target=self._run, name="telemetry-sampler", daemon=True)

    def start(self) -> None:
        self._host = HostTelemetry(top_n=25)
        self._ctrs = ContainerTelemetry()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> tuple[dict | None, str | None]:
        with self._lock:
            return self._latest, self._error

    def _run(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            next_tick += self.interval_s
            self._stop.wait(max(0.0, next_tick - time.monotonic()))
            try:
                host = self._host.sample()
                ctrs = self._ctrs.sample()
                self._window.append(host)
                cls = classify(list(self._window), ctrs, self.config)
                self._history.append({
                    "t": round(host.timestamp, 1),
                    "cpu": host.cpu.percent,
                    "mem": host.memory.percent,
                    "workload": cls.workload,
                })
                payload = build_payload(host, ctrs, cls, list(self._history))
                with self._lock:
                    self._latest, self._error = payload, None
            except Exception as e:  # keep sampling; surface the error to the page
                with self._lock:
                    self._error = f"{type(e).__name__}: {e}"
            if time.monotonic() - next_tick > 5 * self.interval_s:
                next_tick = time.monotonic()  # fell far behind (e.g. VM stall): don't burst-catch-up


def create_app(sampler: Sampler) -> FastAPI:
    app = FastAPI(title="ResourceTuner dashboard")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "dashboard.html")

    @app.get("/api/snapshot")
    def snapshot() -> JSONResponse:
        payload, error = sampler.latest()
        if payload is None:
            return JSONResponse({"status": "warming_up", "error": error}, status_code=503)
        return JSONResponse({**payload, "sampler_error": error})

    return app


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="ResourceTuner live dashboard")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 to reach it from the VM host)")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    sampler = Sampler()
    sampler.start()
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"ResourceTuner dashboard: http://{shown}:{args.port} par kholo  (Ctrl+C to stop)", flush=True)
    try:
        uvicorn.run(create_app(sampler), host=args.host, port=args.port, log_level="warning")
    finally:
        sampler.stop()


if __name__ == "__main__":
    main()
