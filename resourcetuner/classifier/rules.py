"""Rule-based workload classifier (deterministic, no LLM).

Inputs: a window of recent ``HostSnapshot``s (and optionally ``ContainersSnapshot``s for
names/per-container CPU). It never reads ``ContainerStats.ground_truth_label``: that label
exists only to *validate* this classifier (scripts/validate_classifier.py).

How it works
------------
1. Each process gets an *identity*: its executable name, or for interpreters (python, node,
   java, sh) the script/module it runs — e.g. ``python -m pytest`` -> ``pytest``.
2. The identity is matched against signature tables (``SIGNATURES``). A process without a
   match of its own inherits a *workload* class from its nearest matching ancestor
   (pytest-xdist workers run as ``python -c ...`` and only their parent says ``pytest``;
   a compiler's ``sh -c`` helpers inherit from ``make``).
3. Activity = CPU + block-I/O wait (100 = one core's worth of time) is summed per class per
   snapshot, then averaged over the window. Counting I/O wait keeps I/O-bound work (an indexer
   starved by a disk hog) from looking idle; it needs kernel.task_delayacct=1, else it is CPU only.
4. Decision:
   * FOREGROUND classes (BUILD, TESTING, DEV_SERVER) win over CODE_INDEXING, which IDEs run
     in the background; indexing is the workload only when no foreground class is active.
   * No active class: IDLE, or UNKNOWN if a lot of CPU is unattributed.
5. "Confidence" is never invented. It is ``min(dominance, persistence)``:
   * dominance   = top class CPU / CPU of all active classes in the same tier
   * persistence = fraction of window snapshots in which the top class was active
6. The LLM fallback flag is purely mechanical (§5): low confidence, conflicting foreground
   signals, or busy-but-no-signature. ``llm_fallback.py`` acts on it; this module never calls an LLM.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from resourcetuner.telemetry.containers import ContainersSnapshot
from resourcetuner.telemetry.host import HostSnapshot, ProcessInfo

BUILD, TESTING, CODE_INDEXING, DEV_SERVER = "BUILD", "TESTING", "CODE_INDEXING", "DEV_SERVER"
IDLE, UNKNOWN = "IDLE", "UNKNOWN"
BACKGROUND = "BACKGROUND"  # recognized non-workload activity: IDE, desktop, kernel, container runtime

FOREGROUND = (BUILD, TESTING, DEV_SERVER)
WORKLOAD_CLASSES = FOREGROUND + (CODE_INDEXING,)

# Identity -> class. Identities are lower-cased executable/script/module names.
SIGNATURES: dict[str, tuple[str, ...]] = {
    BUILD: (
        "make", "gmake", "ninja", "cmake", "bazel", "scons", "meson",
        "cc", "c++", "gcc", "g++", "clang", "clang++", "cc1", "cc1plus", "cc1obj", "collect2",
        "ld", "ld.lld", "ld.gold", "lld", "as", "ar",
        "cargo", "rustc", "javac", "mvn", "gradle", "go", "tsc", "esbuild", "webpack", "rollup",
    ),
    TESTING: (
        "pytest", "py.test", "unittest", "nose2", "tox", "nox",
        "jest", "mocha", "vitest", "karma", "ctest", "phpunit", "rspec",
    ),
    CODE_INDEXING: (
        "clangd", "ctags", "universal-ctags", "cscope", "gtags", "rust-analyzer", "gopls",
        "jdtls", "pyright", "pyright-langserver", "tsserver", "ccls",
        "indexer.py",  # synthetic stand-in for an IDE indexer (docker/indexer-sim)
    ),
    DEV_SERVER: (
        "uvicorn", "gunicorn", "hypercorn", "flask", "vite", "next", "nodemon",
        "webpack-dev-server", "rails", "puma", "php-fpm",
    ),
    BACKGROUND: (
        "code", "gnome-shell", "xorg", "xwayland", "pipewire", "pulseaudio", "wireplumber",
        "dockerd", "containerd", "containerd-shim", "containerd-shim-runc-v2", "runc",
        "systemd", "systemd-journald", "systemd-oomd", "systemd-resolved", "snapd",
        "avahi-daemon", "networkmanager", "gnome-terminal-server", "ibus-daemon", "claude",
    ),
}
# Substrings of the full cmdline (lower-cased) that identify language servers launched via node/java.
CMDLINE_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("vscode-pylance", CODE_INDEXING),
    ("tsserver", CODE_INDEXING),
    ("webpack serve", DEV_SERVER),
    ("manage.py runserver", DEV_SERVER),
    ("next dev", DEV_SERVER),
)
_KERNEL_THREAD_RE = re.compile(r"^(kworker|ksoftirqd|rcu_|migration|kswapd|jbd2|irq/|kthreadd|cpuhp|idle_inject)")
_INTERPRETER_RE = re.compile(r"^(python[\d.]*|pypy[\d.]*|node|nodejs|java|ruby|perl|php|sh|bash|dash|ash|zsh|env)$")
_IDENTITY_INDEX = {name: cls for cls, names in SIGNATURES.items() for name in names}

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "thresholds.yaml"


@dataclass
class ClassifierConfig:
    # The *_cpu_percent thresholds apply to activity (CPU + block-I/O wait, 100 = one core's
    # worth of time); names kept so config/thresholds.yaml stays stable.
    window_samples: int = 5
    min_active_cpu_percent: float = 50.0
    conflict_min_cpu_percent: float = 100.0
    min_dominance: float = 0.75
    min_persistence: float = 0.6
    unknown_busy_cpu_percent: float = 150.0

    @classmethod
    def load(cls, path: str | os.PathLike = DEFAULT_CONFIG_PATH) -> "ClassifierConfig":
        raw = yaml.safe_load(Path(path).read_text()) or {}
        return cls(**raw.get("classifier", {}))


@dataclass
class ClassEvidence:
    activity_percent: float     # window average of CPU + block-I/O wait; 100 = one core's worth of time
    cpu_percent: float          # the CPU part of it
    io_wait_percent: float      # the block-I/O-wait part (0 unless kernel.task_delayacct=1)
    persistence: float          # fraction of snapshots with activity >= min_active
    processes: list[str]        # most active identities seen (max 3), for explanations


@dataclass
class Classification:
    workload: str
    confidence: float | None    # min(dominance, persistence); None for IDLE/UNKNOWN
    dominance: float | None
    persistence: float | None
    needs_llm_fallback: bool
    fallback_reasons: list[str]
    evidence: dict[str, ClassEvidence]
    background_activity: list[str]      # active non-dominant classes, e.g. CODE_INDEXING during BUILD
    unattributed_activity_percent: float  # activity of processes matching no signature (e.g. stress-ng)
    unattributed_processes: list[str]
    containers: dict[str, str] = field(default_factory=dict)  # container name -> workload class
    window: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# --- process identity ----------------------------------------------------------------

def identity(cmdline: str, name: str = "") -> str:
    """Executable name, or the script/module an interpreter runs. '' if not determinable."""
    tokens = cmdline.split()
    if not tokens:
        return name.lower()
    exe = os.path.basename(tokens[0]).lower()
    if not _INTERPRETER_RE.match(exe):
        return exe
    args = tokens[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-m" and i + 1 < len(args):
            return args[i + 1].lower()
        if a == "-c":
            return ""  # inline code: no identity of its own (e.g. pytest-xdist workers)
        if a.startswith("-"):
            i += 1
            continue
        return os.path.basename(a).lower()
    return exe


def classify_process(p: ProcessInfo) -> str | None:
    """Class of one process: own signature first, then a workload class inherited from ancestors."""
    if _KERNEL_THREAD_RE.match(p.name):
        return BACKGROUND
    own = _match(p.cmdline, p.name)
    if own is not None:
        return own
    for anc in p.ancestors:
        cls = _match(anc, "")
        if cls in WORKLOAD_CLASSES:
            return cls
    return None


def _match(cmdline: str, name: str) -> str | None:
    ident = identity(cmdline, name)
    if ident in _IDENTITY_INDEX:
        return _IDENTITY_INDEX[ident]
    if name.lower() in _IDENTITY_INDEX and not _INTERPRETER_RE.match(name.lower()):
        return _IDENTITY_INDEX[name.lower()]
    low = cmdline.lower()
    for needle, cls in CMDLINE_SIGNATURES:
        if needle in low:
            return cls
    return None


# --- classification ----------------------------------------------------------------------

def classify(host_window: list[HostSnapshot], containers: ContainersSnapshot | None = None,
             config: ClassifierConfig | None = None) -> Classification:
    cfg = config or ClassifierConfig()
    window = host_window[-cfg.window_samples:]
    if not window:
        raise ValueError("classify() needs at least one HostSnapshot")
    n = len(window)
    classes = WORKLOAD_CLASSES + (BACKGROUND,)

    per_snap = {c: [0.0] * n for c in classes}     # activity per class per snapshot
    cpu_sum = {c: 0.0 for c in classes}
    io_sum = {c: 0.0 for c in classes}
    unattributed = [0.0] * n
    top_procs: dict[str, dict[str, float]] = {c: {} for c in classes + (UNKNOWN,)}
    ctr_act: dict[str, dict[str, float]] = {}  # container id -> class -> summed activity

    for i, snap in enumerate(window):
        for p in snap.top_processes:
            cls = classify_process(p)
            label = identity(p.cmdline, p.name) or p.name
            # Activity = CPU + time blocked on disk. CPU alone hides I/O-bound work: an indexer
            # starved by an I/O hog drops to ~30-40% CPU and was misread as IDLE (Step 4).
            act = p.cpu_percent + p.io_wait_percent
            if cls is None:
                unattributed[i] += act
                bucket = top_procs[UNKNOWN]
            else:
                per_snap[cls][i] += act
                cpu_sum[cls] += p.cpu_percent
                io_sum[cls] += p.io_wait_percent
                bucket = top_procs[cls]
            bucket[label] = bucket.get(label, 0.0) + act
            if p.container_id:
                c = ctr_act.setdefault(p.container_id, {})
                key = cls or UNKNOWN
                c[key] = c.get(key, 0.0) + act / n

    def top3(bucket: dict[str, float]) -> list[str]:
        return [k for k, v in sorted(bucket.items(), key=lambda kv: -kv[1]) if v > 0][:3]

    evidence = {
        c: ClassEvidence(
            activity_percent=round(sum(v) / n, 1),
            cpu_percent=round(cpu_sum[c] / n, 1),
            io_wait_percent=round(io_sum[c] / n, 1),
            persistence=round(sum(x >= cfg.min_active_cpu_percent for x in v) / n, 2),
            processes=top3(top_procs[c]),
        )
        for c, v in per_snap.items()
    }
    unattributed_avg = round(sum(unattributed) / n, 1)
    active = [c for c in WORKLOAD_CLASSES if evidence[c].activity_percent >= cfg.min_active_cpu_percent]
    fg_active = [c for c in active if c in FOREGROUND]
    tier = fg_active or [c for c in active if c == CODE_INDEXING]

    reasons: list[str] = []
    if not tier:
        if unattributed_avg >= cfg.unknown_busy_cpu_percent:
            workload = UNKNOWN
            reasons.append(f"no_signature_match: {unattributed_avg:.0f}% activity unattributed "
                           f"(>= {cfg.unknown_busy_cpu_percent:.0f}%) and no workload class active")
        else:
            workload = IDLE
        dominance = persistence = confidence = None
        background = []
    else:
        workload = max(tier, key=lambda c: evidence[c].activity_percent)
        tier_total = sum(evidence[c].activity_percent for c in tier)
        dominance = round(evidence[workload].activity_percent / tier_total, 2)
        persistence = evidence[workload].persistence
        confidence = min(dominance, persistence)
        background = [c for c in active if c != workload]
        strong_fg = [c for c in fg_active if evidence[c].activity_percent >= cfg.conflict_min_cpu_percent]
        if len(strong_fg) >= 2:
            reasons.append("conflicting_signals: " + ", ".join(
                f"{c}={evidence[c].activity_percent:.0f}%" for c in strong_fg)
                + f" (each >= {cfg.conflict_min_cpu_percent:.0f}%)")
        if dominance < cfg.min_dominance:
            reasons.append(f"low_confidence: dominance {dominance:.2f} < {cfg.min_dominance}")
        if persistence < cfg.min_persistence:
            reasons.append(f"low_confidence: persistence {persistence:.2f} < {cfg.min_persistence}")

    per_container = {}
    if containers is not None and containers.available:
        names = {c.id: c.name for c in containers.containers}
        for c in containers.containers:
            cls_act = ctr_act.get(c.id, {})
            wl = {k: v for k, v in cls_act.items() if k in WORKLOAD_CLASSES and v >= cfg.min_active_cpu_percent}
            if wl:
                per_container[names[c.id]] = max(wl, key=wl.get)
            elif (c.cpu_percent or 0) >= cfg.min_active_cpu_percent:
                per_container[names[c.id]] = UNKNOWN
            else:
                per_container[names[c.id]] = IDLE

    return Classification(
        workload=workload,
        confidence=confidence,
        dominance=dominance,
        persistence=persistence,
        needs_llm_fallback=bool(reasons),
        fallback_reasons=reasons,
        evidence=evidence,
        background_activity=background,
        unattributed_activity_percent=unattributed_avg,
        unattributed_processes=top3(top_procs[UNKNOWN]),
        containers=per_container,
        window=n,
    )
