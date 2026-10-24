"""Validate the rule-based classifier against Step 2's synthetic scenarios.

For each scenario: start it, let it warm up, sample host + container telemetry at 1 Hz, and
classify every tick over the configured window. Host-level results are compared with the
scenario's expected workload / fallback; per-container results with each container's
`workload=` ground-truth label. The label is read HERE, never inside the classifier.

Usage (repo root):
  ./venv/bin/python scripts/validate_classifier.py               # all scenarios
  ./venv/bin/python scripts/validate_classifier.py build_only idle
Results are appended to data/classifier_validation.jsonl.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import docker

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import run_scenario as rs  # noqa: E402
from resourcetuner.classifier.rules import (  # noqa: E402
    BUILD, CODE_INDEXING, IDLE, TESTING, UNKNOWN, ClassifierConfig, classify,
)
from resourcetuner.telemetry.containers import ContainerTelemetry  # noqa: E402
from resourcetuner.telemetry.host import HostTelemetry  # noqa: E402

# Ground-truth label -> acceptable per-container classes. Hogs run no developer tool, so the
# correct answer is "not a dev workload": UNKNOWN when busy, IDLE when nearly idle.
LABEL_EXPECT = {
    "build": {BUILD},
    "testing": {TESTING},
    "code_indexing": {CODE_INDEXING},
    "idle": {IDLE},
    "cpu_hog": {UNKNOWN},
    "io_hog": {UNKNOWN, IDLE},
    "ram_hog": {IDLE, UNKNOWN},
}

# (scenario, level, acceptable host workloads, expected fallback: True/False/None = either)
CASES = [
    ("idle", None, {IDLE}, False),
    ("build_only", None, {BUILD}, False),
    ("test_only", None, {TESTING}, False),
    ("indexer_only", None, {CODE_INDEXING}, False),
    ("build_vs_cpu", "medium", {BUILD}, False),
    ("build_vs_indexer", "low", {BUILD}, False),
    ("test_vs_cpu", "medium", {TESTING}, False),
    ("build_and_test", None, {BUILD, TESTING}, True),
    ("cpu_pressure", "medium", {UNKNOWN}, True),
    ("io_pressure", "medium", {IDLE, UNKNOWN}, None),
    ("ram_pressure", "low", {IDLE}, False),
    # Added in Step 4: calibration showed an I/O-starved indexer can fall below min_active CPU.
    ("indexer_vs_io", "medium", {CODE_INDEXING}, False),
]

WARMUP_S = 12
SAMPLE_S = 20


def run_case(client, name, level, expect_host, expect_fallback, cfg) -> dict:
    scenario = rs.load_scenario(name, level)
    rs.start_scenario(client, scenario, replace=True)
    try:
        host, ctrs = HostTelemetry(top_n=25), ContainerTelemetry(client)
        time.sleep(WARMUP_S)
        host.sample(); ctrs.sample()  # fresh deltas after warm-up
        window, ticks = [], []
        for _ in range(SAMPLE_S):
            time.sleep(1.0)
            window.append(host.sample())
            cs = ctrs.sample()
            if len(window) < cfg.window_samples:
                continue
            r = classify(window, cs, cfg)
            labels = {c.name: c.ground_truth_label for c in cs.containers}  # validation only
            ticks.append({
                "workload": r.workload, "fallback": r.needs_llm_fallback, "reasons": r.fallback_reasons,
                "dominance": r.dominance, "persistence": r.persistence,
                "unattributed": r.unattributed_activity_percent,
                "containers": {n: (labels.get(n), cls) for n, cls in r.containers.items()},
            })
    finally:
        rs.stop_all(client)

    host_ok = sum(t["workload"] in expect_host for t in ticks)
    fb_ok = sum(expect_fallback is None or t["fallback"] == expect_fallback for t in ticks)
    ctr_total = ctr_ok = 0
    for t in ticks:
        for label, cls in t["containers"].values():
            if label in LABEL_EXPECT:
                ctr_total += 1
                ctr_ok += cls in LABEL_EXPECT[label]
    seen = sorted({t["workload"] for t in ticks})
    reasons = sorted({r.split(":")[0] for t in ticks for r in t["reasons"]})
    return {
        "scenario": name, "level": level, "ticks": len(ticks),
        "host_accuracy": round(host_ok / len(ticks), 2) if ticks else None,
        "fallback_accuracy": round(fb_ok / len(ticks), 2) if ticks else None,
        "container_accuracy": round(ctr_ok / ctr_total, 2) if ctr_total else None,
        "host_workloads_seen": seen, "fallback_reasons_seen": reasons,
        "expected_host": sorted(expect_host), "expected_fallback": expect_fallback,
        "ticks_detail": ticks,
    }


def main(argv: list[str]) -> int:
    cfg = ClassifierConfig.load()
    client = docker.from_env(timeout=rs.DOCKER_TIMEOUT_S)
    cases = [c for c in CASES if not argv or c[0] in argv]
    out = REPO / "data" / "classifier_validation.jsonl"
    out.parent.mkdir(exist_ok=True)
    print(f"{'scenario':<18} {'level':<7} {'host':>5} {'fallbk':>6} {'ctrs':>5}  seen / reasons", flush=True)
    for name, level, expect_host, expect_fb in cases:
        res = run_case(client, name, level, expect_host, expect_fb, cfg)
        res["ts"] = time.time()
        with out.open("a") as f:
            f.write(json.dumps(res) + "\n")
        fmt = lambda x: "-" if x is None else f"{x:.2f}"  # noqa: E731
        print(f"{name:<18} {level or '-':<7} {fmt(res['host_accuracy']):>5} {fmt(res['fallback_accuracy']):>6} "
              f"{fmt(res['container_accuracy']):>5}  {res['host_workloads_seen']} / {res['fallback_reasons_seen']}",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
