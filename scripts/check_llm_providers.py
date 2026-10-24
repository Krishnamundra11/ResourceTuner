"""Probe every model in an LLM chain once, directly (no fallback), with a tiny structured request.

Answers, per provider/model: is it reachable with our key, does it accept response_format
json_schema (and the OpenRouter extras: require_parameters + response-healing), does the
answer validate, and how long it took. This is how Groq's structured output is verified
before relying on it (decision 2026-10-22). Keys are read from .env and never printed.

Usage: ./venv/bin/python scripts/check_llm_providers.py [--chain diagnostic]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from pydantic import BaseModel, Field

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from resourcetuner.agents import llm_router as R  # noqa: E402


class Probe(BaseModel):
    resource: str = Field(description="always the string 'cpu'")
    busy_cores: float = Field(description="cpu_percent / 100")


MESSAGES = [{"role": "user", "content": "A container uses cpu_percent=327.0 (100 = one core). "
                                        "Return resource='cpu' and busy_cores."}]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="diagnostic")
    args = ap.parse_args(argv)
    base = R.LLMRouter.from_config(call_log=R.DEFAULT_CALL_LOG)
    print(f"{'provider':<11} {'model':<44} {'outcome':<16} {'mode':<12} {'ms':>6}  detail")
    for step in base.config["chains"][args.chain]:
        cfg = dict(base.config)
        cfg["chains"] = {"probe": [step]}
        cfg["circuit_breaker"] = {**base.config.get("circuit_breaker", {}), "state_file": None}  # probe ignores breaker
        r = R.LLMRouter(cfg, call_log=R.DEFAULT_CALL_LOG)
        t0 = time.monotonic()
        try:
            res = r.complete("probe", MESSAGES, Probe)
            a = res.attempts[-1]
            detail = f"busy_cores={res.parsed.busy_cores}" + (f" (after: {', '.join(x.outcome for x in res.attempts[:-1])})"
                                                             if len(res.attempts) > 1 else "")
            print(f"{step['provider']:<11} {step['model']:<44} {'OK':<16} {str(res.structured_mode):<12} "
                  f"{int((time.monotonic() - t0) * 1000):>6}  {detail}")
        except R.AllProvidersFailed as e:
            for a in e.attempts:
                print(f"{a.provider:<11} {a.model:<44} {a.outcome:<16} {str(a.structured_mode):<12} "
                      f"{a.latency_ms or 0:>6}  {a.detail[:110]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
