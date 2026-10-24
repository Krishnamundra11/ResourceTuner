"""Per-container telemetry (deterministic, no LLM).

Design decision (measured, not assumed)
---------------------------------------
The naive approach is to call ``container.stats()`` for every container every tick.
Measured on the project VM (Docker 29.1.3, cgroup v2, 8 vCPU):

* ``container.stats(stream=False, one_shot=True)``: ~250ms per container, serial.
* ``containers.list()``: ~1.2s per call (daemon-side), vs ~20ms for one ``inspect``.
* So 5 scenario containers would cost ~2.4s per tick, before any analysis.
* ``docker stats --no-stream`` reported 124% CPU for a container hard-capped at
  ``--cpus 0.5`` (physically impossible); its very short sampling window is noisy.
  Our cgroup-delta reading for the same container: ~50%, with throttling active.

What we do instead:

* Discovery: glob the cgroup tree for ``docker-<id>.scope`` (cheap, every tick);
  call ``inspect`` once per *new* container for name/image/labels (docker SDK).
* Metrics: read the container's cgroup v2 files directly — the same files the
  daemon reads to build ``stats()`` — in milliseconds. These also expose things
  ``stats()`` doesn't cleanly provide: per-container PSI, CPU throttling, and the
  *enforced* limits (cpu.max / cpu.weight / io.weight / memory.max) that the
  Resource Controller needs as old_value for exact rollback (§8).
* Fallback: if no docker cgroup layout is visible, use ``containers.list()`` +
  ``container.stats(one_shot=True)`` (``source="docker_api"``): slow but correct.

Validated with probe containers of known caps (``--cpus 0.5`` → ~50% + throttled;
200MB in a 512MiB container → 201MiB, matching ``docker stats``).

Like ``host.py``, the collector is stateful: rates are deltas between consecutive
``sample()`` calls.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import docker
from docker.errors import DockerException, NotFound

from resourcetuner.telemetry.host import PSI, read_psi

CGROUP_ROOT = Path("/sys/fs/cgroup")
GROUND_TRUTH_LABEL = "workload"


@dataclass
class ContainerLimits:
    cpu_quota_cores: float | None   # cpu.max quota/period; None = unlimited
    cpu_weight: int | None          # cgroup v2 cpu.weight (1-10000, default 100)
    io_weight: int | None           # cgroup v2 io.weight default (1-10000, default 100)
    memory_max_bytes: int | None    # None = unlimited


@dataclass
class ContainerStats:
    id: str
    name: str
    image: str
    status: str
    # Ground truth from `--label workload=<type>`. For CALIBRATION/VALIDATION ONLY —
    # the classifier must never read this field.
    ground_truth_label: str | None
    source: str                              # "cgroup" | "docker_api"
    cpu_percent: float | None                # docker convention: 100 = one full core
    cpu_throttled_ms_per_s: float | None     # time spent throttled by cpu.max, per second
    memory_bytes: int | None                 # memory.current (includes page cache)
    memory_working_set_bytes: int | None     # current - inactive_file (what `docker stats` shows)
    # Cumulative memory.events counters. On a no-swap host, anon memory can't be reclaimed, so
    # a container over its cap gets OOM-killed instead of stalling: memory PSI stays ~0 and
    # these counters are the real contention signal (measured: 171 max hits, 4 oom_kills, PSI 0%).
    memory_max_events: int | None            # times usage hit memory.max
    memory_oom_kills: int | None             # processes OOM-killed inside the container
    io_read_bytes_per_s: float | None
    io_write_bytes_per_s: float | None
    pids: int | None
    cpu_psi: PSI | None
    memory_psi: PSI | None
    io_psi: PSI | None
    limits: ContainerLimits | None


@dataclass
class ContainersSnapshot:
    timestamp: float
    available: bool                          # False if the docker daemon is unreachable
    error: str | None
    containers: list[ContainerStats] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --- cgroup v2 parsing helpers ---------------------------------------------------

def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _kv(text: str | None) -> dict[str, int]:
    """Parse 'key value' lines (cpu.stat, memory.stat)."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def parse_io_stat(text: str | None) -> tuple[int, int]:
    """Sum rbytes/wbytes across devices from io.stat ('8:0 rbytes=.. wbytes=.. ...')."""
    rbytes = wbytes = 0
    for line in (text or "").splitlines():
        for field_ in line.split()[1:]:
            k, _, v = field_.partition("=")
            if k == "rbytes":
                rbytes += int(v)
            elif k == "wbytes":
                wbytes += int(v)
    return rbytes, wbytes


def parse_limits(cg: Path) -> ContainerLimits:
    cpu_quota = None
    cpu_max = _read(cg / "cpu.max")
    if cpu_max:
        quota, _, period = cpu_max.partition(" ")
        if quota != "max" and period:
            cpu_quota = int(quota) / int(period)
    cpu_weight = _read(cg / "cpu.weight")
    io_weight = None
    for line in (_read(cg / "io.weight") or "").splitlines():
        if line.startswith("default "):
            io_weight = int(line.split()[1])
    mem_max = _read(cg / "memory.max")
    return ContainerLimits(
        cpu_quota_cores=cpu_quota,
        cpu_weight=int(cpu_weight) if cpu_weight and cpu_weight.isdigit() else None,
        io_weight=io_weight,
        memory_max_bytes=int(mem_max) if mem_max and mem_max.isdigit() else None,
    )


def find_cgroup_dir(container_id: str, root: Path = CGROUP_ROOT) -> Path | None:
    for candidate in (
        root / "system.slice" / f"docker-{container_id}.scope",   # systemd cgroup driver
        root / "docker" / container_id,                            # cgroupfs driver
    ):
        if (candidate / "cpu.stat").exists():
            return candidate
    return None


def _meta_from_inspect(attrs: dict) -> dict:
    config = attrs.get("Config") or {}
    labels = config.get("Labels") or {}
    return dict(
        id=attrs["Id"],
        name=(attrs.get("Name") or attrs["Id"][:12]).lstrip("/"),
        image=config.get("Image", ""),
        status=(attrs.get("State") or {}).get("Status", ""),
        ground_truth_label=labels.get(GROUND_TRUTH_LABEL),
    )


# --- collector -------------------------------------------------------------------

class ContainerTelemetry:
    """Stateful per-container collector. Never raises on docker problems; reports them."""

    def __init__(self, client: docker.DockerClient | None = None, cgroup_root: Path = CGROUP_ROOT):
        self.cgroup_root = cgroup_root
        self._client = client
        self._client_error: str | None = None
        self._meta: dict[str, dict] = {}  # container id -> name/image/status/label (inspected once)
        # container id -> (monotonic time, cpu_usage_usec, rbytes, wbytes, throttled_usec)
        self._prev: dict[str, tuple[float, int, int, int, int]] = {}
        self.sample()  # prime delta baselines

    def _get_client(self) -> docker.DockerClient | None:
        if self._client is None:
            try:
                self._client = docker.from_env()
                self._client_error = None
            except DockerException as e:
                self._client_error = f"docker unavailable: {e}"
        return self._client

    def _cgroup_container_ids(self) -> set[str] | None:
        """Running container ids from the cgroup tree; None if no docker cgroup layout exists."""
        layouts = [
            (self.cgroup_root / "system.slice", "docker-*.scope", lambda n: n[len("docker-"):-len(".scope")]),
            (self.cgroup_root / "docker", "*", lambda n: n),
        ]
        found_layout, ids = False, set()
        for base, pattern, extract in layouts:
            if not base.is_dir():
                continue
            found_layout = True
            for p in base.glob(pattern):
                cid = extract(p.name)
                if len(cid) == 64 and (p / "cpu.stat").exists():
                    ids.add(cid)
        return ids if found_layout else None

    def sample(self) -> ContainersSnapshot:
        """Discovery is done from the cgroup tree (cheap); the docker API is only called once
        per *new* container to fetch its name/image/labels. On this VM `containers.list()`
        takes ~1.2s (daemon-side) vs ~20ms for a single inspect, so listing every tick
        would stall the telemetry loop."""
        ts = time.time()
        client = self._get_client()
        if client is None:
            return ContainersSnapshot(ts, False, self._client_error)

        ids = self._cgroup_container_ids()
        if ids is None:
            return self._sample_via_api(ts, client)

        results = []
        for cid in ids:
            meta = self._meta.get(cid)
            if meta is None:
                try:
                    meta = self._meta[cid] = _meta_from_inspect(client.api.inspect_container(cid))
                except NotFound:
                    continue  # exited between glob and inspect
                except DockerException as e:
                    self._client = None
                    return ContainersSnapshot(ts, False, f"docker inspect failed: {e}")
            cg = find_cgroup_dir(cid, self.cgroup_root)
            if cg is None:
                continue
            try:
                results.append(self._from_cgroup(cg, meta))
            except FileNotFoundError:
                continue  # exited mid-read
        self._meta = {k: v for k, v in self._meta.items() if k in ids}
        self._prev = {k: v for k, v in self._prev.items() if k in ids}
        results.sort(key=lambda c: c.name)
        return ContainersSnapshot(ts, True, None, results)

    def _sample_via_api(self, ts: float, client) -> ContainersSnapshot:
        """Fallback when no docker cgroup v2 layout is visible: slow, but correct."""
        try:
            listed = client.containers.list()
        except DockerException as e:
            self._client = None
            return ContainersSnapshot(ts, False, f"docker list failed: {e}")
        results = []
        for c in listed:
            try:
                results.append(self._from_api(c, _meta_from_inspect(c.attrs)))
            except NotFound:
                continue
        live = {c.id for c in results}
        self._prev = {k: v for k, v in self._prev.items() if k in live}
        results.sort(key=lambda c: c.name)
        return ContainersSnapshot(ts, True, None, results)

    def _rates(self, cid: str, now: float, usage_usec: int, rbytes: int, wbytes: int,
               throttled_usec: int) -> tuple[float | None, float | None, float | None, float | None]:
        prev = self._prev.get(cid)
        self._prev[cid] = (now, usage_usec, rbytes, wbytes, throttled_usec)
        if prev is None or now <= prev[0]:
            return None, None, None, None
        dt = now - prev[0]
        cpu_pct = (usage_usec - prev[1]) / (dt * 1e6) * 100
        throttled = (throttled_usec - prev[4]) / 1000 / dt
        return round(cpu_pct, 2), round(throttled, 2), (rbytes - prev[2]) / dt, (wbytes - prev[3]) / dt

    def _from_cgroup(self, cg: Path, meta: dict) -> ContainerStats:
        now = time.monotonic()
        cpu = _kv(_read(cg / "cpu.stat"))
        if not cpu:
            raise FileNotFoundError(cg)  # cgroup removed mid-read
        mem_stat = _kv(_read(cg / "memory.stat"))
        mem_events = _kv(_read(cg / "memory.events"))
        mem_current = _read(cg / "memory.current")
        mem_bytes = int(mem_current) if mem_current and mem_current.isdigit() else None
        rbytes, wbytes = parse_io_stat(_read(cg / "io.stat"))
        pids = _read(cg / "pids.current")
        cpu_pct, throttled, r_rate, w_rate = self._rates(
            meta["id"], now, cpu.get("usage_usec", 0), rbytes, wbytes, cpu.get("throttled_usec", 0))
        return ContainerStats(
            **meta,
            source="cgroup",
            cpu_percent=cpu_pct,
            cpu_throttled_ms_per_s=throttled,
            memory_bytes=mem_bytes,
            memory_working_set_bytes=(
                max(0, mem_bytes - mem_stat.get("inactive_file", 0)) if mem_bytes is not None else None),
            memory_max_events=mem_events.get("max"),
            memory_oom_kills=mem_events.get("oom_kill"),
            io_read_bytes_per_s=r_rate,
            io_write_bytes_per_s=w_rate,
            pids=int(pids) if pids and pids.isdigit() else None,
            cpu_psi=read_psi("cpu.pressure", cg),
            memory_psi=read_psi("memory.pressure", cg),
            io_psi=read_psi("io.pressure", cg),
            limits=parse_limits(cg),
        )

    def _from_api(self, c, meta: dict) -> ContainerStats:
        st = c.stats(stream=False, one_shot=True)
        now = time.monotonic()
        cpu = st.get("cpu_stats", {})
        usage_usec = cpu.get("cpu_usage", {}).get("total_usage", 0) // 1000
        throttled_usec = cpu.get("throttling_data", {}).get("throttled_time", 0) // 1000
        rbytes = wbytes = 0
        for entry in (st.get("blkio_stats", {}).get("io_service_bytes_recursive") or []):
            op = entry.get("op", "").lower()
            if op == "read":
                rbytes += entry.get("value", 0)
            elif op == "write":
                wbytes += entry.get("value", 0)
        mem = st.get("memory_stats", {})
        usage = mem.get("usage")
        cpu_pct, throttled, r_rate, w_rate = self._rates(
            meta["id"], now, usage_usec, rbytes, wbytes, throttled_usec)
        return ContainerStats(
            **meta,
            source="docker_api",
            cpu_percent=cpu_pct,
            cpu_throttled_ms_per_s=throttled,
            memory_bytes=usage,
            memory_working_set_bytes=(
                max(0, usage - mem.get("stats", {}).get("inactive_file", 0)) if usage is not None else None),
            memory_max_events=None, memory_oom_kills=None,
            io_read_bytes_per_s=r_rate,
            io_write_bytes_per_s=w_rate,
            pids=st.get("pids_stats", {}).get("current"),
            cpu_psi=None, memory_psi=None, io_psi=None, limits=None,
        )


def format_snapshot(s: ContainersSnapshot) -> str:
    if not s.available:
        return f"containers: UNAVAILABLE ({s.error})"
    if not s.containers:
        return "containers: none running"
    mib = lambda b: f"{b / 2**20:.1f}MiB" if b is not None else "n/a"  # noqa: E731
    lines = []
    for c in s.containers:
        psi = f"{c.cpu_psi.some_avg10:.1f}%" if c.cpu_psi else "n/a"
        lim = c.limits
        lim_s = (f"quota={lim.cpu_quota_cores} cpu.w={lim.cpu_weight} io.w={lim.io_weight} "
                 f"mem.max={mib(lim.memory_max_bytes) if lim.memory_max_bytes else 'max'}") if lim else "n/a"
        lines.append(
            f"{c.name:<16} [{c.source}] label={c.ground_truth_label} cpu={c.cpu_percent}% "
            f"thr={c.cpu_throttled_ms_per_s}ms/s mem_ws={mib(c.memory_working_set_bytes)} "
            f"io r={mib(c.io_read_bytes_per_s)}/s w={mib(c.io_write_bytes_per_s)}/s "
            f"pids={c.pids} cpuPSI={psi} | {lim_s}")
    return "\n".join(lines)


if __name__ == "__main__":
    t = ContainerTelemetry()
    for _ in range(3):
        time.sleep(1.0)
        start = time.perf_counter()
        snap = t.sample()
        print(f"(sample took {(time.perf_counter() - start) * 1000:.1f}ms)")
        print(format_snapshot(snap))
        print("-" * 80)
