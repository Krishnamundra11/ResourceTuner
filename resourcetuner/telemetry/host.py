"""Host-level telemetry (deterministic, no LLM).

Wraps psutil + /proc into a single structured snapshot. Rates (disk bytes/s,
per-process CPU%, per-process I/O) are computed as deltas between consecutive
calls to ``HostTelemetry.sample()``, so the collector is stateful: create one
instance and call ``sample()`` on every tick.

Containers run on the host kernel, so these numbers already include container
load; ``containers.py`` provides the per-container breakdown.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import NamedTuple

import psutil

PSI_ROOT = Path("/proc/pressure")
# Matches both cgroup v2 systemd driver (docker-<id>.scope) and cgroupfs driver (/docker/<id>).
_CONTAINER_CGROUP_RE = re.compile(r"(?:docker-|/docker/)([0-9a-f]{64})")
# Virtual block devices that would double-count or add noise.
_IGNORED_DISK_PREFIXES = ("loop", "ram", "zram", "sr", "dm-")


@dataclass
class PSI:
    """Linux Pressure Stall Information for one resource (percent of wall time stalled)."""

    some_avg10: float
    some_avg60: float
    full_avg10: float | None  # cpu "full" is not meaningful at system level on older kernels
    full_avg60: float | None


@dataclass
class CPUStats:
    percent: float                 # overall busy %, all cores
    per_cpu: list[float]
    user: float
    system: float
    iowait: float
    steal: float                   # relevant on a VM: time stolen by the hypervisor
    load_avg_1m: float
    core_count: int
    psi: PSI | None


@dataclass
class MemoryStats:
    total_bytes: int
    available_bytes: int
    percent: float
    swap_total_bytes: int
    swap_used_bytes: int
    psi: PSI | None


@dataclass
class DiskStats:
    read_bytes_per_s: float | None     # None on the first sample (no delta yet)
    write_bytes_per_s: float | None
    read_iops: float | None
    write_iops: float | None
    max_util_percent: float | None     # busiest physical disk, busy_time / wall time
    psi: PSI | None


@dataclass
class ProcessInfo:
    pid: int
    name: str
    cmdline: str                       # truncated
    username: str | None
    cpu_percent: float                 # can exceed 100 on multi-core (psutil convention)
    rss_bytes: int
    num_threads: int
    nice: int | None
    ionice_class: str | None
    io_read_bytes_per_s: float | None  # None if AccessDenied (e.g. root-owned) or first sample
    io_write_bytes_per_s: float | None
    container_id: str | None           # full 64-char docker id if the process lives in a container
    ppid: int | None = None
    # Cmdlines of up to 3 ancestors, nearest first. Needed because workers often carry no
    # signature of their own: pytest-xdist workers run as
    # `python -c import sys;exec(eval(sys.stdin.readline()))`, only their parent says pytest.
    ancestors: list[str] = field(default_factory=list)
    # Share of the interval spent blocked on block I/O (100 = the whole interval), from
    # delayacct_blkio_ticks. Always 0 unless `sysctl kernel.task_delayacct=1` (off by default
    # since Linux 5.14; enabling it needs root).
    io_wait_percent: float = 0.0


@dataclass
class HostSnapshot:
    timestamp: float
    interval_s: float | None           # wall time since previous sample
    cpu: CPUStats
    memory: MemoryStats
    disk: DiskStats
    top_processes: list[ProcessInfo] = field(default_factory=list)
    # kernel.task_delayacct: when off, every io_wait_percent is 0 and the classifier sees CPU only.
    delayacct_enabled: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def read_psi(resource: str, root: Path = PSI_ROOT) -> PSI | None:
    """Parse /proc/pressure/<resource>. Returns None if PSI is unavailable."""
    try:
        text = (root / resource).read_text()
    except OSError:
        return None
    return parse_psi(text)


def parse_psi(text: str) -> PSI | None:
    values: dict[str, dict[str, float]] = {}
    for line in text.strip().splitlines():
        kind, *pairs = line.split()
        values[kind] = {k: float(v) for k, v in (p.split("=") for p in pairs)}
    if "some" not in values:
        return None
    full = values.get("full")
    return PSI(
        some_avg10=values["some"]["avg10"],
        some_avg60=values["some"]["avg60"],
        full_avg10=full["avg10"] if full else None,
        full_avg60=full["avg60"] if full else None,
    )


_CLK_TCK = os.sysconf("SC_CLK_TCK")
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


class ProcStat(NamedTuple):
    ticks: int        # utime + stime, clock ticks
    rss_pages: int
    starttime: int    # clock ticks since boot
    ppid: int
    blkio_ticks: int = 0  # delayacct_blkio_ticks: time blocked on block I/O (0 unless kernel.task_delayacct=1)


def scan_proc_stat(proc: str = "/proc") -> dict[int, ProcStat]:
    """pid -> ProcStat, one read of /proc/<pid>/stat per process."""
    out = {}
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(f"{proc}/{name}/stat", "rb") as f:
                raw = f.read()
        except OSError:
            continue  # process exited mid-scan
        # comm (field 2) may contain spaces/parens, so split after the LAST ')'.
        fields = raw[raw.rfind(b")") + 2:].split()
        try:
            out[int(name)] = ProcStat(int(fields[11]) + int(fields[12]), int(fields[21]),
                                      int(fields[19]), int(fields[1]),
                                      int(fields[39]) if len(fields) > 39 else 0)
        except (IndexError, ValueError):
            continue
    return out


def read_cmdline(pid: int, max_len: int = 200) -> str | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read(max_len * 2)
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode(errors="replace").strip()[:max_len] or None


def _uptime_s() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def delayacct_enabled() -> bool | None:
    try:
        return Path("/proc/sys/kernel/task_delayacct").read_text().strip() == "1"
    except OSError:
        return None


def container_id_for_pid(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None
    m = _CONTAINER_CGROUP_RE.search(text)
    return m.group(1) if m else None


class HostTelemetry:
    """Stateful host collector. Call ``sample()`` periodically; rates are since the last call."""

    def __init__(self, top_n: int = 15, cmdline_max_len: int = 200):
        self.top_n = top_n
        self.cmdline_max_len = cmdline_max_len
        self._last_time: float | None = None
        self._last_disk: dict | None = None
        # Keys are (pid, starttime): starttime guards against PID reuse.
        self._last_proc_io: dict[tuple[int, int], tuple[int, int]] = {}
        self._last_proc_ticks: dict[tuple[int, int], tuple[int, int]] = {}  # -> (cpu ticks, blkio ticks)
        # Prime delta counters so the first real sample is meaningful.
        psutil.cpu_percent(interval=None, percpu=True)
        psutil.cpu_times_percent(interval=None)
        self._last_proc_ticks = {(pid, st.starttime): (st.ticks, st.blkio_ticks)
                                 for pid, st in scan_proc_stat().items()}
        self._last_disk = self._read_disk_counters()
        self._last_time = time.monotonic()

    def sample(self) -> HostSnapshot:
        now = time.monotonic()
        interval = now - self._last_time if self._last_time is not None else None
        snap = HostSnapshot(
            timestamp=time.time(),
            interval_s=interval,
            cpu=self._cpu(),
            memory=self._memory(),
            disk=self._disk(interval),
            top_processes=self._processes(interval),
            delayacct_enabled=delayacct_enabled(),
        )
        self._last_time = now
        return snap

    # --- CPU -----------------------------------------------------------------
    def _cpu(self) -> CPUStats:
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        times = psutil.cpu_times_percent(interval=None)
        return CPUStats(
            percent=round(sum(per_cpu) / len(per_cpu), 2) if per_cpu else 0.0,
            per_cpu=per_cpu,
            user=times.user,
            system=times.system,
            iowait=getattr(times, "iowait", 0.0),
            steal=getattr(times, "steal", 0.0),
            load_avg_1m=os.getloadavg()[0],
            core_count=psutil.cpu_count(logical=True) or len(per_cpu),
            psi=read_psi("cpu"),
        )

    # --- Memory --------------------------------------------------------------
    def _memory(self) -> MemoryStats:
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return MemoryStats(
            total_bytes=vm.total,
            available_bytes=vm.available,
            percent=vm.percent,
            swap_total_bytes=sw.total,
            swap_used_bytes=sw.used,
            psi=read_psi("memory"),
        )

    # --- Disk ----------------------------------------------------------------
    @staticmethod
    def _read_disk_counters() -> dict:
        counters = psutil.disk_io_counters(perdisk=True, nowrap=True) or {}
        # /sys/block lists whole disks only (no partitions), so summing these can't double-count.
        try:
            whole_disks = set(os.listdir("/sys/block"))
        except OSError:
            whole_disks = set(counters)
        return {
            name: c for name, c in counters.items()
            if name in whole_disks and not name.startswith(_IGNORED_DISK_PREFIXES)
        }

    def _disk(self, interval: float | None) -> DiskStats:
        current = self._read_disk_counters()
        prev, self._last_disk = self._last_disk, current
        psi = read_psi("io")
        if not interval or not prev:
            return DiskStats(None, None, None, None, None, psi)
        rb = wb = rc = wc = 0
        max_util = 0.0
        for name, c in current.items():
            p = prev.get(name)
            if p is None:
                continue
            rb += c.read_bytes - p.read_bytes
            wb += c.write_bytes - p.write_bytes
            rc += c.read_count - p.read_count
            wc += c.write_count - p.write_count
            busy_ms = getattr(c, "busy_time", 0) - getattr(p, "busy_time", 0)
            max_util = max(max_util, min(100.0, busy_ms / (interval * 1000) * 100))
        return DiskStats(
            read_bytes_per_s=rb / interval,
            write_bytes_per_s=wb / interval,
            read_iops=rc / interval,
            write_iops=wc / interval,
            max_util_percent=round(max_util, 2),
            psi=psi,
        )

    # --- Processes -----------------------------------------------------------
    def _processes(self, interval: float | None) -> list[ProcessInfo]:
        # Pass 1 (cheap, all processes): one read of /proc/<pid>/stat per process, enough to
        # rank by CPU and RSS. Going through psutil here costs ~0.5s CPU per sample on this VM
        # under load (≈600 file opens + overhead), which would make the collector itself a top
        # CPU consumer exactly when the machine is contended.
        current = scan_proc_stat()
        uptime = _uptime_s()
        prev_ticks, ticks_now = self._last_proc_ticks, {}
        ranked = []  # (activity = cpu + io_wait, cpu_percent, io_wait_percent, rss_bytes, pid, starttime)
        for pid, st in current.items():
            key = (pid, st.starttime)
            ticks_now[key] = (st.ticks, st.blkio_ticks)
            prev = prev_ticks.get(key)
            if prev is not None and interval:
                cpu = (st.ticks - prev[0]) / _CLK_TCK / interval * 100
                io_wait = (st.blkio_ticks - prev[1]) / _CLK_TCK / interval * 100
            elif interval:
                # Born since the last sample: average over its lifetime. Without this, short-lived
                # workers (a compiler's cc1 lives ~0.3s) would always read 0% and a build would
                # be invisible to the classifier.
                age = uptime - st.starttime / _CLK_TCK
                fresh = 0.05 < age <= interval * 1.5
                cpu = st.ticks / _CLK_TCK / age * 100 if fresh else 0.0
                io_wait = st.blkio_ticks / _CLK_TCK / age * 100 if fresh else 0.0
            else:
                cpu = io_wait = 0.0
            cpu, io_wait = round(cpu, 1), round(max(0.0, io_wait), 1)
            ranked.append((cpu + io_wait, cpu, io_wait, st.rss_pages * _PAGE_SIZE, pid, st.starttime))
        self._last_proc_ticks = ticks_now
        # Rank by activity, not CPU alone: a process starved on disk I/O (e.g. an indexer under
        # an I/O hog) spends its time blocked, not on the CPU, and must not drop out of view.
        by_activity = sorted(ranked, reverse=True)[: self.top_n]
        by_mem = sorted(ranked, key=lambda r: r[3], reverse=True)[: self.top_n // 3]
        selected = {r[4]: r for r in by_activity + by_mem}.values()

        # Pass 2 (expensive, top-N only): identity, priorities, I/O, container membership.
        result = []
        seen_io_keys = set()
        cmdline_cache: dict[int, str | None] = {}
        for _, cpu_pct, io_wait_pct, rss, pid, starttime in selected:
            try:
                p = psutil.Process(pid)
                with p.oneshot():
                    info = p.as_dict(["name", "cmdline", "username", "num_threads", "nice"],
                                     ad_value=None)
            except psutil.NoSuchProcess:
                continue
            key = (pid, starttime)
            read_rate = write_rate = None
            try:
                io = p.io_counters()
                seen_io_keys.add(key)
                prev = self._last_proc_io.get(key)
                self._last_proc_io[key] = (io.read_bytes, io.write_bytes)
                if prev is not None and interval:
                    read_rate = (io.read_bytes - prev[0]) / interval
                    write_rate = (io.write_bytes - prev[1]) / interval
            except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                pass
            try:
                ionice_class = p.ionice().ioclass.name
            except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                ionice_class = None
            cmdline = " ".join(info["cmdline"] or []) or info["name"] or ""
            ppid = current[pid].ppid if pid in current else None
            ancestors, a = [], ppid
            while a and a > 1 and len(ancestors) < 3:
                if a not in cmdline_cache:
                    cmdline_cache[a] = read_cmdline(a, self.cmdline_max_len)
                if cmdline_cache[a]:
                    ancestors.append(cmdline_cache[a])
                a = current[a].ppid if a in current else None
            result.append(ProcessInfo(
                pid=pid,
                name=info["name"] or "",
                cmdline=cmdline[: self.cmdline_max_len],
                username=info["username"],
                cpu_percent=cpu_pct,
                rss_bytes=rss,
                num_threads=info["num_threads"] or 0,
                nice=info["nice"],
                ionice_class=ionice_class,
                io_read_bytes_per_s=read_rate,
                io_write_bytes_per_s=write_rate,
                container_id=container_id_for_pid(pid),
                ppid=ppid,
                ancestors=ancestors,
                io_wait_percent=io_wait_pct,
            ))
        # Drop I/O baselines for processes no longer tracked, so the dict can't grow forever.
        self._last_proc_io = {k: v for k, v in self._last_proc_io.items() if k in seen_io_keys}
        result.sort(key=lambda r: r.cpu_percent + r.io_wait_percent, reverse=True)
        return result


def _fmt_bytes(n: float | None) -> str:
    if n is None:
        return "n/a"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TiB"


def format_snapshot(s: HostSnapshot) -> str:
    psi = lambda x: f"some10={x.some_avg10:.1f}%" if x else "n/a"  # noqa: E731
    lines = [
        f"interval={s.interval_s:.2f}s" if s.interval_s else "interval=first",
        f"CPU  {s.cpu.percent:5.1f}%  user={s.cpu.user:.1f} sys={s.cpu.system:.1f} "
        f"iowait={s.cpu.iowait:.1f} steal={s.cpu.steal:.1f} load1={s.cpu.load_avg_1m:.2f} "
        f"cores={s.cpu.core_count}  PSI {psi(s.cpu.psi)}",
        f"MEM  {s.memory.percent:5.1f}%  avail={_fmt_bytes(s.memory.available_bytes)} / "
        f"{_fmt_bytes(s.memory.total_bytes)} swap_used={_fmt_bytes(s.memory.swap_used_bytes)}  "
        f"PSI {psi(s.memory.psi)}",
        f"DISK read={_fmt_bytes(s.disk.read_bytes_per_s)}/s write={_fmt_bytes(s.disk.write_bytes_per_s)}/s "
        f"util={s.disk.max_util_percent}%  PSI {psi(s.disk.psi)}",
        "TOP PROCESSES:",
    ]
    for p in s.top_processes[:8]:
        cid = p.container_id[:12] if p.container_id else "-"
        lines.append(f"  {p.pid:>7} {p.cpu_percent:6.1f}% {_fmt_bytes(p.rss_bytes):>9} "
                     f"nice={p.nice} ctr={cid:<12} {p.name}")
    return "\n".join(lines)


if __name__ == "__main__":
    t = HostTelemetry()
    for _ in range(3):
        time.sleep(1.0)
        print(format_snapshot(t.sample()))
        print("-" * 80)
