import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import run_scenario as rs  # noqa: E402

ALL_SCENARIOS = sorted(p.stem for p in rs.SCENARIO_DIR.glob("*.yaml"))


def _levels(name):
    import yaml
    raw = yaml.safe_load((rs.SCENARIO_DIR / f"{name}.yaml").read_text())
    return list(raw.get("levels", {})) or [None]


@pytest.mark.parametrize("name,level", [(n, lvl) for n in ALL_SCENARIOS for lvl in _levels(n)])
def test_every_scenario_and_level_loads(name, level):
    sc = rs.load_scenario(name, level)
    assert sc["containers"]
    for cname, spec in sc["containers"].items():
        assert spec["image"] in rs.IMAGES
        if spec["image"] == "ram-hog":
            assert "mem_limit" in spec, f"{name}/{level}/{cname}: ram-hog without mem_limit"


def test_level_merges_env_and_replaces_scalars():
    base = {"image": "ram-hog", "mem_limit": "2g", "env": {"VM_BYTES": "1G", "VM_WORKERS": 1}}
    out = rs.merge_spec(base, {"mem_limit": "1g", "env": {"VM_BYTES": "512M"}})
    assert out == {"image": "ram-hog", "mem_limit": "1g", "env": {"VM_BYTES": "512M", "VM_WORKERS": 1}}
    assert base["env"]["VM_BYTES"] == "1G"  # original untouched


def test_levels_actually_change_intensity():
    low = rs.load_scenario("build_vs_cpu", "low")["containers"]["hog"]["env"]["CPU_WORKERS"]
    high = rs.load_scenario("build_vs_cpu", "high")["containers"]["hog"]["env"]["CPU_WORKERS"]
    assert int(low) < int(high)


def test_set_overrides_env_and_mem_limit():
    sc = rs.load_scenario("build_vs_ram", "low", ["build.JOBS=2", "hog.mem_limit=768m"])
    assert sc["containers"]["build"]["env"]["JOBS"] == "2"
    assert sc["containers"]["hog"]["mem_limit"] == "768m"


@pytest.mark.parametrize("bad", ["nope.JOBS=1", "build-JOBS=1", "build.JOBS"])
def test_bad_set_rejected(bad):
    with pytest.raises(rs.ScenarioError):
        rs.load_scenario("build_only", None, [bad])


def test_unknown_scenario_and_level_rejected():
    with pytest.raises(rs.ScenarioError):
        rs.load_scenario("does_not_exist")
    with pytest.raises(rs.ScenarioError):
        rs.load_scenario("build_vs_cpu", "extreme")


def test_parse_size():
    assert rs.parse_size("512m") == 512 * 2**20
    assert rs.parse_size("1536M") == 1536 * 2**20
    assert rs.parse_size("2g") == 2 * 2**30
    assert rs.parse_size(1000) == 1000
    with pytest.raises(rs.ScenarioError):
        rs.parse_size("lots")


def test_safety_rejects_uncapped_ram_hog():
    sc = {"containers": {"hog": {"image": "ram-hog", "env": {}}}}
    with pytest.raises(rs.ScenarioError, match="mem_limit"):
        rs.check_safety(sc)


def test_safety_rejects_caps_beyond_available_ram(monkeypatch):
    class VM:
        available = 4 * 2**30
    monkeypatch.setattr(rs.psutil, "virtual_memory", lambda: VM)
    ok = {"containers": {"hog": {"image": "ram-hog", "mem_limit": "2g"}}}
    rs.check_safety(ok)  # 2g <= 60% of 4g
    too_big = {"containers": {"hog": {"image": "ram-hog", "mem_limit": "1g", "replicas": 3}}}
    with pytest.raises(rs.ScenarioError, match="available RAM"):
        rs.check_safety(too_big)
