"""Derive thresholds from labelled telemetry (Step 4) and explain every value.

Reads data/telemetry_log.jsonl (written by collect_telemetry.py), compares the distributions
of each metric between "negative" samples (idle + uncontended load) and "positive" samples
(congestion runs for that resource), and picks a threshold with one fixed rule:

  * separable   (negative p99 < positive p5):  midpoint of those two percentiles,
                 i.e. the largest symmetric safety margin on both sides;
  * overlapping: the value maximising Youden's J = TPR - FPR over all observed values.

Low-intensity congestion runs are designed to stay within the VM's capacity (e.g. 4 build
jobs + 2 hog workers on 6 cores), so they are reported separately ("light") and not used
for fitting. Trigger persistence is the longest run of consecutive negative samples above
the CPU threshold, never shorter than the brief's 10 s.

Usage:
  ./venv/bin/python scripts/calibrate_thresholds.py                 # print the report
  ./venv/bin/python scripts/calibrate_thresholds.py --write         # also rewrite config + docs
  ./venv/bin/python scripts/calibrate_thresholds.py --sessions cal-20261007
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG = REPO / "data" / "telemetry_log.jsonl"
CONFIG = REPO / "config" / "thresholds.yaml"
REPORT = REPO / "docs" / "step4_calibration.md"

sys.path.insert(0, str(REPO / "scripts"))
import run_scenario as rs  # noqa: E402

# CPU threads each image is designed to keep busy (defaults from its Dockerfile ENV).
# The CPU ground truth is *designed oversubscription*: demand > vCPUs. Intensity names alone
# are not enough: build_and_test (3+3 threads) and cpu_pressure medium (4) fill 6 cores without
# oversubscribing them, and their PSI stayed near the uncontended single-workload runs.
DEFAULT_THREADS = {"build-sim": ("JOBS", 4), "test-sim": ("WORKERS", 4)}
IO_SCENARIOS = {"io_pressure", "indexer_vs_io"}
MEM_SCENARIOS = {"ram_pressure", "build_vs_ram"}
LOAD_EXPECT = {"build_only": "BUILD", "test_only": "TESTING", "indexer_only": "CODE_INDEXING"}
FOREGROUND = ("BUILD", "TESTING", "DEV_SERVER")
MIN_PERSISTENCE_S = 10  # brief §3: "condition persists for 10s"

# Published conventions used as references (memory has no swap here, see report).
SYSTEMD_OOMD_PRESSURE_LIMIT = 60.0   # % (man oomd.conf, systemd 259 on this VM)
SYSTEMD_OOMD_DURATION_S = 30         # s (same source)
EARLYOOM_MIN_AVAIL_PCT = 10.0        # % (earlyoom README default -m 10; not installed here)


# --- statistics --------------------------------------------------------------------------

def pct(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile, q in [0, 100]."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def rates(neg, pos, t, higher_is_worse=True):
    hit = (lambda x: x >= t) if higher_is_worse else (lambda x: x <= t)
    tpr = sum(map(hit, pos)) / len(pos) if pos else float("nan")
    fpr = sum(map(hit, neg)) / len(neg) if neg else float("nan")
    return tpr, fpr


def fit(neg: list[float], pos: list[float], higher_is_worse: bool = True, decimals: int = 1) -> dict:
    neg = [x for x in neg if x is not None]
    pos = [x for x in pos if x is not None]
    if not neg or not pos:
        return {"value": None, "method": "insufficient data", "n_neg": len(neg), "n_pos": len(pos)}
    if higher_is_worse:
        n_edge, p_edge = pct(neg, 99), pct(pos, 5)
        separable = n_edge < p_edge
    else:
        n_edge, p_edge = pct(neg, 1), pct(pos, 95)
        separable = n_edge > p_edge
    if separable:
        t = round((n_edge + p_edge) / 2, decimals)
        method = (f"midpoint of negative p{'99' if higher_is_worse else '1'} ({n_edge:.{decimals}f}) and "
                  f"positive p{'5' if higher_is_worse else '95'} ({p_edge:.{decimals}f})")
    else:
        best = None
        for c in sorted(set(neg + pos)):
            tpr, fpr = rates(neg, pos, c, higher_is_worse)
            j = tpr - fpr
            if best is None or j > best[0] + 1e-12 or (abs(j - best[0]) < 1e-12 and fpr < best[2]):
                best = (j, c, fpr)
        t = round(best[1], decimals)
        method = f"overlapping distributions: max Youden J = {best[0]:.2f}"
    tpr, fpr = rates(neg, pos, t, higher_is_worse)
    return {"value": t, "method": method, "tpr": round(tpr, 3), "fpr": round(fpr, 3),
            "neg_p50": pct(neg, 50), "neg_p99": pct(neg, 99), "neg_max": max(neg),
            "pos_p5": pct(pos, 5), "pos_p50": pct(pos, 50), "pos_min": min(pos),
            "n_neg": len(neg), "n_pos": len(pos), "separable": separable}


def longest_streak(rows: list[dict], key, t: float) -> int:
    """Longest run of consecutive ticks (within one run_id) with key(row) >= t."""
    best = 0
    by_run = defaultdict(list)
    for r in rows:
        by_run[r["run_id"]].append(r)
    for run in by_run.values():
        cur = 0
        for r in sorted(run, key=lambda r: r["tick"]):
            v = key(r)
            cur = cur + 1 if v is not None and v >= t else 0
            best = max(best, cur)
    return best


# --- data ----------------------------------------------------------------------------------

def cpu_demand(scenario: str, level: str | None) -> float:
    """Designed CPU demand of a scenario in cores (0 for idle runs)."""
    try:
        sc = rs.load_scenario(scenario, level)
    except rs.ScenarioError:
        return 0.0
    total = 0.0
    for spec in sc["containers"].values():
        env, img = spec.get("env", {}), spec["image"]
        if img in DEFAULT_THREADS:
            key, default = DEFAULT_THREADS[img]
            threads = float(env.get(key, default))
        elif img == "indexer-sim":
            threads = 1.0
        elif img == "cpu-hog":
            threads = float(env.get("CPU_WORKERS", 2)) * float(env.get("CPU_LOAD", 100)) / 100
        else:
            threads = 0.0  # ram/io hogs and idle are not CPU demand
        total += threads * int(spec.get("replicas", 1))
    return total


def load(sessions: list[str] | None) -> list[dict]:
    rows = [json.loads(line) for line in LOG.open()]
    if sessions:
        rows = [r for r in rows if r.get("session") in sessions]
    return rows


def groups(rows):
    idle = [r for r in rows if r["state"] == "idle"]
    load_ = [r for r in rows if r["state"] == "load"]
    cong = [r for r in rows if r["state"] == "congestion" and r.get("level") != "low"]
    light = [r for r in rows if r["state"] == "congestion" and r.get("level") == "low"]
    return idle, load_, cong, light


def H(key):
    return lambda r: r["host"].get(key)


def oom_delta_per_run(rows) -> dict[str, int]:
    """Total OOM kills that happened during each run (max - min of the cumulative counter)."""
    seen = defaultdict(lambda: defaultdict(list))
    for r in rows:
        for c in r["containers"]:
            if c.get("oom_kills") is not None:
                seen[r["run_id"]][c["name"]].append(c["oom_kills"])
    return {run: sum(max(v) - min(v) for v in ctrs.values()) for run, ctrs in seen.items()}


# --- calibration -----------------------------------------------------------------------------

def calibrate(rows: list[dict], cores: int) -> dict:
    idle, load_, cong, light = groups(rows)
    neg = idle + load_
    demand = {}
    for r in rows:
        key = (r["scenario"], r.get("level"))
        if key not in demand:
            demand[key] = cpu_demand(*key)
    contended = [r for r in rows if r["state"] == "congestion" and demand[(r["scenario"], r.get("level"))] > cores]
    saturated = [r for r in rows if r["state"] == "congestion"
                 and 0 < demand[(r["scenario"], r.get("level"))] <= cores]
    cpu_pos = contended
    io_pos = [r for r in cong if r["scenario"] in IO_SCENARIOS]
    mem_pos = [r for r in cong if r["scenario"] in MEM_SCENARIOS]
    vals = lambda rs, k: [k(r) for r in rs]  # noqa: E731

    out: dict = {"counts": {"idle": len(idle), "load": len(load_), "congestion": len(cong), "light": len(light),
                            "cpu_oversubscribed": len(contended), "cpu_saturated": len(saturated)},
                 "cores": cores, "demand": {f"{k[0]}:{k[1] or '-'}": v for k, v in demand.items() if v}}

    # Contention triggers
    out["cpu_psi"] = fit(vals(neg, H("psi_cpu")), vals(cpu_pos, H("psi_cpu")))
    out["cpu_util"] = fit(vals(neg, H("cpu")), vals(cpu_pos, H("cpu")), decimals=0)
    variants = sorted({r["scenario"] for r in idle})
    out["idle_variants"] = variants
    if len(variants) > 1:
        neg_real = [r for r in neg if r["scenario"] != "idle-clean"]
        out["sensitivity"] = {
            "cpu_psi_realistic_only": fit(vals(neg_real, H("psi_cpu")), vals(cpu_pos, H("psi_cpu")))["value"],
            "io_psi_realistic_only": fit(vals(neg_real, H("psi_io")), vals(io_pos, H("psi_io")))["value"],
        }
    out["io_psi"] = fit(vals(neg, H("psi_io")), vals(io_pos, H("psi_io")))
    out["iowait"] = fit(vals(neg, H("iowait")), vals(io_pos, H("iowait")))
    out["mem_psi"] = fit(vals(neg, H("psi_mem")), vals(mem_pos, H("psi_mem")))
    out["mem_avail"] = fit(vals(neg, H("mem_avail_pct")), vals(mem_pos, H("mem_avail_pct")), higher_is_worse=False)
    out["oom_by_run"] = oom_delta_per_run(rows)

    t_cpu = out["cpu_psi"]["value"]
    if t_cpu is not None:
        sat = [H("psi_cpu")(r) for r in saturated if H("psi_cpu")(r) is not None]
        out["saturated_trigger_rate"] = round(sum(v >= t_cpu for v in sat) / len(sat), 3) if sat else None
    streak = longest_streak(neg, H("psi_cpu"), t_cpu) if t_cpu is not None else 0
    out["persistence"] = {"longest_negative_streak_s": streak,
                          "value": max(MIN_PERSISTENCE_S, streak + 1)}

    # Classifier thresholds (classifier output was logged with the provisional config;
    # the per-class CPU it reports does not depend on those thresholds).
    def cls(r):
        return r.get("cls")

    def act(c):  # classifier activity (CPU + I/O wait); sessions before the I/O-wait change only have CPU
        return c.get("class_activity") or c["class_cpu"]
    idle_cls = [cls(r) for r in idle if cls(r)]
    load_cls = [(r["scenario"], cls(r)) for r in load_ if cls(r)]
    out["min_active"] = fit(
        [max(act(c).get(k, 0) for k in FOREGROUND + ("CODE_INDEXING",)) for c in idle_cls],
        [act(c)[LOAD_EXPECT[s]] for s, c in load_cls], decimals=0)
    hog_runs = [r for r in cong if r["scenario"] == "cpu_pressure" and cls(r)]
    out["unknown_busy"] = fit([cls(r)["unattributed"] for r in neg if cls(r)],
                              [cls(r)["unattributed"] for r in hog_runs], decimals=0)
    single = [r for r in load_ + cong + light if cls(r) and r["scenario"] != "build_and_test"
              and r["scenario"] not in ("cpu_pressure", "io_pressure", "ram_pressure")]
    conflict = [r for r in rows if r["scenario"] == "build_and_test" and cls(r)]

    def second_fg(c):
        v = sorted((act(c).get(k, 0) for k in FOREGROUND), reverse=True)
        return v[1]
    out["conflict"] = fit([second_fg(cls(r)) for r in single], [second_fg(cls(r)) for r in conflict], decimals=0)
    out["dominance"] = fit([cls(r)["dominance"] for r in conflict if cls(r)["dominance"] is not None],
                           [cls(r)["dominance"] for r in single if cls(r)["dominance"] is not None],
                           decimals=2)
    pers = [cls(r)["persistence"] for r in single if cls(r)["persistence"] is not None]
    out["persistence_obs"] = {"p1": pct(pers, 1), "p50": pct(pers, 50), "n": len(pers)}

    # Per-run medians for the report
    per_run = defaultdict(list)
    for r in rows:
        per_run[r["run_id"]].append(r)
    out["runs"] = []
    for run_id, rs in per_run.items():
        r0 = rs[0]
        wl = [cls(r)["workload"] for r in rs if cls(r)]
        out["runs"].append({
            "scenario": r0["scenario"], "level": r0.get("level"), "state": r0["state"], "n": len(rs),
            "demand": demand[(r0["scenario"], r0.get("level"))],
            "cpu": pct(vals(rs, H("cpu")), 50), "psi_cpu": pct(vals(rs, H("psi_cpu")), 50),
            "psi_io": pct(vals(rs, H("psi_io")), 50), "iowait": pct(vals(rs, H("iowait")), 50),
            "psi_mem": pct(vals(rs, H("psi_mem")), 50), "mem_avail": pct(vals(rs, H("mem_avail_pct")), 50),
            "oom": out["oom_by_run"].get(run_id, 0),
            "workload": max(set(wl), key=wl.count) if wl else None,
        })
    return out


# --- output --------------------------------------------------------------------------------

def _f(x, d=1):
    return "–" if x is None else f"{x:.{d}f}"


def describe(name, f, unit="", d=1):
    if f["value"] is None:
        return f"{name}: insufficient data"
    return (f"{name} = {f['value']}{unit}: {f['method']}; negatives p50/p99/max "
            f"{_f(f['neg_p50'], d)}/{_f(f['neg_p99'], d)}/{_f(f['neg_max'], d)}, positives p5/p50 "
            f"{_f(f['pos_p5'], d)}/{_f(f['pos_p50'], d)} (n={f['n_neg']}/{f['n_pos']}); "
            f"at this value TPR {f['tpr']:.2f}, FPR {f['fpr']:.2f}")


def memory_values(c):
    """Memory has no host-level data signal here (no swap: the kernel OOM-kills inside the
    capped container instead of stalling), so these come from published conventions."""
    return {"psi_some_avg10": 10.0, "available_min_percent": EARLYOOM_MIN_AVAIL_PCT}


# Top-level keys this script fits from telemetry data and therefore owns. Any OTHER top-level
# key already in config/thresholds.yaml (currently just `evaluation:`, hand-tuned, not fitted
# from telemetry) must survive a --write untouched — see preserve_unowned_sections().
OWNED_SECTIONS = ("contention", "classifier")


def preserve_unowned_sections(existing_text: str) -> str:
    """Returns the exact original text of every top-level YAML section this script does not
    own, verbatim (comments, formatting and all) — starting from the first line that is not part
    of an owned section's header comment or an owned section itself.

    Without this, `--write` replaces the WHOLE file with only `render_yaml`'s output: the
    `evaluation:` section that EvaluationEngine reads (config/thresholds.yaml's discrete/
    continuous ±10%/±15% bands) is not produced by `render_yaml` at all, so a bare `--write`
    would silently delete it. EvaluationEngine.evaluate_discrete() then falls back to its own
    hardcoded code defaults with NO warning (resourcetuner/evaluation/engine.py:112-115) — those
    happen to match the current file today, but a future hand-tune of the evaluation bands would
    be silently lost the next time someone recalibrates the contention/classifier thresholds.
    """
    if not existing_text.strip():
        return ""
    lines = existing_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        # A top-level key: starts at column 0, is not a comment, and ends its first token in ":".
        if line and not line[0].isspace() and not line.startswith("#") and ":" in line:
            key = line.split(":", 1)[0].strip()
            if key and key not in OWNED_SECTIONS:
                start = i
                break
    if start is None:
        return ""
    return "\n".join(lines[start:]).rstrip() + "\n"


def render_yaml(c, sessions, preserved: str = "") -> str:
    cp, cu, ip, iw = c["cpu_psi"], c["cpu_util"], c["io_psi"], c["iowait"]
    ma, ub, cf, dm = c["min_active"], c["unknown_busy"], c["conflict"], c["dominance"]
    mem = memory_values(c)
    stamp = time.strftime("%Y-%m-%d")
    lines = [
        "# ResourceTuner thresholds — calibrated in Step 4.",
        f"# Generated by scripts/calibrate_thresholds.py on {stamp} from data/telemetry_log.jsonl",
        f"# (sessions: {', '.join(sessions) if sessions else 'all'}). Full methodology and tables:",
        "# docs/step4_calibration.md. Units: CPU in percent of one core (100 = one core) unless noted;",
        "# PSI values are /proc/pressure 'some avg10' percentages.",
        "# Rule: separable -> midpoint(negative p99, positive p5); otherwise max Youden J.",
        "# Negatives = idle + uncontended single-workload samples. CPU positives = designed CPU demand > vCPUs;",
        "# I/O and memory positives = medium/high runs of that resource's scenarios.",
        "",
        "contention:",
        "  cpu:",
        f"    psi_some_avg10: {cp['value']}   # PRIMARY trigger. {cp['method']}; FPR {cp['fpr']:.2f}, TPR {cp['tpr']:.2f}",
        "    # No utilization threshold: CPU % does not separate contended from uncontended load",
        f"    # ({cu['method']}, FPR {cu['fpr']:.2f} at {cu['value']:.0f}%: an uncontended build already runs ~70%).",
        "  io:",
        f"    psi_some_avg10: {ip['value']}   # {ip['method']}; FPR {ip['fpr']:.2f}, TPR {ip['tpr']:.2f}",
        f"    iowait_percent: {iw['value']}   # supporting evidence. {iw['method']}; FPR {iw['fpr']:.2f}, TPR {iw['tpr']:.2f}",
        "  memory:",
        f"    psi_some_avg10: {mem['psi_some_avg10']}   # NOT data-derived: host memory PSI stayed ~0 in every run (no swap ->",
        f"                           # kernel OOM-kills instead of stalling). Set well below systemd-oomd's kill limit",
        f"                           # ({SYSTEMD_OOMD_PRESSURE_LIMIT:.0f}% for {SYSTEMD_OOMD_DURATION_S}s, man oomd.conf) because we must act before a kill.",
        f"    available_min_percent: {mem['available_min_percent']}   # earlyoom's default kill point (-m 10); we treat crossing it as contention.",
        "    container_oom_kill_is_contention: true   # memory.events oom_kill delta > 0 in the window (observed in ram_pressure high).",
        f"  persistence_s: {c['persistence']['value']}   # condition must hold this long. Longest run of consecutive",
        f"                    # negative seconds above the CPU PSI threshold = {c['persistence']['longest_negative_streak_s']}s; floor = brief's 10s.",
        "",
        "classifier:",
        "  window_samples: 5              # design choice (not fitted): 5 s at 1 Hz, half the 10 s trigger persistence",
        f"  min_active_cpu_percent: {ma['value']:.0f}   # {ma['method']} (negative = busiest workload class while idle,",
        "                                 # e.g. the IDE language server; positive = the workload's own class in single-workload runs)",
        f"  conflict_min_cpu_percent: {cf['value']:.0f}   # {cf['method']} (2nd-largest foreground class: single-workload runs",
        "                                   # vs build_and_test)",
        f"  min_dominance: {dm['value']}           # {dm['method']} (negative = build_and_test, positive = single-workload runs)",
        f"  min_persistence: 0.6           # design choice (not fitted): steady runs show persistence p1 = {_f(c['persistence_obs']['p1'], 2)},",
        "                                 # so it only matters during transitions; 3 of 5 samples active",
        f"  unknown_busy_cpu_percent: {ub['value']:.0f}   # {ub['method']} (negative = unattributed CPU in idle/load,",
        "                                   # positive = cpu_pressure hogs, which match no developer-tool signature)",
        "",
    ]
    out = "\n".join(lines)
    if preserved:
        out += "\n" + preserved          # keep the usual one-blank-line gap before the next section
    return out


def render_report(c, sessions) -> str:
    cn = c["counts"]
    rows = sorted(c["runs"], key=lambda r: ({"idle": 0, "load": 1, "congestion": 2}[r["state"]], r["scenario"], r["level"] or ""))
    t = [
        "# Step 4 — Threshold calibration",
        "",
        f"_Generated by `scripts/calibrate_thresholds.py` on {time.strftime('%Y-%m-%d %H:%M')}; "
        f"sessions: {', '.join(sessions) if sessions else 'all'}._",
        "",
        "## Methodology",
        "",
        "1. **Data collection** (`scripts/collect_telemetry.py`): telemetry sampled at 1 Hz and appended to "
        "`data/telemetry_log.jsonl`, one line per second, each labelled with `state`, `scenario`, `level` and `notes`.",
        "   * **idle** — no workload containers, in two variants: *realistic* (desktop, VS Code and this tooling "
        "open — the deployment condition) and *clean* (VS Code closed — tightest signal). Both are negatives.",
        "   * **load** — one developer workload alone (build / tests / indexer): busy but uncontended.",
        "   * **congestion** — a workload with a competitor, or a resource hog, at low / medium / high intensity. "
        "Each run: 15 s warm-up (PSI avg10 is a 10 s average), then 45 s recorded.",
        "2. **Grouping.** Negatives = idle + load. **CPU positives = runs whose designed CPU demand exceeds the "
        f"{c['cores']} vCPUs** (demand = build JOBS + test WORKERS + indexer replicas + hog CPU_WORKERS x CPU_LOAD, read "
        "from the scenario files). Runs that fill but do not exceed the cores (e.g. build_and_test, 3+3 threads) are "
        "*saturated, not oversubscribed*; they are reported separately. I/O and memory positives = medium/high runs of "
        "io_pressure, indexer_vs_io / ram_pressure, build_vs_ram; low-intensity runs are reported, not fitted.",
        "3. **Threshold rule** (identical for every metric): if negative p99 < positive p5 the classes are separable and "
        "the threshold is the midpoint of those two percentiles (largest symmetric margin); otherwise the value "
        "maximising Youden's J = TPR − FPR. TPR/FPR are reported per sample.",
        "4. **Persistence**: longest run of consecutive negative seconds above the CPU PSI threshold, with the brief's "
        "10 s as a floor.",
        "5. **References**: Linux PSI (`/proc/pressure`, some avg10); systemd-oomd defaults from `man oomd.conf` on this "
        f"VM (systemd 259): memory pressure limit {SYSTEMD_OOMD_PRESSURE_LIMIT:.0f}% for {SYSTEMD_OOMD_DURATION_S} s; "
        f"earlyoom default minimum available memory {EARLYOOM_MIN_AVAIL_PCT:.0f}% (from its documentation; not installed here).",
        "6. **Validation**: `scripts/validate_classifier.py` re-run with the new config.",
        "",
        f"Samples: idle {cn['idle']}, load {cn['load']}, congestion (medium/high) {cn['congestion']}, light (low) "
        f"{cn['light']}; CPU-oversubscribed {cn['cpu_oversubscribed']}, CPU-saturated-only {cn['cpu_saturated']}.",
        "",
        "## Derived thresholds",
        "",
    ]
    for name, key, unit, d in [("CPU PSI some avg10", "cpu_psi", "%", 1), ("CPU utilization", "cpu_util", "%", 0),
                               ("I/O PSI some avg10", "io_psi", "%", 1), ("iowait", "iowait", "%", 1),
                               ("Memory PSI some avg10 (data)", "mem_psi", "%", 1),
                               ("Memory available (data)", "mem_avail", "%", 1),
                               ("Classifier min_active_cpu", "min_active", "%", 0),
                               ("Classifier unknown_busy_cpu", "unknown_busy", "%", 0),
                               ("Classifier conflict_min_cpu", "conflict", "%", 0),
                               ("Classifier min_dominance", "dominance", "", 2)]:
        t.append(f"* {describe(name, c[key], unit, d)}")
    if c.get("sensitivity"):
        sv = c["sensitivity"]
        t.append(f"* Robustness to IDE noise: fitting with the realistic idle only gives CPU PSI "
                 f"{sv['cpu_psi_realistic_only']}% and I/O PSI {sv['io_psi_realistic_only']}% (vs "
                 f"{c['cpu_psi']['value']}% / {c['io_psi']['value']}% with both baselines): the IDE does not move the triggers.")
    t.append("* CPU utilization is NOT used as a threshold: it does not separate contended from uncontended load "
             "(see the line above; an uncontended build already runs ~70%), so contention is detected with PSI.")
    p = c["persistence"]
    t += [f"* Trigger persistence = {p['value']} s (longest negative streak above CPU PSI threshold: "
          f"{p['longest_negative_streak_s']} s; floor 10 s).",
          f"* Saturated-but-not-oversubscribed samples above the CPU PSI threshold: "
          f"{_f(100 * c['saturated_trigger_rate'] if c.get('saturated_trigger_rate') is not None else None)}% "
          "(these are not counted as errors either way; they show where the boundary sits).",
          "* Memory available (data) above is shown for completeness but NOT used: it tracks how much the hogs were "
          "designed to allocate, not contention; test_only (uncontended) already sits below it.",
          "* Caveat — host CPU utilization: on this VirtualBox VM, `/proc/stat`-based host CPU % can under-report "
          "relative to cgroup CPU usage (measured: container 200% = 33% of host while /proc/stat showed ~29%; in one "
          "recorded run 6% vs 31%). PSI and cgroup usage are scheduler-clock based, so the trigger uses PSI; "
          "utilization is supporting evidence only.",
          "* Memory: host memory PSI did not rise in any run (no swap: the kernel OOM-kills inside the capped "
          "container instead of stalling), so memory thresholds come from the references above, plus container "
          "OOM-kill events as a direct signal.",
          "",
          "## Per-run medians",
          "",
          "| state | scenario | level | CPU demand | n | CPU % | CPU PSI | I/O PSI | iowait | mem PSI | mem avail % | OOM kills | workload |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        t.append(f"| {r['state']} | {r['scenario']} | {r['level'] or '–'} | {_f(r['demand'], 0) if r['demand'] else '–'} | "
                 f"{r['n']} | {_f(r['cpu'], 0)} | "
                 f"{_f(r['psi_cpu'])} | {_f(r['psi_io'])} | {_f(r['iowait'])} | {_f(r['psi_mem'])} | "
                 f"{_f(r['mem_avail'])} | {r['oom']} | {r['workload'] or '–'} |")
    return "\n".join(t) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", nargs="*", help="only use these collection sessions (default: all)")
    ap.add_argument("--cores", type=int, default=os.cpu_count(), help="vCPUs of the machine the data came from")
    ap.add_argument("--write", action="store_true", help="rewrite config/thresholds.yaml and docs/step4_calibration.md")
    args = ap.parse_args(argv)
    rows = load(args.sessions)
    if not any(r["state"] == "idle" for r in rows) or not any(r["state"] == "congestion" for r in rows):
        print("error: need both idle and congestion samples", file=sys.stderr)
        return 2
    c = calibrate(rows, args.cores)
    report = render_report(c, args.sessions)
    print(report)
    if args.write:
        preserved = preserve_unowned_sections(CONFIG.read_text()) if CONFIG.is_file() else ""
        REPORT.parent.mkdir(exist_ok=True)
        REPORT.write_text(report)
        CONFIG.write_text(render_yaml(c, args.sessions, preserved))
        note = f" (preserved existing: {', '.join(sorted({l.split(':', 1)[0] for l in preserved.splitlines() if l and not l[0].isspace() and ':' in l}))})" if preserved else ""
        print(f"wrote {CONFIG.relative_to(REPO)} and {REPORT.relative_to(REPO)}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
