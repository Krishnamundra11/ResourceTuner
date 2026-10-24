"""Run one CrewAI diagnostic agent (cpu / ram / io) on hand-written evidence, for real.

Uses the configured chain (config/llm.yaml: Groq -> OpenRouter free -> Ollama on the Windows host) and
prints the diagnosis, the deterministic check result, and which provider/model answered.
No ADK, no MCP, nothing is changed on the system.

Usage: ./venv/bin/python scripts/test_diagnostic_crew.py [--case build_vs_indexer|no_contention|ram_pressure|ram_ok|io_contention|io_quiet]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from resourcetuner.agents.diagnostic_crew import sample_evidence  # noqa: E402
from resourcetuner.agents.diagnostic_crew.crew import run_diagnostic_crew  # noqa: E402

CASES = {  # case -> (agent, evidence)
    "build_vs_indexer": ("cpu", sample_evidence.BUILD_VS_INDEXER), "no_contention": ("cpu", sample_evidence.NO_CONTENTION),
    "ram_pressure": ("ram", sample_evidence.RAM_PRESSURE), "ram_ok": ("ram", sample_evidence.RAM_OK),
    "io_contention": ("io", sample_evidence.IO_CONTENTION), "io_quiet": ("io", sample_evidence.IO_QUIET),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=sorted(CASES), default="build_vs_indexer")
    args = ap.parse_args(argv)
    agent, evidence = CASES[args.case]
    f = run_diagnostic_crew(evidence, agents=(agent,))[0]
    print(json.dumps(f.to_dict(), indent=2))
    if f.error:
        print(f"\nFAILED: {f.error}", file=sys.stderr)
        return 1
    p = f.provenance
    print(f"\nanswered by {p['provider']}/{p['model']} (structured={p['structured_mode']}, degraded={p['degraded']})")
    print("deterministic check:", "OK" if f.check_ok else f"ISSUES {f.check_issues}")
    print("actionable:", f.actionable, "" if f.actionable else f"({f.to_dict()['not_actionable_reason']})")
    return 0 if f.check_ok else 2


if __name__ == "__main__":
    sys.exit(main())
