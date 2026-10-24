import json
import os
import signal
import subprocess
import time
import uuid

import pytest

from resourcetuner.telemetry.containers import (
    ContainerTelemetry, find_cgroup_dir, parse_io_stat, parse_limits,
)
from resourcetuner.telemetry.host import HostTelemetry, parse_psi, read_psi, scan_proc_stat


# --- pure parsers (no live system needed) -----------------------------------------

def test_parse_psi_with_full_line():
    psi = parse_psi("some avg10=12.50 avg60=3.00 avg300=1.00 total=100\n"
                    "full avg10=2.00 avg60=0.50 avg300=0.10 total=10\n")
    assert psi.some_avg10 == 12.5 and psi.some_avg60 == 3.0
    assert psi.full_avg10 == 2.0 and psi.full_avg60 == 0.5


def test_parse_psi_some_only():
    psi = parse_psi("some avg10=1.00 avg60=2.00 avg300=3.00 total=4\n")
    assert psi.full_avg10 is None


def test_read_psi_missing_file_returns_none(tmp_path):
    assert read_psi("cpu", tmp_path) is None


def test_parse_io_stat_sums_devices():
    text = ("8:0 rbytes=100 wbytes=200 rios=1 wios=2 dbytes=0 dios=0\n"
            "8:16 rbytes=1 wbytes=2 rios=1 wios=1 dbytes=0 dios=0\n")
    assert parse_io_stat(text) == (101, 202)
    assert parse_io_stat("") == (0, 0)
    assert parse_io_stat(None) == (0, 0)


def test_parse_limits(tmp_path):
    (tmp_path / "cpu.max").write_text("50000 100000\n")
    (tmp_path / "cpu.weight").write_text("100\n")
    (tmp_path / "io.weight").write_text("default 2930\n")
    (tmp_path / "memory.max").write_text("536870912\n")
    lim = parse_limits(tmp_path)
    assert lim.cpu_quota_cores == 0.5
    assert lim.cpu_weight == 100 and lim.io_weight == 2930
    assert lim.memory_max_bytes == 512 * 2**20


def test_parse_limits_unlimited(tmp_path):
    (tmp_path / "cpu.max").write_text("max 100000\n")
    (tmp_path / "memory.max").write_text("max\n")
    lim = parse_limits(tmp_path)
    assert lim.cpu_quota_cores is None and lim.memory_max_bytes is None


def test_find_cgroup_dir_systemd_layout(tmp_path):
    cid = "a" * 64
    d = tmp_path / "system.slice" / f"docker-{cid}.scope"
    d.mkdir(parents=True)
    (d / "cpu.stat").write_text("usage_usec 1\n")
    assert find_cgroup_dir(cid, tmp_path) == d
    assert find_cgroup_dir("b" * 64, tmp_path) is None


def test_scan_proc_stat_handles_weird_comm(tmp_path):
    # comm "(a) b)" contains spaces and parens; fields after it must still parse.
    rest = " ".join(["S", "7"] + ["0"] * 9 + ["70", "30"] + ["0"] * 6 + ["12345", "0", "250"] + ["0"] * 20)
    (tmp_path / "42").mkdir()
    (tmp_path / "42" / "stat").write_bytes(f"42 ((a) b)) {rest}".encode())
    (tmp_path / "self").mkdir()  # non-numeric entries ignored
    st = scan_proc_stat(str(tmp_path))[42]
    assert (st.ticks, st.rss_pages, st.starttime, st.ppid) == (100, 250, 12345, 7)


def test_scan_proc_stat_reads_blkio_delay(tmp_path):
    # field 42 (index 39 after the comm) = delayacct_blkio_ticks
    after_comm = ["S", "1"] + ["0"] * 9 + ["5", "5"] + ["0"] * 6 + ["100", "0", "10"] + ["0"] * 17 + ["77"] + ["0"] * 10
    assert len(after_comm) > 39 and after_comm[39] == "77"
    (tmp_path / "9").mkdir()
    (tmp_path / "9" / "stat").write_bytes(("9 (idx) " + " ".join(after_comm)).encode())
    assert scan_proc_stat(str(tmp_path))[9].blkio_ticks == 77


# --- live host ----------------------------------------------------------------------

def test_host_sample_is_sane_and_serializable():
    t = HostTelemetry()
    time.sleep(0.5)
    s = t.sample()
    assert 0.0 <= s.cpu.percent <= 100.0
    assert s.cpu.core_count >= 1
    assert 0 < s.memory.available_bytes <= s.memory.total_bytes
    assert s.disk.read_bytes_per_s is not None and s.disk.read_bytes_per_s >= 0
    assert s.top_processes, "expected at least one process"
    json.dumps(s.to_dict())  # must be JSONL-loggable


def test_short_lived_child_cpu_and_ancestry_are_visible():
    # A busy child born between samples must show real CPU (not 0%) and carry its parent's
    # cmdline in `ancestors` — the classifier depends on both (cc1 under make, xdist under pytest).
    t = HostTelemetry(top_n=40)
    parent = subprocess.Popen(["sh", "-c", "sleep 0.3; exec python3 -c 'while True: pass' rt_marker_child"],
                              start_new_session=True)
    try:
        time.sleep(1.0)
        procs = t.sample().top_processes
        child = next((p for p in procs if "rt_marker_child" in p.cmdline), None)
        assert child is not None, "busy child missing from top processes"
        assert child.cpu_percent > 30
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
    assert child.ppid is not None


def test_ancestors_include_parent_cmdline():
    t = HostTelemetry(top_n=40)
    proc = subprocess.Popen(["sh", "-c", "python3 -c 'while True: pass' rt_marker_grandchild; true"],
                            start_new_session=True)
    try:
        time.sleep(0.5)
        t.sample()
        time.sleep(0.5)
        child = next(p for p in t.sample().top_processes if "rt_marker_grandchild" in p.cmdline)
        assert child.ancestors and child.ancestors[0].startswith("sh -c python3")
    finally:
        os.killpg(proc.pid, signal.SIGKILL)  # only our own process group
        proc.wait(timeout=5)


def test_host_sample_is_cheap():
    t = HostTelemetry()
    time.sleep(0.2)
    start = time.perf_counter()
    t.sample()
    # The collector must not become a significant load on the machine it measures.
    assert time.perf_counter() - start < 0.5


# --- live containers (skipped if docker is unavailable) --------------------------------

def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


docker_required = pytest.mark.skipif(not _docker_ok(), reason="docker daemon not available")


@docker_required
def test_container_cpu_limit_and_label_are_observed():
    name = f"rt-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--label", "workload=test-cpu",
                    "--cpus", "0.5", "alpine", "sh", "-c", "while :; do :; done"],
                   check=True, capture_output=True)
    try:
        t = ContainerTelemetry()
        time.sleep(2.0)
        snap = t.sample()
        assert snap.available
        c = next(c for c in snap.containers if c.name == name)
        assert c.source == "cgroup"
        assert c.ground_truth_label == "test-cpu"
        assert c.limits.cpu_quota_cores == 0.5
        # A busy loop capped at 0.5 cores: ~50% of one core, and it must be throttled.
        assert 35.0 <= c.cpu_percent <= 60.0
        assert c.cpu_throttled_ms_per_s > 0
        json.dumps(snap.to_dict())
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@docker_required
def test_container_that_exits_is_dropped_cleanly():
    name = f"rt-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "alpine", "sleep", "60"],
                   check=True, capture_output=True)
    t = ContainerTelemetry()
    assert any(c.name == name for c in t.sample().containers)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    snap = t.sample()  # must not raise
    assert snap.available and all(c.name != name for c in snap.containers)
