"""Read-only access to records written before the event layer existed.

Deliberately imports nothing that can act: no runner, no controller, no agents. Reading a
historical run therefore cannot rerun agents, call an LLM, touch a container or change a policy
— that property is structural, not a convention.

`data/vertical_slice_log.jsonl` holds ONE summary row per cycle, written at the end of the
cycle. It has no per-stage events and no per-stage timestamps, so `legacy_cycle_stages()`
reports exactly what the row proves and marks everything else `evidence_unavailable`. It never
invents an event: a stage is only "completed" when the row carries its result.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from resourcetuner.pipeline import stages as S

REPO = Path(__file__).resolve().parents[2]
SLICE_LOG = REPO / "data" / "vertical_slice_log.jsonl"
ACTIONS_DB = REPO / "data" / "actions.db"
POLICY_DB = REPO / "data" / "policy.db"


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows[-limit:] if limit else rows


def legacy_cycles(limit: int = 50, offset: int = 0) -> dict:
    rows = _read_jsonl(SLICE_LOG)
    rows.reverse()                                   # newest first
    page = rows[offset:offset + limit]
    return {
        "source": "data/vertical_slice_log.jsonl",
        "read_only": True,
        "note": "Cycle summaries written before structured events existed: per-stage evidence was not persisted.",
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "cycles": [_legacy_summary(r) for r in page],
    }


def _legacy_summary(row: dict) -> dict:
    ev = row.get("evaluation") or {}
    dec = (row.get("decision") or {}).get("chosen_action")
    ex = row.get("execution") or {}
    return {
        "cycle": row.get("cycle"),
        "timestamp": row.get("timestamp"),
        "duration_s": row.get("duration_s"),
        "trigger": row.get("trigger"),
        "delegated_to": (row.get("delegation") or {}).get("resources"),
        "candidate_count": len(row.get("candidates") or []),
        "action": dec,
        "execution_status": ex.get("status"),
        "gate_reasons": ex.get("reasons"),
        "verdict": ev.get("verdict"),
        "improvement_pct": ev.get("improvement_pct"),
        "rolled_back": ev.get("rolled_back"),
        "before_s": (row.get("before_build") or {}).get("duration_s"),
        "after_s": (row.get("after_build") or {}).get("duration_s"),
        "llm_degraded": row.get("llm_degraded"),
        "promote": (row.get("meta") or {}).get("promote"),
    }


def legacy_cycle_detail(cycle: int) -> dict | None:
    for row in _read_jsonl(SLICE_LOG):
        if row.get("cycle") == cycle:
            return {"source": "data/vertical_slice_log.jsonl", "read_only": True,
                    "cycle": cycle, "summary": _legacy_summary(row),
                    "stages": legacy_cycle_stages(row), "raw": row}
    return None


def legacy_cycle_stages(row: dict) -> list[dict]:
    """Map one legacy summary row onto the canonical stage list.

    Only fields the row actually contains become stages with a status; everything else is
    `evidence_unavailable`, because those details were never written.
    """
    ex = row.get("execution") or {}
    ev = row.get("evaluation") or {}
    status_of_exec = ex.get("status")
    dec = row.get("decision") or {}
    chosen = dec.get("chosen_action")
    meta = row.get("meta") or {}

    def stage(name: str, status: str, summary: str, payload: dict | None = None) -> dict:
        return {"stage": name, "status": status, "summary": summary, "payload": payload or {},
                "ts": None, "duration_ms": None, "derived_from": "cycle_summary"}

    unavailable = lambda name, why: stage(name, S.EVIDENCE_UNAVAILABLE, why)  # noqa: E731

    out = [
        unavailable(S.WARMUP, "warm-up build was recorded in the summary only")
        if not row.get("warm_up_build") else
        stage(S.WARMUP, S.COMPLETED, f"warm-up {row['warm_up_build'].get('duration_s')}s (discarded)",
              {"record": row["warm_up_build"]}),
    ]
    b = row.get("before_build")
    out.append(stage(S.BASELINE, S.COMPLETED, f"before = {b.get('duration_s')}s", {"record": b})
               if b else unavailable(S.BASELINE, "no before-measurement in this row"))
    t = row.get("trigger")
    out.append(stage(S.TRIGGER, S.COMPLETED,
                     f"{t.get('metric')} {t.get('value')} >= {t.get('threshold')}", {"trigger": t})
               if t else unavailable(S.TRIGGER, "no trigger recorded"))
    out.append(unavailable(S.CLASSIFY, "classifier output was not persisted for this cycle"))
    d = row.get("delegation") or {}
    out.append(stage(S.SUPERVISOR, S.COMPLETED, f"delegated to {d.get('resources')}", {"delegation": d})
               if d else unavailable(S.SUPERVISOR, "no delegation recorded"))
    findings = row.get("findings")
    out.append(
        stage(S.DIAGNOSTICS, S.COMPLETED if findings else S.SKIPPED,
             "; ".join(f"{f.get('agent')}: "
                       f"{'contention' if (f.get('diagnosis') or {}).get('contention') else 'no contention'}"
                       f"{'' if f.get('check_ok') else ' (checks failed)'}" for f in findings)
             if findings else "no specialist was consulted",
             {"findings": findings})
        if findings is not None else
        # Rows from before 2026-10-25's fix recorded only the filtered `candidates`, never the
        # per-agent findings (diagnosis, cited evidence, check result, provenance) they came
        # from — that evidence genuinely does not exist for those cycles and is never invented.
        unavailable(S.DIAGNOSTICS, "per-agent diagnoses were not persisted; only the filtered candidate actions were"))
    out.append(stage(S.DECISION, S.COMPLETED if chosen else S.SKIPPED,
                     (f"{chosen.get('tool')} on {chosen.get('target_id')}" if chosen else "no_action"),
                     {"decision": dec, "candidates": row.get("candidates")})
               if dec else unavailable(S.DECISION, "no decision recorded"))
    gate_map = {"applied": S.COMPLETED, "rejected": S.REJECTED, "blocked": S.REJECTED,
                "no_action": S.SKIPPED}
    out.append(stage(S.SAFETY_GATE, gate_map.get(status_of_exec, S.FAILED),
                     ("; ".join(ex.get("reasons", [])) if ex.get("reasons") else str(status_of_exec)),
                     {"execution": ex})
               if ex else unavailable(S.SAFETY_GATE, "no execution result recorded"))
    if status_of_exec == "applied":
        out.append(stage(S.CONTROLLER, S.COMPLETED,
                         f"{ex.get('knob')} {ex.get('old_value')} -> {ex.get('new_value')}",
                         {"execution": ex,
                          "cgroup_readback": row.get("indexer_post_action_weights")}))
    else:
        out.append(stage(S.CONTROLLER, S.SKIPPED, "nothing was applied"))
    a = row.get("after_build")
    out.append(stage(S.POST_MEASURE, S.COMPLETED, f"after = {a.get('duration_s')}s", {"record": a})
               if a else stage(S.POST_MEASURE, S.SKIPPED, "no after-measurement (no action applied)"))
    out.append(stage(S.EVALUATION,
                     S.COMPLETED if ev.get("verdict") in ("IMPROVED", "UNCHANGED") else S.REJECTED,
                     f"{ev.get('verdict')} ({ev.get('improvement_pct')}%)"
                     f"{', rolled back' if ev.get('rolled_back') else ''}", {"evaluation": ev})
               if ev else stage(S.EVALUATION, S.SKIPPED, "no evaluation (no action applied)"))
    out.append(unavailable(S.POLICY, "the policy-store write was not recorded in this row")
               if not row.get("policy_record") else
               stage(S.POLICY, S.COMPLETED, "policy record updated", {"policy_record": row["policy_record"]}))
    out.append(stage(S.META, S.SKIPPED if not meta.get("promote") else S.COMPLETED,
                     str(meta.get("reasoning", ""))[:200], {"meta": meta})
               if meta else stage(S.META, S.SKIPPED, "meta-agent did not run"))
    return out


def actions(limit: int = 50, offset: int = 0) -> dict:
    if not ACTIONS_DB.is_file():
        return {"total": 0, "actions": [], "limit": limit, "offset": offset, "read_only": True}
    with sqlite3.connect(f"file:{ACTIONS_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) n FROM actions").fetchone()["n"]
        rows = conn.execute("SELECT * FROM actions ORDER BY created_at DESC LIMIT ? OFFSET ?",
                            (limit, offset)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("old_value", "new_value"):
            try:
                d[k] = json.loads(d[k])
            except (ValueError, TypeError):
                pass
        out.append(d)
    return {"source": "data/actions.db", "read_only": True, "total": total, "limit": limit,
            "offset": offset, "actions": out}


def policies() -> dict:
    if not POLICY_DB.is_file():
        return {"total": 0, "policies": [], "read_only": True}
    with sqlite3.connect(f"file:{POLICY_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM policies ORDER BY sample_count DESC, last_updated DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["action_param"] = json.loads(d["action_param"])
        except (ValueError, TypeError):
            pass
        d["promoted"] = bool(d["promoted"])
        d["success_rate"] = round(d["success_count"] / d["sample_count"], 3) if d["sample_count"] else None
        out.append(d)
    return {"source": "data/policy.db", "read_only": True, "total": len(out), "policies": out,
            "promotion_floor": {"min_samples": 10, "min_success_rate": 0.80, "requires_positive_improvement": True}}
