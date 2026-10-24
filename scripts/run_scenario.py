"""Synthetic workload generator: build images, start/stop scenarios, show live status.

Usage (from the repo root):
  ./venv/bin/python scripts/run_scenario.py build [IMAGE ...]
  ./venv/bin/python scripts/run_scenario.py list
  ./venv/bin/python scripts/run_scenario.py start SCENARIO [--level L] [--set CTR.KEY=VAL ...]
                                                           [--duration SECONDS] [--replace]
  ./venv/bin/python scripts/run_scenario.py status
  ./venv/bin/python scripts/run_scenario.py stop

Every container gets:
  workload=<type>                 ground truth, for calibration/validation ONLY (never a
                                  classifier input)
  resourcetuner.managed=true      so `stop` only ever touches containers this script created
  resourcetuner.scenario / .level which experiment and intensity it belongs to

Scenario files live in docker/scenarios/*.yaml; see build_vs_indexer.yaml for the format.
"""

from __future__ import annotations

import argparse
import copy
import re
import subprocess
import sys
import time
from pathlib import Path

import docker
import psutil
import yaml
import requests
from docker.errors import APIError, ImageNotFound, NotFound

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from resourcetuner.telemetry.containers import ContainerTelemetry  # noqa: E402

SCENARIO_DIR = REPO / "docker" / "scenarios"
MANAGED = "resourcetuner.managed"
NAME_PREFIX = "rt-"

# image key -> how to build it and its ground-truth workload label
IMAGES = {
    "build-sim":   {"tag": "resourcetuner/build-sim",   "context": "docker/build-sim",   "workload": "build"},
    "indexer-sim": {"tag": "resourcetuner/indexer-sim", "context": "docker/indexer-sim", "workload": "code_indexing"},
    "test-sim":    {"tag": "resourcetuner/test-sim",    "context": "docker/test-sim",    "workload": "testing"},
    "cpu-hog":     {"tag": "resourcetuner/cpu-hog", "context": "docker/stress", "args": {"MODE": "cpu"}, "workload": "cpu_hog"},
    "ram-hog":     {"tag": "resourcetuner/ram-hog", "context": "docker/stress", "args": {"MODE": "ram"}, "workload": "ram_hog"},
    "io-hog":      {"tag": "resourcetuner/io-hog",  "context": "docker/stress", "args": {"MODE": "io"},  "workload": "io_hog"},
    "idle-sim":    {"tag": "resourcetuner/idle-sim",    "context": "docker/idle-sim",    "workload": "idle"},
}

# Docker's default 60s API timeout is too short under full CPU saturation on this VM
# (measured: `docker update` 60s, container removal >60s while cpu_pressure=high runs).
DOCKER_TIMEOUT_S = 180

# Safety: total memory caps of memory hogs may not exceed this share of currently available RAM.
# The VM has no swap, so an uncapped hog could get VS Code / the desktop OOM-killed.
MAX_RAM_HOG_SHARE = 0.6


class ScenarioError(Exception):
    pass


# --- scenario loading ------------------------------------------------------------------

def parse_size(s: str | int) -> int:
    """'512m' / '2g' / '1536M' / bytes -> bytes."""
    if isinstance(s, int):
        return s
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgKMG]?)[bB]?", str(s).strip())
    if not m:
        raise ScenarioError(f"bad size: {s!r}")
    return int(float(m.group(1)) * {"": 1, "k": 2**10, "m": 2**20, "g": 2**30}[m.group(2).lower()])


def merge_spec(base: dict, override: dict) -> dict:
    """Level/--set overrides: env is merged key-by-key, everything else replaces."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if k == "env":
            out.setdefault("env", {}).update(v)
        else:
            out[k] = v
    return out


def load_scenario(name: str, level: str | None = None, sets: list[str] | None = None) -> dict:
    path = SCENARIO_DIR / f"{name}.yaml"
    if not path.exists():
        raise ScenarioError(f"no scenario {name!r} (see `list`)")
    raw = yaml.safe_load(path.read_text())
    levels = raw.get("levels", {})
    level = level or raw.get("default_level")
    if level is not None and level not in levels:
        raise ScenarioError(f"scenario {name!r} has no level {level!r}; levels: {sorted(levels) or 'none'}")

    containers = {}
    for cname, spec in raw["containers"].items():
        if spec["image"] not in IMAGES:
            raise ScenarioError(f"{name}.{cname}: unknown image {spec['image']!r}")
        if level:
            spec = merge_spec(spec, levels[level].get(cname, {}))
        containers[cname] = spec

    for item in sets or []:
        m = re.fullmatch(r"([\w-]+)\.(\w+)=(.*)", item)
        if not m or m.group(1) not in containers:
            raise ScenarioError(f"bad --set {item!r}; expected CONTAINER.KEY=VALUE, containers: {list(containers)}")
        cname, key, value = m.groups()
        override = {key: value} if key in ("mem_limit", "replicas") else {"env": {key: value}}
        containers[cname] = merge_spec(containers[cname], override)

    return {"name": name, "level": level, "description": raw.get("description", "").strip(),
            "containers": containers}


def check_safety(scenario: dict) -> None:
    """Deterministic pre-flight checks; refuse to start rather than endanger the VM."""
    total = 0
    for cname, spec in scenario["containers"].items():
        if spec["image"] != "ram-hog":
            continue
        if "mem_limit" not in spec:
            raise ScenarioError(f"{cname}: ram-hog containers must set mem_limit (VM has no swap)")
        total += parse_size(spec["mem_limit"]) * int(spec.get("replicas", 1))
    available = psutil.virtual_memory().available
    if total > MAX_RAM_HOG_SHARE * available:
        raise ScenarioError(
            f"ram-hog caps total {total / 2**30:.1f}GiB > {MAX_RAM_HOG_SHARE:.0%} of available RAM "
            f"({available / 2**30:.1f}GiB); lower the level or free memory first")


# --- docker operations -----------------------------------------------------------------

def managed_containers(client) -> list[dict]:
    """One API call, no per-container inspect: a container removed mid-listing can't race us."""
    rows = client.api.containers(all=True, filters={"label": f"{MANAGED}=true"})
    return [{"id": r["Id"], "name": r["Names"][0].lstrip("/"), "status": r["State"],
             "labels": r.get("Labels") or {}} for r in rows]


def build_images(names: list[str]) -> None:
    for name in names or IMAGES:
        img = IMAGES.get(name)
        if img is None:
            raise ScenarioError(f"unknown image {name!r}; known: {list(IMAGES)}")
        cmd = ["docker", "build", "-t", img["tag"]]
        for k, v in img.get("args", {}).items():
            cmd += ["--build-arg", f"{k}={v}"]
        cmd.append(str(REPO / img["context"]))
        print(f"==> building {img['tag']}")
        start = time.monotonic()
        subprocess.run(cmd, check=True)
        print(f"    done in {time.monotonic() - start:.0f}s")


def start_scenario(client, scenario: dict, replace: bool = False) -> list:
    existing = managed_containers(client)
    if existing:
        if not replace:
            names = ", ".join(c["name"] for c in existing)
            raise ScenarioError(f"managed containers already exist ({names}); `stop` first or pass --replace")
        stop_all(client)
    check_safety(scenario)
    for spec in scenario["containers"].values():
        try:
            client.images.get(IMAGES[spec["image"]]["tag"])
        except ImageNotFound:
            raise ScenarioError(f"image {IMAGES[spec['image']]['tag']} not built; run `build {spec['image']}`")

    started = []
    for cname, spec in scenario["containers"].items():
        img = IMAGES[spec["image"]]
        replicas = int(spec.get("replicas", 1))
        for i in range(1, replicas + 1):
            name = f"{NAME_PREFIX}{scenario['name']}-{cname}" + (f"-{i}" if replicas > 1 else "")
            kwargs = dict(
                name=name, detach=True,
                environment={k: str(v) for k, v in spec.get("env", {}).items()},
                labels={
                    "workload": img["workload"],
                    MANAGED: "true",
                    "resourcetuner.scenario": scenario["name"],
                    "resourcetuner.level": scenario["level"] or "",
                },
                volumes={vol: {"bind": path, "mode": "rw"} for vol, path in spec.get("volumes", {}).items()},
            )
            if "mem_limit" in spec:
                kwargs["mem_limit"] = spec["mem_limit"]
                kwargs["memswap_limit"] = spec["mem_limit"]  # no swap escape hatch
            started.append(client.containers.run(img["tag"], **kwargs))
    return started


def stop_all(client, attempts: int = 5) -> int:
    """Remove every managed container. Under full CPU saturation the daemon can miss the exit
    event (500 "could not kill container") or exceed the API timeout (both observed with
    cpu_pressure=high), so retry with backoff; raise ScenarioError naming any that won't go."""
    containers = managed_containers(client)
    failed = []
    for c in containers:
        for attempt in range(1, attempts + 1):
            try:
                client.api.remove_container(c["id"], force=True)
                break
            except NotFound:
                break
            except (APIError, requests.exceptions.RequestException) as e:
                if attempt == attempts:
                    failed.append(f"{c['name']}: {getattr(e, 'explanation', None) or e}")
                else:
                    time.sleep(2 * attempt)
    if failed:
        raise ScenarioError("could not remove: " + "; ".join(failed))
    return len(containers)


def print_status(client) -> None:
    containers = managed_containers(client)
    if not containers:
        print("no managed containers")
        return
    # Dogfood Step 1: live metrics come from our own telemetry layer.
    telemetry = ContainerTelemetry(client)
    time.sleep(1.0)
    stats = {s.id: s for s in telemetry.sample().containers}
    print(f"{'NAME':<34} {'STATUS':<9} {'WORKLOAD':<14} {'LEVEL':<7} {'CPU%':>7} {'MEM':>9} {'IO r/w MiB/s':>14}")
    for c in sorted(containers, key=lambda c: c["name"]):
        s = stats.get(c["id"])
        cpu = f"{s.cpu_percent:.1f}" if s and s.cpu_percent is not None else "-"
        mem = f"{s.memory_working_set_bytes / 2**20:.0f}MiB" if s and s.memory_working_set_bytes is not None else "-"
        io = (f"{(s.io_read_bytes_per_s or 0) / 2**20:.1f}/{(s.io_write_bytes_per_s or 0) / 2**20:.1f}"
              if s else "-")
        print(f"{c['name']:<34} {c['status']:<9} {c['labels'].get('workload', ''):<14} "
              f"{c['labels'].get('resourcetuner.level', ''):<7} {cpu:>7} {mem:>9} {io:>14}")


# --- CLI -------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build workload images")
    b.add_argument("images", nargs="*", help=f"subset of {list(IMAGES)} (default: all)")
    sub.add_parser("list", help="list scenarios and their levels")
    s = sub.add_parser("start", help="start a scenario")
    s.add_argument("scenario")
    s.add_argument("--level", help="intensity level defined in the scenario file")
    s.add_argument("--set", action="append", default=[], metavar="CTR.KEY=VAL",
                   help="override an env var (or mem_limit/replicas) for one container")
    s.add_argument("--duration", type=float, help="run for N seconds, then stop and remove")
    s.add_argument("--replace", action="store_true", help="stop running managed containers first")
    sub.add_parser("status", help="show managed containers with live metrics")
    sub.add_parser("stop", help="stop and remove all managed containers")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "build":
            build_images(args.images)
            return 0
        if args.cmd == "list":
            for path in sorted(SCENARIO_DIR.glob("*.yaml")):
                raw = yaml.safe_load(path.read_text())
                levels = ", ".join(raw.get("levels", {})) or "-"
                print(f"{path.stem:<18} levels: {levels:<20} {' '.join(raw.get('description', '').split())}")
            return 0

        client = docker.from_env(timeout=DOCKER_TIMEOUT_S)
        if args.cmd == "stop":
            print(f"removed {stop_all(client)} managed container(s)")
        elif args.cmd == "status":
            print_status(client)
        elif args.cmd == "start":
            scenario = load_scenario(args.scenario, args.level, args.set)
            started = start_scenario(client, scenario, replace=args.replace)
            print(f"started scenario {scenario['name']!r} level={scenario['level']}: "
                  + ", ".join(c.name for c in started))
            if args.duration:
                try:
                    time.sleep(args.duration)
                finally:  # also on Ctrl-C
                    print(f"removed {stop_all(client)} managed container(s)")
        return 0
    except ScenarioError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
