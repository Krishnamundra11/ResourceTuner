"""Live end-to-end check of the REAL backend (Step 7): MCP client -> stdio MCP server
(--backend docker) -> Safety Gate with the live classifier -> Resource Controller -> cgroups.

Runs build_vs_indexer (low), then over MCP:
  1. lowers the indexer's CPU weight          -> must be approved and really applied
  2. tries to lower the BUILD's CPU weight      -> must be rejected (foreground)
  3. tries to pause the indexer                 -> must be rejected (one action in flight)
Then rolls the action back through the controller and checks the cgroup is restored.

Usage: ./venv/bin/python scripts/test_controller_live.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import docker

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import run_scenario as rs  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402
from resourcetuner.controller.resource_controller import ActionStore, DockerOps, ResourceController  # noqa: E402

BUILD, IDX = "rt-build_vs_indexer-build", "rt-build_vs_indexer-indexer"


def payload(result) -> dict:
    if result.isError:
        return {"mcp_error": " ".join(c.text for c in result.content if hasattr(c, "text"))[:200]}
    return json.loads(result.content[0].text)


async def mcp_calls(db: str) -> list[dict]:
    params = StdioServerParameters(command=str(REPO / "venv/bin/python"),
                                   args=["-m", "resourcetuner.mcp_server", "--backend", "docker", "--db", db],
                                   cwd=str(REPO))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            await asyncio.sleep(12)   # let the server's live classifier fill its window
            out = [payload(await s.call_tool("list_targets", {}))]
            out.append(payload(await s.call_tool("set_container_cpu_weight", {"target_id": f"container:{IDX}", "weight": 20})))
            out.append(payload(await s.call_tool("set_container_cpu_weight", {"target_id": f"container:{BUILD}", "weight": 50})))
            out.append(payload(await s.call_tool("pause_container", {"target_id": f"container:{IDX}"})))
            return out


def main() -> int:
    client = docker.from_env(timeout=rs.DOCKER_TIMEOUT_S)
    rs.start_scenario(client, rs.load_scenario("build_vs_indexer", "low"), replace=True)
    db = str(Path(tempfile.mkdtemp()) / "actions.db")
    checks = []
    try:
        time.sleep(5)
        targets, r_idx, r_build, r_pause = asyncio.run(mcp_calls(db))
        ops = DockerOps()
        idx_id = client.api.inspect_container(IDX)["Id"]
        build_id = client.api.inspect_container(BUILD)["Id"]
        checks.append(("backend is docker", targets.get("backend") == "docker", targets.get("backend")))
        checks.append(("indexer deprioritised: applied", r_idx.get("status") == "applied" and r_idx.get("gate") == "approved",
                       json.dumps({k: r_idx.get(k) for k in ("status", "old_value", "new_value", "observed")})))
        checks.append(("indexer cgroup really changed", ops.read_knob(idx_id, "cpu.weight") == 20,
                       f"cpu.weight = {ops.read_knob(idx_id, 'cpu.weight')}"))
        checks.append(("build deprioritise: rejected", r_build.get("status") == "rejected"
                       and any(x.startswith("foreground") for x in r_build.get("reasons", [])),
                       "; ".join(r_build.get("reasons", [str(r_build)]))[:150]))
        checks.append(("build cgroup untouched", ops.read_knob(build_id, "cpu.weight") == 100, ""))
        checks.append(("second action while in flight: rejected", r_pause.get("status") == "rejected"
                       and any(x.startswith("in_flight") for x in r_pause.get("reasons", [])),
                       "; ".join(r_pause.get("reasons", [str(r_pause)]))[:150]))
        store = ActionStore(db)
        rb = ResourceController(store, ops).rollback(r_idx["action_id"])
        checks.append(("rollback restored indexer", rb.get("status") == "rolled_back"
                       and ops.read_knob(idx_id, "cpu.weight") == 100, json.dumps(rb)))
        checks.append(("store: nothing left in flight", store.in_flight() == [],
                       f"{len(store.history())} action(s) recorded"))
    finally:
        rs.stop_all(client)
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<40} {detail}")
    return 0 if checks and all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
