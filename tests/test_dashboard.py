import json

from resourcetuner.classifier.rules import classify
from resourcetuner.dashboard.main import build_payload
from resourcetuner.telemetry.containers import ContainerLimits, ContainersSnapshot, ContainerStats
from resourcetuner.telemetry.host import CPUStats, DiskStats, HostSnapshot, MemoryStats, ProcessInfo

MIB = 2 ** 20


def _host(*procs):
    return HostSnapshot(
        timestamp=100.0, interval_s=1.0,
        cpu=CPUStats(42.0, [], 0, 0, 1.5, 0, 2.0, 6, None),
        memory=MemoryStats(8 * 1024 * MIB, 6 * 1024 * MIB, 25.0, 0, 0, None),
        disk=DiskStats(2 * MIB, 3 * MIB, 0, 0, 40.0, None),
        top_processes=list(procs),
    )


def _proc(cmd, cpu, cid):
    return ProcessInfo(1, cmd.split()[0], cmd, "root", cpu, 0, 1, 0, None, None, None, cid, 1, [])


def test_payload_shape_and_values():
    cid = "c" * 64
    host = _host(_proc("cc1 a.c", 250, cid))
    ctr = ContainerStats(cid, "builder", "img", "running", "build", "cgroup", 250.0, 0, 0,
                         512 * MIB, 0, 0, MIB, 0, 3, None, None, None,
                         ContainerLimits(None, 100, 100, 1024 * MIB))
    cs = ContainersSnapshot(100.0, True, None, [ctr])
    cls = classify([host] * 5, cs)
    p = build_payload(host, cs, cls, [{"t": 100.0, "cpu": 42.0, "mem": 25.0, "workload": cls.workload}])

    json.dumps(p)  # must be serializable as-is
    assert p["host"]["cpu_percent"] == 42.0 and p["host"]["cores"] == 6
    assert p["host"]["disk_read_mib_s"] == 2.0 and p["host"]["disk_write_mib_s"] == 3.0
    assert p["classification"]["workload"] == "BUILD"
    assert p["classification"]["confidence"] == 1.0
    assert p["classification"]["needs_llm_fallback"] is False
    row = p["containers"][0]
    assert row["name"] == "builder" and row["classified_as"] == "BUILD"
    assert row["memory_mib"] == 512.0 and row["memory_limit_mib"] == 1024
    assert row["io_read_mib_s"] == 1.0
    assert row["ground_truth_label"] == "build"  # displayed for validation only
    assert len(p["history"]) == 1


def test_payload_without_docker():
    cs = ContainersSnapshot(0.0, False, "docker unavailable: boom", [])
    host = _host()
    p = build_payload(host, cs, classify([host], cs), [])
    assert p["containers"] == [] and p["docker"] == {"available": False, "error": "docker unavailable: boom"}
    assert p["classification"]["workload"] == "IDLE"
