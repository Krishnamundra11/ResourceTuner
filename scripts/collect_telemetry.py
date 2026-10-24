"""Collect labelled telemetry for threshold calibration (Step 4).

Every second, one compact JSON line is appended to data/telemetry_log.jsonl with:
  run_id, state ("idle" | "load" | "congestion"), scenario, level, notes, tick,
  host metrics, classifier output, and per-container metrics.

States
  idle        nothing of ours running (the VM's normal background: desktop, IDE, etc.)
  load        one developer workload alone, no competitor (busy but uncontended)
  congestion  a workload plus a competitor, or a resource hog, at a named intensity level
The brief's calibration uses idle vs congestion; "load" is added so that thresholds are also
checked against a busy-but-uncontended machine (a build using its cores is not contention).

Usage (repo root):
  ./venv/bin/python scripts/collect_telemetry.py idle --duration 480 --notes "realistic idle: VS Code open"
  ./venv/bin/python scripts/collect_telemetry.py scenario build_vs_cpu --level high --state congestion
  ./venv/bin/python scripts/collect_telemetry.py plan                 # full Step 4 congestion plan
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections import deque
from pathlib import Path

import docker

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import run_scenario as rs  # noqa: E402
from resourcetuner.classifier.rules import ClassifierConfig, classify  # noqa: E402
from resourcetuner.telemetry.containers import ContainersSnapshot, ContainerTelemetry  # noqa: E402
from resourcetuner.telemetry.host import HostSnapshot, HostTelemetry  # noqa: E402

LOG = REPO / "data" / "telemetry_log.jsonl"
WARMUP_S = 15
SAMPLE_S = 45

# (scenario, level, state). Low levels are designed to stay within the 6 cores
# (e.g. 4 build jobs + 2 hog workers), so they are kept as a separate "light" group in analysis.
PLAN = [
    ("build_only", None, "load"),
    ("test_only", None, "load"),
    ("indexer_only", None, "load"),
    *[("cpu_pressure", lv, "congestion") for lv in ("low", "medium", "high")],
    *[("io_pressure", lv, "congestion") for lv in ("low", "medium", "high")],
    *[("ram_pressure", lv, "congestion") for lv in ("low", "medium", "high")],
    *[("build_vs_cpu", lv, "congestion") for lv in ("low", "medium", "high")],
    *[("test_vs_cpu", lv, "congestion") for lv in ("low", "medium", "high")],
    *[("build_vs_indexer", lv, "congestion") for lv in ("low", "medium", "high")],
    *[("indexer_vs_io", lv, "congestion") for lv in ("low", "medium", "high")],
    *[("build_vs_ram", lv, "congestion") for lv in ("low", "medium", "high")],
    ("build_and_test", None, "congestion"),
]


def _psi(p, full=False):
    if p is None:
        return None
    return p.full_avg10 if full else p.some_avg10


def compact(host: HostSnapshot, ctrs: ContainersSnapshot, window: list[HostSnapshot],
            cfg: ClassifierConfig) -> dict:
    mib = 2 ** 20
    rec = {
        "ts": round(host.timestamp, 2),
        "host": {
            "cpu": host.cpu.percent, "iowait": host.cpu.iowait, "load1": round(host.cpu.load_avg_1m, 2),
            "psi_cpu": _psi(host.cpu.psi), "psi_cpu60": host.cpu.psi.some_avg60 if host.cpu.psi else None,
            "psi_mem": _psi(host.memory.psi), "psi_mem_full": _psi(host.memory.psi, True),
            "psi_io": _psi(host.disk.psi), "psi_io_full": _psi(host.disk.psi, True),
            "mem_pct": host.memory.percent,
            "mem_avail_pct": round(100 * host.memory.available_bytes / host.memory.total_bytes, 2),
            "disk_r_mib": round((host.disk.read_bytes_per_s or 0) / mib, 2),
            "disk_w_mib": round((host.disk.write_bytes_per_s or 0) / mib, 2),
            "disk_util": host.disk.max_util_percent,
            "delayacct": host.delayacct_enabled,  # False -> classifier activity was CPU-only
        },
        "containers": [
            {
                "name": c.name, "label": c.ground_truth_label, "cpu": c.cpu_percent,
                "throttled_ms": c.cpu_throttled_ms_per_s,
                "mem_mib": round(c.memory_working_set_bytes / mib, 1) if c.memory_working_set_bytes is not None else None,
                "io_r_mib": round((c.io_read_bytes_per_s or 0) / mib, 2),
                "io_w_mib": round((c.io_write_bytes_per_s or 0) / mib, 2),
                "psi_cpu": _psi(c.cpu_psi), "psi_mem": _psi(c.memory_psi), "psi_io": _psi(c.io_psi),
                "oom_kills": c.memory_oom_kills, "max_events": c.memory_max_events,
            }
            for c in ctrs.containers
        ],
        "cls": None,
    }
    if len(window) >= cfg.window_samples:
        r = classify(window, ctrs, cfg)
        rec["cls"] = {
            "workload": r.workload, "confidence": r.confidence, "dominance": r.dominance,
            "persistence": r.persistence, "fallback": r.needs_llm_fallback,
            "reasons": sorted({x.split(":")[0] for x in r.fallback_reasons}),
            "class_cpu": {k: v.cpu_percent for k, v in r.evidence.items()},
            "class_activity": {k: v.activity_percent for k, v in r.evidence.items()},
            "unattributed": r.unattributed_activity_percent,
            "containers": r.containers,
        }
    return rec


def record(duration_s: float, meta: dict, warmup_s: float = 0) -> int:
    cfg = ClassifierConfig.load()
    host, ctrs = HostTelemetry(top_n=25), ContainerTelemetry()
    time.sleep(warmup_s)
    host.sample(); ctrs.sample()  # fresh deltas after warm-up
    window: deque[HostSnapshot] = deque(maxlen=cfg.window_samples)
    LOG.parent.mkdir(exist_ok=True)
    n = 0
    next_tick = time.monotonic()
    with LOG.open("a") as f:
        while n < duration_s:
            next_tick += 1.0
            time.sleep(max(0.0, next_tick - time.monotonic()))
            h, c = host.sample(), ctrs.sample()
            window.append(h)
            f.write(json.dumps({**meta, "tick": n, **compact(h, c, list(window), cfg)}) + "\n")
            f.flush()
            n += 1
    return n


def run_scenario_and_record(client, session: str, scenario: str, level: str | None, state: str,
                            notes: str, duration_s: float) -> int:
    sc = rs.load_scenario(scenario, level)
    rs.start_scenario(client, sc, replace=True)
    try:
        meta = {"run_id": f"{session}:{scenario}:{level or '-'}", "session": session, "state": state,
                "scenario": scenario, "level": level, "notes": notes or sc["description"][:160]}
        return record(duration_s, meta, warmup_s=WARMUP_S)
    finally:
        rs.stop_all(client)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default=time.strftime("cal-%Y%m%d-%H%M%S"),
                    help="groups runs that belong to one calibration session")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("idle", help="record the idle baseline (no managed containers)")
    i.add_argument("--duration", type=float, default=480)
    i.add_argument("--variant", default="realistic", help="realistic (IDE open) | clean (IDE closed)")
    i.add_argument("--notes", default="")
    s = sub.add_parser("scenario", help="record one scenario")
    s.add_argument("scenario")
    s.add_argument("--level")
    s.add_argument("--state", choices=["load", "congestion"], required=True)
    s.add_argument("--duration", type=float, default=SAMPLE_S)
    s.add_argument("--notes", default="")
    p = sub.add_parser("plan", help="record every load/congestion scenario in PLAN")
    p.add_argument("--duration", type=float, default=SAMPLE_S)
    args = ap.parse_args(argv)

    client = docker.from_env(timeout=rs.DOCKER_TIMEOUT_S)
    if args.cmd == "idle":
        if rs.managed_containers(client):
            print("error: managed containers are running; `run_scenario.py stop` first", file=sys.stderr)
            return 2
        meta = {"run_id": f"{args.session}:idle-{args.variant}", "session": args.session, "state": "idle",
                "scenario": f"idle-{args.variant}", "level": None,
                "notes": args.notes or f"{args.variant} idle baseline"}
        n = record(args.duration, meta)
        print(f"recorded {n} idle ticks -> {LOG} (session {args.session})")
    elif args.cmd == "scenario":
        n = run_scenario_and_record(client, args.session, args.scenario, args.level, args.state,
                                    args.notes, args.duration)
        print(f"recorded {n} ticks -> {LOG}")
    else:
        for k, (scenario, level, state) in enumerate(PLAN, 1):
            start = time.monotonic()
            n = run_scenario_and_record(client, args.session, scenario, level, state, "", args.duration)
            print(f"[{k}/{len(PLAN)}] {scenario:<17} {level or '-':<7} {state:<10} {n} ticks "
                  f"({time.monotonic() - start:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
