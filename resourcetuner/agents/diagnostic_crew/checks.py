"""Deterministic checks shared by the CPU, RAM and I/O diagnostic agents.

Plain code, no LLM. Each agent's checker calls `common_issues` and then adds its own rules.
The rules came from real failures: a local 3B model named the BUILD as culprit, repeated one
metric six times, proposed a no-op action, and cited `workload.class = 1.0` (the class is the
string "BUILD"; 1.0 is the confidence) — the number existed somewhere in the evidence, so the
first checker passed it. A cited value must now match the metric it is cited for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PROTECTED_WORKLOADS = ("BUILD", "TESTING", "DEV_SERVER")
SECTIONS = ("host", "workload", "trigger", "thresholds", "history", "targets")


@dataclass
class CheckResult:
    ok: bool
    issues: list[str] = field(default_factory=list)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def metric_rows(evidence: dict) -> list[tuple[str, str | None, str, float]]:
    """Every numeric leaf of the evidence as (section, target_id or None, field, value)."""
    rows = []
    for section, val in evidence.items():
        if isinstance(val, dict):
            rows += [(section, None, k, float(v)) for k, v in val.items() if _is_num(v)]
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    tid = item.get("target_id") if section == "targets" else None
                    rows += [(section, tid, k, float(v)) for k, v in item.items() if _is_num(v)]
    return rows


def _target_matches(target_id: str, scope: str) -> bool:
    t, s = target_id.lower().removeprefix("container:"), scope.lower().removeprefix("container:")
    return t == s or t.endswith(s)          # 'indexer-1' / 'hog' name a target by its suffix


def split_metric(metric: str) -> tuple[str, str]:
    """'host.cpu_psi' -> ('host', 'cpu_psi'); 'container:rt-x-build cpu_percent' -> ('container:rt-x-build',
    'cpu_percent'); 'cpu_psi' -> ('', 'cpu_psi')."""
    m = metric.strip().lower()
    i = max(m.rfind("."), m.rfind(" "))
    return (m[:i].strip(), m[i + 1:]) if i != -1 else ("", m)


def cited_value_issue(metric: str, value: float, evidence: dict) -> str | None:
    """None if `metric` names a numeric field of the evidence whose value is `value`."""
    # The trigger states a metric/value pair itself (trigger.metric = "host.cpu_psi", trigger.value = 41.7),
    # so an agent whose projection does not carry host.cpu_psi may still cite exactly that pair.
    trig = evidence.get("trigger") or {}
    if (isinstance(trig.get("metric"), str) and _is_num(trig.get("value")) and metric.strip().lower() == trig["metric"].lower()
            and abs(value - trig["value"]) <= max(0.05, 0.01 * abs(trig["value"]))):
        return None
    scope, leaf = split_metric(metric)
    rows = metric_rows(evidence)
    if not scope:
        pool = rows
    elif scope in SECTIONS:
        pool = [r for r in rows if r[0] == scope]
    else:
        pool = [r for r in rows if r[1] and _target_matches(r[1], scope)]
        if not pool:
            return f"evidence metric {metric!r}: {scope!r} is neither a section nor a target in the evidence"
    candidates = [r[3] for r in pool if r[2] == leaf]
    if not candidates:
        return f"evidence metric {metric!r} does not exist as a numeric field in the evidence"
    if not any(abs(value - k) <= max(0.05, 0.01 * abs(k)) for k in candidates):
        shown = ", ".join(f"{k:g}" for k in sorted(set(candidates))[:4])
        return f"evidence value {value:g} does not match metric {metric!r} (the evidence has {shown})"
    return None


def targets_by_id(evidence: dict) -> dict[str, dict]:
    return {t["target_id"]: t for t in evidence.get("targets", [])}


def is_protected(target_id: str, evidence: dict) -> bool:
    """BUILD/TESTING/DEV_SERVER, or whatever the host is currently running."""
    t = targets_by_id(evidence).get(target_id, {})
    w = t.get("workload")
    host = (evidence.get("workload") or {}).get("class")
    return w in PROTECTED_WORKLOADS or (w is not None and w == host)


def common_issues(culprit_ids: list[str], evidence_items, evidence: dict) -> list[str]:
    issues = []
    by_id = targets_by_id(evidence)
    for tid in culprit_ids:
        if tid not in by_id:
            issues.append(f"culprit {tid!r} is not a target in the evidence")
        elif is_protected(tid, evidence):
            issues.append(f"culprit {tid!r} runs the protected/active workload {by_id[tid].get('workload')}")
    seen = set()
    for e in evidence_items:
        if (problem := cited_value_issue(e.metric, e.value, evidence)) is not None:
            issues.append(problem)
        if (e.metric, e.value) in seen:
            issues.append(f"evidence item {e.metric}={e.value} is repeated")
        seen.add((e.metric, e.value))
    return issues
