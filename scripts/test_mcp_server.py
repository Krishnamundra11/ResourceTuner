"""Live end-to-end check of the MCP tool server over stdio (Step 5).

Starts a managed scenario plus one UNMANAGED "outsider" container (stands in for anything
protected), launches `python -m resourcetuner.mcp_server` as a real MCP client would, and:
  1. lists tools and targets,
  2. proposes a CPU-priority shift on an indexer (dry-run),
  3. tries to name the outsider and a host PID (must be unknown targets),
  4. tries an out-of-range value (must be rejected by the schema),
  5. reads the indexer's cgroup to prove nothing was changed.

Usage: ./venv/bin/python scripts/test_mcp_server.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import docker

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import run_scenario as rs  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402
from resourcetuner.telemetry.containers import find_cgroup_dir  # noqa: E402

OUTSIDER = "outsider-probe"


def payload(result) -> dict:
    if result.isError:
        return {"mcp_error": " ".join(c.text for c in result.content if hasattr(c, "text"))[:160]}
    return json.loads(result.content[0].text)


async def run_checks() -> list[tuple[str, bool, str]]:
    params = StdioServerParameters(command=str(REPO / "venv/bin/python"), args=["-m", "resourcetuner.mcp_server"],
                                   cwd=str(REPO))
    checks = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            checks.append(("tools exposed", tools == sorted(
                ["list_targets", "pause_container", "set_container_cpu_limit", "set_container_cpu_weight"]),
                ", ".join(tools)))

            targets = payload(await s.call_tool("list_targets", {}))["targets"]
            ids = [t["target_id"] for t in targets]
            checks.append(("only managed containers listed", all(i.startswith("container:rt-") for i in ids)
                           and not any(OUTSIDER in i for i in ids), ", ".join(ids)))
            checks.append(("no ground-truth labels exposed", not any("label" in k for t in targets for k in t), ""))

            # Pick the indexer by its container role (name suffix), not by substring: the scenario
            # name "build_vs_indexer" also contains "indexer" (an earlier version of this script
            # picked the BUILD container that way — the check now also verifies the target).
            idx = next(i for i in ids if i.endswith("-indexer") or "-indexer-" in i)
            r = payload(await s.call_tool("set_container_cpu_weight", {"target_id": idx, "weight": 20}))
            checks.append(("dry-run priority shift on indexer", r.get("status") == "dry_run" and r.get("target_id") == idx
                           and r.get("old_value") == 100 and r.get("new_value") == 20,
                           json.dumps({k: r.get(k) for k in ("target_id", "status", "knob", "old_value", "new_value")})))

            r = payload(await s.call_tool("pause_container", {"target_id": f"container:{OUTSIDER}"}))
            checks.append(("outsider (protected) unnameable", r.get("error") == "unknown_target", r.get("message", str(r))))
            r = payload(await s.call_tool("set_container_cpu_limit", {"target_id": "container:1", "cpus": 1}))
            checks.append(("PID-like target unnameable", r.get("error") == "unknown_target", r.get("message", str(r))))
            r = payload(await s.call_tool("set_container_cpu_weight", {"target_id": idx, "weight": 0}))
            checks.append(("out-of-range rejected by schema", "mcp_error" in r, r.get("mcp_error", str(r))[:100]))
    return checks


def main() -> int:
    client = docker.from_env(timeout=rs.DOCKER_TIMEOUT_S)
    rs.start_scenario(client, rs.load_scenario("build_vs_indexer", "low"), replace=True)
    client.containers.run("alpine:3.20", "sleep 300", name=OUTSIDER, detach=True, remove=True)
    try:
        checks = asyncio.run(run_checks())
        weights = {n: (find_cgroup_dir(client.api.inspect_container(n)["Id"]) / "cpu.weight").read_text().strip()
                   for n in ("rt-build_vs_indexer-build", "rt-build_vs_indexer-indexer")}
        checks.append(("cgroups untouched by dry-run", set(weights.values()) == {"100"}, json.dumps(weights)))
    finally:
        rs.stop_all(client)
        subprocess.run(["docker", "rm", "-f", OUTSIDER], capture_output=True)
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<34} {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
