"""Hand-written evidence for testing the diagnostic crew in isolation (Step 8a).

Shaped like what the pipeline will hand the crew: trigger + classifier result + per-target
telemetry + PRE-SUMMARISED history (Statistics Engine). The numbers come from the real
build_vs_indexer (medium) run measured on this VM; the history entry is invented for the test.
"""

BUILD_VS_INDEXER = {
    "trigger": {"resource": "cpu", "metric": "host.cpu_psi", "value": 41.7, "threshold": 15.6, "persisted_s": 15},
    "workload": {"class": "BUILD", "confidence": 1.0, "dominance": 1.0, "persistence": 1.0},
    "host": {"cpu_percent": 98.8, "cores": 6, "load_1m": 7.5, "cpu_psi": 41.7},
    "targets": [
        {"target_id": "container:rt-build_vs_indexer-build", "workload": "BUILD",
         "cpu_percent": 327.0, "cpu_psi": 11.1, "cpu_weight": 100, "cpu_limit_cores": None},
        {"target_id": "container:rt-build_vs_indexer-indexer-1", "workload": "CODE_INDEXING",
         "cpu_percent": 65.2, "cpu_psi": 3.3, "cpu_weight": 100, "cpu_limit_cores": None},
        {"target_id": "container:rt-build_vs_indexer-indexer-2", "workload": "CODE_INDEXING",
         "cpu_percent": 62.7, "cpu_psi": 3.1, "cpu_weight": 100, "cpu_limit_cores": None},
    ],
    "history": [
        {"workload": "BUILD", "condition": "cpu_contention", "action": "set_container_cpu_weight",
         "target_workload": "CODE_INDEXING", "successes": 8, "failures": 2, "samples": 10,
         "avg_improvement_pct": 12.4, "recent_5_successes": 4},
    ],
}

NO_CONTENTION = {
    "trigger": {"resource": "cpu", "metric": "host.cpu_psi", "value": 4.0, "threshold": 15.6, "persisted_s": 0},
    "workload": {"class": "BUILD", "confidence": 1.0, "dominance": 1.0, "persistence": 1.0},
    "host": {"cpu_percent": 70.0, "cores": 6, "load_1m": 4.1, "cpu_psi": 4.0},
    "targets": [
        {"target_id": "container:rt-build_only-build", "workload": "BUILD",
         "cpu_percent": 395.0, "cpu_psi": 3.8, "cpu_weight": 100, "cpu_limit_cores": None},
    ],
    "history": [],
}


# --- RAM / I/O (Step 8a). Numbers: medians of the Step 4 calibration runs (data/telemetry_log.jsonl,
# session cal-20261007). Workload classes are what the CURRENT classifier (activity = CPU + I/O wait,
# validated 100% on these scenarios) reports; the I/O run itself was logged before that fix, as IDLE.

RAM_PRESSURE = {   # ram_pressure high: 1200M allocated under a 1g cap -> OOM loop inside the container
    "trigger": {"resource": "memory", "metric": "targets.oom_kills_delta", "value": 42, "window_s": 45},
    "workload": {"class": "IDLE"},
    "host": {"mem_used_pct": 45.0, "mem_avail_pct": 55.0, "mem_psi": 0.0, "swap_total_mib": 0},
    "thresholds": {"mem_psi": 10.0, "mem_avail_min_pct": 10.0},
    "targets": [
        {"target_id": "container:rt-ram_pressure-hog", "workload": "UNKNOWN", "mem_mib": 570.3,
         "memory_limit_mib": 1024, "oom_kills_delta": 42, "limit_hits_delta": 1553, "mem_psi": 0.0,
         "cpu_percent": 100.0},
    ],
}

RAM_OK = {         # ram_pressure low: 512M under a 1g cap, nothing killed
    "trigger": {"resource": "memory", "metric": "host.mem_avail_pct", "value": 56.0, "window_s": 45},
    "workload": {"class": "IDLE"},
    "host": {"mem_used_pct": 44.0, "mem_avail_pct": 56.0, "mem_psi": 0.0, "swap_total_mib": 0},
    "thresholds": {"mem_psi": 10.0, "mem_avail_min_pct": 10.0},
    "targets": [
        {"target_id": "container:rt-ram_pressure-hog", "workload": "IDLE", "mem_mib": 516.4,
         "memory_limit_mib": 1024, "oom_kills_delta": 0, "limit_hits_delta": 0, "mem_psi": 0.0, "cpu_percent": 0.0},
    ],
}

IO_CONTENTION = {  # indexer_vs_io medium: the indexer starves while an io-hog writes ~138 MiB/s
    "trigger": {"resource": "io", "metric": "host.io_psi", "value": 45.1, "threshold": 18.4, "persisted_s": 15},
    "workload": {"class": "CODE_INDEXING", "confidence": 1.0},
    "host": {"io_psi": 45.1, "iowait": 38.3, "disk_read_mib_s": 2.6, "disk_write_mib_s": 130.0, "disk_util": 78.7},
    "targets": [
        {"target_id": "container:rt-indexer_vs_io-indexer", "workload": "CODE_INDEXING", "state": "running",
         "io_read_mib_s": 2.6, "io_write_mib_s": 0.0, "io_psi": 42.7, "cpu_percent": 38.2},
        {"target_id": "container:rt-indexer_vs_io-hog", "workload": "UNKNOWN", "state": "running",
         "io_read_mib_s": 0.0, "io_write_mib_s": 137.6, "io_psi": 43.0, "cpu_percent": 30.3},
    ],
    "history": [],
}

IO_QUIET = {       # indexer_only: the indexer alone reads ~6.5 MiB/s, no contention
    "trigger": {"resource": "io", "metric": "host.io_psi", "value": 2.5, "threshold": 18.4, "persisted_s": 0},
    "workload": {"class": "CODE_INDEXING", "confidence": 1.0},
    "host": {"io_psi": 2.5, "iowait": 1.1, "disk_read_mib_s": 6.5, "disk_write_mib_s": 0.0, "disk_util": 12.6},
    "targets": [
        {"target_id": "container:rt-indexer_only-indexer", "workload": "CODE_INDEXING", "state": "running",
         "io_read_mib_s": 6.5, "io_write_mib_s": 0.0, "io_psi": 3.8, "cpu_percent": 85.8},
    ],
    "history": [],
}


# --- COMBINED evidence (Step 8c): the single dict a cycle hands to the Supervisor and, projected per
# agent (evidence.project_evidence), to the diagnostic crew. CPU/IO numbers are the measured medians of
# build_vs_indexer (medium) and indexer_vs_io (medium); the RAM figures (18% available, PSI 6.2) and the
# history entries are INVENTED for the test — the history is the "RAM pressure often precedes CPU
# saturation for BUILD" correlation the brief says the Supervisor must be able to use.

_THRESHOLDS = {"cpu_psi": 15.6, "mem_psi": 10.0, "mem_avail_min_pct": 10.0, "io_psi": 18.4}

COMBINED_CPU_RAM = {
    "trigger": {"resource": "cpu", "metric": "host.cpu_psi", "value": 41.7, "threshold": 15.6, "persisted_s": 15},
    "workload": {"class": "BUILD", "confidence": 1.0, "dominance": 1.0, "persistence": 1.0},
    "host": {"cpu_percent": 98.8, "cores": 6, "load_1m": 7.5, "cpu_psi": 41.7,
             "mem_used_pct": 82.0, "mem_avail_pct": 18.0, "mem_psi": 6.2, "swap_total_mib": 0,
             "io_psi": 1.2, "iowait": 0.9, "disk_read_mib_s": 0.1, "disk_write_mib_s": 0.0, "disk_util": 1.0},
    "thresholds": _THRESHOLDS,
    "targets": [
        {"target_id": "container:rt-build_vs_indexer-build", "workload": "BUILD", "state": "running",
         "cpu_percent": 327.0, "cpu_psi": 11.1, "cpu_weight": 100, "cpu_limit_cores": None,
         "mem_mib": 235.9, "memory_limit_mib": None, "oom_kills_delta": 0, "limit_hits_delta": 0, "mem_psi": 0.0,
         "io_read_mib_s": 0.0, "io_write_mib_s": 0.1, "io_psi": 0.0},
        {"target_id": "container:rt-build_vs_indexer-indexer-1", "workload": "CODE_INDEXING", "state": "running",
         "cpu_percent": 65.2, "cpu_psi": 3.3, "cpu_weight": 100, "cpu_limit_cores": None,
         "mem_mib": 298.5, "memory_limit_mib": None, "oom_kills_delta": 0, "limit_hits_delta": 0, "mem_psi": 0.0,
         "io_read_mib_s": 6.2, "io_write_mib_s": 0.0, "io_psi": 5.4},
        {"target_id": "container:rt-build_vs_indexer-indexer-2", "workload": "CODE_INDEXING", "state": "running",
         "cpu_percent": 62.7, "cpu_psi": 3.1, "cpu_weight": 100, "cpu_limit_cores": None,
         "mem_mib": 294.4, "memory_limit_mib": None, "oom_kills_delta": 0, "limit_hits_delta": 0, "mem_psi": 0.0,
         "io_read_mib_s": 6.0, "io_write_mib_s": 0.0, "io_psi": 5.1},
    ],
    "history": [
        {"resource": "cpu", "workload": "BUILD", "condition": "cpu_contention", "action": "set_container_cpu_weight",
         "target_workload": "CODE_INDEXING", "successes": 8, "failures": 2, "samples": 10,
         "avg_improvement_pct": 12.4, "recent_5_successes": 4},
        {"resource": "ram", "workload": "BUILD", "observation": "ram_pressure_preceded_cpu_saturation",
         "count": 7, "of_cpu_triggers": 10},
    ],
}

COMBINED_IO = {
    "trigger": {"resource": "io", "metric": "host.io_psi", "value": 45.1, "threshold": 18.4, "persisted_s": 15},
    "workload": {"class": "CODE_INDEXING", "confidence": 1.0, "dominance": 1.0, "persistence": 1.0},
    "host": {"cpu_percent": 16.2, "cores": 6, "load_1m": 2.1, "cpu_psi": 0.7,
             "mem_used_pct": 36.1, "mem_avail_pct": 63.9, "mem_psi": 0.0, "swap_total_mib": 0,
             "io_psi": 45.1, "iowait": 38.3, "disk_read_mib_s": 2.6, "disk_write_mib_s": 130.0, "disk_util": 78.7},
    "thresholds": _THRESHOLDS,
    "targets": [
        {"target_id": "container:rt-indexer_vs_io-indexer", "workload": "CODE_INDEXING", "state": "running",
         "cpu_percent": 38.2, "cpu_psi": 0.0, "cpu_weight": 100, "cpu_limit_cores": None,
         "mem_mib": 309.7, "memory_limit_mib": None, "oom_kills_delta": 0, "limit_hits_delta": 0, "mem_psi": 0.0,
         "io_read_mib_s": 2.6, "io_write_mib_s": 0.0, "io_psi": 42.7},
        {"target_id": "container:rt-indexer_vs_io-hog", "workload": "UNKNOWN", "state": "running",
         "cpu_percent": 30.3, "cpu_psi": 0.1, "cpu_weight": 100, "cpu_limit_cores": None,
         "mem_mib": 4.5, "memory_limit_mib": None, "oom_kills_delta": 0, "limit_hits_delta": 0, "mem_psi": 0.0,
         "io_read_mib_s": 0.0, "io_write_mib_s": 137.6, "io_psi": 43.0},
    ],
    "history": [],
}
