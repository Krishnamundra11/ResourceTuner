import pytest

from resourcetuner.classifier import rules
from resourcetuner.classifier.rules import (
    BACKGROUND, BUILD, CODE_INDEXING, IDLE, TESTING, UNKNOWN,
    ClassifierConfig, classify, classify_process, identity,
)
from resourcetuner.telemetry.containers import ContainersSnapshot, ContainerStats
from resourcetuner.telemetry.host import CPUStats, DiskStats, HostSnapshot, MemoryStats, ProcessInfo


def proc(cmdline, cpu, name=None, ancestors=(), container_id=None, pid=1000, io_wait=0.0):
    return ProcessInfo(
        pid=pid, name=name or cmdline.split()[0].rsplit("/", 1)[-1], cmdline=cmdline, username="root",
        cpu_percent=cpu, rss_bytes=0, num_threads=1, nice=0, ionice_class=None,
        io_read_bytes_per_s=None, io_write_bytes_per_s=None, container_id=container_id,
        ppid=1, ancestors=list(ancestors), io_wait_percent=io_wait,
    )


def snap(*procs):
    return HostSnapshot(
        timestamp=0.0, interval_s=1.0,
        cpu=CPUStats(0, [], 0, 0, 0, 0, 0, 6, None),
        memory=MemoryStats(1, 1, 0, 0, 0, None),
        disk=DiskStats(None, None, None, None, None, None),
        top_processes=list(procs),
    )


def window(*procs, n=5):
    return [snap(*procs) for _ in range(n)]


XDIST_WORKER = "/usr/local/bin/python3.13 -u -c import sys;exec(eval(sys.stdin.readline()))"
PYTEST_MAIN = "/usr/local/bin/python3.13 /usr/local/bin/pytest -n 4 -q /suite"


# --- identity ------------------------------------------------------------------------

@pytest.mark.parametrize("cmdline,expected", [
    ("/usr/libexec/gcc/x86_64-alpine-linux-musl/13.2.1/cc1 -quiet foo.c", "cc1"),
    ("make -j4 -C /src/programs zstd", "make"),
    (PYTEST_MAIN, "pytest"),
    ("python3 -m pytest tests/", "pytest"),
    ("python -u /app/indexer.py", "indexer.py"),
    (XDIST_WORKER, ""),
    ("/bin/sh -c while :; do :; done", ""),
    ("stress-ng --cpu 4 --cpu-load 100", "stress-ng"),
])
def test_identity(cmdline, expected):
    assert identity(cmdline) == expected


def test_kernel_thread_and_ide_are_background():
    assert classify_process(proc("ksoftirqd/7", 50, name="ksoftirqd/7")) == BACKGROUND
    assert classify_process(proc("/snap/code/267/usr/share/code/code --type=renderer", 80)) == BACKGROUND


def test_xdist_worker_inherits_testing_from_parent():
    assert classify_process(proc(XDIST_WORKER, 95, ancestors=[PYTEST_MAIN, "python -u /app/runner.py"])) == TESTING


def test_compiler_helper_inherits_build():
    assert classify_process(proc("sh -c cc -O3 -c x.c", 5, ancestors=["make -j4 zstd"])) == BUILD


def test_background_class_is_not_inherited():
    # A script started from a VS Code terminal is not "BACKGROUND" just because its ancestor is.
    assert classify_process(proc("python3 myscript.py", 90, ancestors=["/snap/code/267/usr/share/code/code"])) is None


def test_stress_ng_is_unattributed():
    assert classify_process(proc("stress-ng-cpu [run]", 100, ancestors=["stress-ng --cpu 4"])) is None


# --- decisions -----------------------------------------------------------------------

def test_build_dominant_with_unattributed_hog_needs_no_fallback():
    r = classify(window(proc("make -j4 zstd", 2), proc("cc1 a.c", 95), proc("cc1 b.c", 90),
                        proc("cc1 c.c", 92), proc("stress-ng-cpu [run]", 300)))
    assert r.workload == BUILD
    assert r.dominance == 1.0 and r.persistence == 1.0 and r.confidence == 1.0
    assert not r.needs_llm_fallback
    assert r.unattributed_activity_percent == 300.0 and r.unattributed_processes == ["stress-ng-cpu"]


def test_build_and_tests_both_significant_is_a_conflict():
    r = classify(window(proc("cc1 a.c", 180), proc(XDIST_WORKER, 190, ancestors=[PYTEST_MAIN])))
    assert r.needs_llm_fallback
    assert any(x.startswith("conflicting_signals") for x in r.fallback_reasons)


def test_indexer_is_background_activity_during_build():
    r = classify(window(proc("cc1 a.c", 350), proc("python -u /app/indexer.py", 160)))
    assert r.workload == BUILD
    assert r.background_activity == [CODE_INDEXING]
    assert not r.needs_llm_fallback  # indexing does not compete with BUILD for dominance


def test_indexing_alone_is_the_workload():
    r = classify(window(proc("python -u /app/indexer.py", 80)))
    assert r.workload == CODE_INDEXING and not r.needs_llm_fallback


def test_busy_with_no_signature_is_unknown_and_falls_back():
    r = classify(window(proc("stress-ng-cpu [run]", 400)))
    assert r.workload == UNKNOWN and r.needs_llm_fallback
    assert r.fallback_reasons[0].startswith("no_signature_match")


def test_ide_noise_alone_is_idle():
    r = classify(window(proc("/snap/code/267/usr/share/code/code", 170), proc("gnome-shell", 40)))
    assert r.workload == IDLE and not r.needs_llm_fallback


def test_intermittent_workload_has_low_persistence():
    busy = snap(proc("cc1 a.c", 200))
    quiet = snap(proc("gnome-shell", 5))
    r = classify([busy, quiet, quiet, quiet, busy])
    assert r.workload == BUILD and r.persistence == 0.4
    assert r.needs_llm_fallback and any("persistence" in x for x in r.fallback_reasons)


def test_window_is_capped_by_config():
    cfg = ClassifierConfig(window_samples=3)
    old = window(proc("stress-ng", 400), n=10)
    new = window(proc("cc1 a.c", 200), n=3)
    r = classify(old + new, config=cfg)
    assert r.window == 3 and r.workload == BUILD


def test_per_container_classification_ignores_ground_truth_label():
    build_id, hog_id = "a" * 64, "b" * 64
    w = window(proc("cc1 a.c", 200, container_id=build_id),
               proc("stress-ng-cpu", 300, container_id=hog_id))

    def ctr(cid, name, cpu, label):
        return ContainerStats(cid, name, "img", "running", label, "cgroup", cpu, 0, 0, 0, 0, 0,
                              0, 0, 1, None, None, None, None)

    # Deliberately WRONG labels: the classifier must not be influenced by them.
    cs = ContainersSnapshot(0.0, True, None, [ctr(build_id, "builder", 200, "testing"),
                                              ctr(hog_id, "hog", 300, "build")])
    r = classify(w, cs)
    assert r.containers == {"builder": BUILD, "hog": UNKNOWN}


def test_classifier_source_never_reads_ground_truth():
    import inspect
    assert "ground_truth" not in inspect.getsource(rules).split('"""', 2)[2]


def test_config_loads_from_yaml(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("classifier:\n  window_samples: 7\n  min_dominance: 0.9\n")
    cfg = ClassifierConfig.load(p)
    assert cfg.window_samples == 7 and cfg.min_dominance == 0.9 and cfg.min_active_cpu_percent == 50.0
    ClassifierConfig.load()  # the repo's config/thresholds.yaml must parse


def test_io_starved_indexer_counts_as_active_via_io_wait():
    # Step 4 finding: under an I/O hog the indexer drops to ~30-40% CPU but spends most of its
    # time blocked on disk. CPU alone would call this IDLE; CPU + I/O wait does not.
    starved = proc("python -u /app/indexer.py", 30, io_wait=45)
    r = classify(window(starved))
    assert r.workload == CODE_INDEXING and not r.needs_llm_fallback
    ev = r.evidence[CODE_INDEXING]
    assert (ev.cpu_percent, ev.io_wait_percent, ev.activity_percent) == (30.0, 45.0, 75.0)
    assert classify(window(proc("python -u /app/indexer.py", 30))).workload == IDLE  # CPU-only view


def test_io_wait_of_unattributed_processes_counts_toward_unknown():
    r = classify(window(proc("stress-ng-hdd [run]", 20, io_wait=80), proc("stress-ng-hdd [run]", 20, io_wait=80)),
                 config=ClassifierConfig(unknown_busy_cpu_percent=150))
    assert r.unattributed_activity_percent == 200.0 and r.workload == UNKNOWN
