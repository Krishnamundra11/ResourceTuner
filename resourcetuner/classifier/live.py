"""Background classifier: samples telemetry at 1 Hz and keeps the latest Classification.

Used where a long-running process needs "what is running right now" — e.g. the Safety Gate
inside the MCP server (via ControllerGateContext). Deterministic: telemetry + rules.py only.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from resourcetuner.classifier.rules import Classification, ClassifierConfig, classify
from resourcetuner.telemetry.containers import ContainerTelemetry
from resourcetuner.telemetry.host import HostTelemetry

log = logging.getLogger("resourcetuner.live")


class LiveClassifier:
    def __init__(self, interval_s: float = 1.0, max_age_s: float = 5.0, config: ClassifierConfig | None = None):
        self.interval_s = interval_s
        self.max_age_s = max_age_s          # older results are treated as "no data"
        self.config = config or ClassifierConfig.load()
        self._lock = threading.Lock()
        self._latest: tuple[float, Classification] | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="live-classifier", daemon=True)

    def start(self) -> "LiveClassifier":
        self._host = HostTelemetry(top_n=25)
        self._ctrs = ContainerTelemetry()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> Classification | None:
        with self._lock:
            if self._latest is None or time.monotonic() - self._latest[0] > self.max_age_s:
                return None
            return self._latest[1]

    def wait_ready(self, timeout_s: float = 15.0) -> bool:
        """Block until a full classification window is available."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            c = self.latest()
            if c is not None and c.window >= self.config.window_samples:
                return True
            time.sleep(0.2)
        return False

    def workload_of(self, container_name: str) -> str | None:
        c = self.latest()
        return None if c is None else c.containers.get(container_name)

    def host_workload(self) -> str | None:
        c = self.latest()
        return None if c is None else c.workload

    def _run(self) -> None:
        window = deque(maxlen=self.config.window_samples)
        next_tick = time.monotonic()
        while not self._stop.is_set():
            next_tick += self.interval_s
            self._stop.wait(max(0.0, next_tick - time.monotonic()))
            try:
                window.append(self._host.sample())
                result = classify(list(window), self._ctrs.sample(), self.config)
                with self._lock:
                    self._latest = (time.monotonic(), result)
            except Exception:  # keep sampling; a stale result becomes "no data" after max_age_s
                log.exception("live classification failed")
            if time.monotonic() - next_tick > 5 * self.interval_s:
                next_tick = time.monotonic()
