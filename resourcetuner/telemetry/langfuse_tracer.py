"""Langfuse LLM Observability & Tracing Integration for ResourceTuner.

Follows official Langfuse multi-agent instrumentation best practices:
1. Agent Graph Visualization: Types subagents with as_type='agent' (Supervisor, Diagnostic Crew, Decision, Meta).
2. Tool Tracking: Types deterministic safety gate and cgroup operations with as_type='tool'.
3. LLM Generation Spans: Captures model version, prompt/completion token usage, latency, and provider provenance.
4. Non-Blocking & Zero-Crash: Gracefully no-ops when keys are absent or network is offline, ensuring kernel
   cgroup tuning loops never fail due to telemetry.
5. End-of-cycle flush: Automatically flushes buffered traces to avoid lost observations.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("resourcetuner.telemetry.langfuse")

_CLIENT_LOCK = threading.Lock()
_CLIENT_INITIALIZED = False
_LANGFUSE_CLIENT: Any = None

# Graceful import of observe from langfuse
try:
    from langfuse import observe
except ImportError:
    def observe(*d_args, **d_kwargs):  # type: ignore
        def decorator(func: Callable) -> Callable:
            return func
        if len(d_args) == 1 and callable(d_args[0]) and not d_kwargs:
            return d_args[0]
        return decorator


def is_langfuse_configured() -> bool:
    """Return True if Langfuse environment variables are populated."""
    try:
        from resourcetuner.agents.llm_router import load_dotenv
        load_dotenv()
    except Exception:
        pass
    if "LANGFUSE_BASE_URL" in os.environ and "LANGFUSE_HOST" not in os.environ:
        os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASE_URL"]
    if os.environ.get("LANGFUSE_PUBLIC_KEY"):
        os.environ.pop("OTEL_SDK_DISABLED", None)
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def get_langfuse_client() -> Optional[Any]:
    """Return singleton Langfuse client if configured, else None."""
    global _CLIENT_INITIALIZED, _LANGFUSE_CLIENT
    if not _CLIENT_INITIALIZED:
        with _CLIENT_LOCK:
            if not _CLIENT_INITIALIZED:
                if is_langfuse_configured():
                    try:
                        from langfuse import Langfuse
                        _LANGFUSE_CLIENT = Langfuse()
                        log.info(
                            "Langfuse client initialized successfully (host=%s)",
                            os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
                        )
                    except Exception as e:
                        log.warning("Langfuse client init failed: %s; falling back to local mode", e)
                        _LANGFUSE_CLIENT = None
                else:
                    _LANGFUSE_CLIENT = None
                _CLIENT_INITIALIZED = True
    return _LANGFUSE_CLIENT


def observe_agent(name: str):
    """Decorator to mark an agent execution node in Langfuse Agent Graph."""
    return observe(name=name, as_type="agent")


def observe_tool(name: str):
    """Decorator to mark a deterministic tool or safety check node."""
    return observe(name=name, as_type="tool")


def observe_cycle(name: str = "tuning_cycle"):
    """Decorator to mark an end-to-end multi-agent tuning cycle."""
    return observe(name=name, as_type="chain")


def record_llm_generation(
    *,
    name: str,
    provider: str,
    model: str,
    prompt: Optional[Any] = None,
    output: Optional[Any] = None,
    usage: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    outcome: str = "ok",
    degraded: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Log an LLM generation call to Langfuse if client is available."""
    client = get_langfuse_client()
    if client is None:
        return

    try:
        usage_details = None
        if usage:
            usage_details = {
                "input": int(usage.get("prompt_tokens") or 0),
                "output": int(usage.get("completion_tokens") or 0),
                "total": int(usage.get("total_tokens") or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))),
            }

        meta = {
            "provider": provider,
            "outcome": outcome,
            "degraded": degraded,
            "latency_ms": latency_ms,
            **(metadata or {}),
        }

        # Create generation observation
        gen = client.start_observation(
            name=name,
            as_type="generation",
            model=model,
            input=prompt,
            output=output,
            usage_details=usage_details,
            metadata=meta,
            level="ERROR" if outcome not in ("ok", "skipped_breaker") else "DEFAULT",
            status_message=f"{outcome}: {meta.get('detail', '')}" if outcome != "ok" else None,
        )
        gen.end()
    except Exception as e:
        log.debug("Langfuse record_llm_generation suppressed exception: %s", e)


def flush_traces() -> None:
    """Flush pending observations to Langfuse backend."""
    client = get_langfuse_client()
    if client is not None:
        try:
            client.flush()
        except Exception as e:
            log.debug("Langfuse flush suppressed exception: %s", e)
