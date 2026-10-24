"""Per-agent views of one combined evidence dict (Step 8c).

The pipeline builds ONE evidence dict per cycle (trigger, workload, host, thresholds, targets, history).
Each diagnostic agent only needs its own resource, and every extra field costs prompt tokens on Groq's
8000 tokens/min budget — so each agent gets a projection, and its deterministic checks run on that
same projection (a model can only be held to what it was shown).
"""

from __future__ import annotations

HOST_FIELDS = {
    "cpu": ("cpu_percent", "cores", "load_1m", "cpu_psi"),
    "ram": ("mem_used_pct", "mem_avail_pct", "mem_psi", "swap_total_mib"),
    "io": ("io_psi", "iowait", "disk_read_mib_s", "disk_write_mib_s", "disk_util"),
}
TARGET_FIELDS = {
    "cpu": ("workload", "state", "cpu_percent", "cpu_psi", "cpu_weight", "cpu_limit_cores"),
    "ram": ("workload", "state", "mem_mib", "memory_limit_mib", "oom_kills_delta", "limit_hits_delta", "mem_psi"),
    "io": ("workload", "state", "io_read_mib_s", "io_write_mib_s", "io_psi"),
}
THRESHOLD_FIELDS = {"cpu": ("cpu_psi",), "ram": ("mem_psi", "mem_avail_min_pct"), "io": ("io_psi",)}
ALWAYS = ("trigger", "workload")


def _pick(d: dict, keys) -> dict:
    return {k: d[k] for k in keys if k in d}      # None is kept: cpu_limit_cores=null means "unlimited"


def project_evidence(agent: str, evidence: dict) -> dict:
    if agent not in HOST_FIELDS:
        raise ValueError(f"unknown agent {agent!r}")
    out = {k: evidence[k] for k in ALWAYS if k in evidence}
    out["host"] = _pick(evidence.get("host", {}), HOST_FIELDS[agent])
    if evidence.get("thresholds"):
        out["thresholds"] = _pick(evidence["thresholds"], THRESHOLD_FIELDS[agent])
    out["targets"] = [{"target_id": t["target_id"], **_pick(t, TARGET_FIELDS[agent])}
                      for t in evidence.get("targets", [])]
    if agent != "ram":      # RAM never acts, so it has no use for action history
        out["history"] = [h for h in evidence.get("history", []) if h.get("resource") == agent]
    return out
