"""ADK plugin: circuit breaker + model fallback + call log + degraded flag, on ADK's NATIVE flow.

Decision 2026-10-24 (path i): ADK agents keep their own model (Gemini); this plugin, registered
once on the Runner, applies to every ADK agent (Supervisor, Decision, Meta) through ADK's
model hooks, verified to exist in google-adk 2.12:

  before_model_callback   primary's breaker open -> skip it, answer from the fallback chain
  on_model_error_callback primary raised (429/404/5xx/network) -> trip breaker, answer from fallback
  after_model_callback    primary answered -> log provider/model/tokens, record provenance

Returning an LlmResponse from before/on_error is ADK's documented way to replace the model call,
so tools/function calls in the request keep working (fallbacks are ADK LiteLlm models).

Provenance goes to session state["llm_provenance"][agent] and data/llm_calls.jsonl (chain "adk").
If the local model (Ollama) answered, state["llm_degraded"] is set: degraded mode (b) — no action,
and the Meta-Agent must not promote a policy from it (Groq answers are NOT degraded).
The breaker state is shared with the CrewAI router (same file), since the accounts are the same.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import yaml
from google.adk.plugins import BasePlugin

from resourcetuner.agents.llm_router import DEFAULT_CALL_LOG, DEFAULT_CONFIG, REPO, CircuitBreaker, load_dotenv

log = logging.getLogger("resourcetuner.adk")
_DAILY_RE = re.compile(r"per[- ]?day|daily|PerDay|quota", re.I)


def gemini_model_for(config: dict, agent: str) -> str:
    """Native Gemini model name for an ADK agent ('supervisor' | 'decision' | 'meta'), from config."""
    return config["adk_models"][agent]


def without_thought_parts(contents: list) -> list:
    """Drop model "thought" parts from a request history.

    ADK's LiteLlm turns earlier thought parts into `reasoning_content` on assistant messages
    (google/adk/models/lite_llm.py, _content_to_message_param) — needed for Anthropic, but Groq's
    gpt-oss rejects it: "'messages.2': property 'reasoning_content' is unsupported" (measured on
    the second turn, after a tool result). Only the outgoing request is changed; the session
    history keeps everything."""
    out = []
    for c in contents or []:
        parts = [p for p in (c.parts or []) if not getattr(p, "thought", False)]
        if parts:
            out.append(c.model_copy(update={"parts": parts}))
    return out


def strip_thought_parts(callback_context, llm_request):
    """before_model_callback for agents whose OWN model is a LiteLlm non-Gemini model (e.g. Groq)."""
    llm_request.contents = without_thought_parts(llm_request.contents)
    return None


def classify_error(e: Exception) -> tuple[str, bool]:
    """-> (outcome, daily_quota). Works for google-genai and LiteLLM exceptions."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    text = f"{type(e).__name__} {e}"
    if code == 429 or "RateLimit" in type(e).__name__ or "RESOURCE_EXHAUSTED" in text:
        return ("daily_quota", True) if _DAILY_RE.search(text) else ("rate_limited", False)
    if code in (401, 403) or "Authentication" in type(e).__name__:
        return "auth_error", False
    if code == 404 or "NotFound" in type(e).__name__:
        return "unavailable", False
    if (isinstance(code, int) and code >= 500) or any(n in type(e).__name__ for n in
                                                       ("ServerError", "ServiceUnavailable", "InternalServer")):
        return "provider_error", False
    if any(n in text for n in ("Timeout", "Connect", "timed out", "Connection")):
        return "network", False
    return "error", False


class ModelFallbackPlugin(BasePlugin):
    def __init__(self, config: dict | None = None, call_log: Path | None = DEFAULT_CALL_LOG, clock=time.time,
                 name: str = "resourcetuner_model_fallback"):
        super().__init__(name=name)
        load_dotenv()
        self.config = config or yaml.safe_load(Path(DEFAULT_CONFIG).read_text())
        self.chain = self.config["chains"]["adk"]
        cb = self.config.get("circuit_breaker", {})
        state = cb.get("state_file")
        self.breaker = CircuitBreaker(cb, (REPO / state) if state else None, clock=clock)
        self.call_log = call_log
        self.clock = clock
        self._t0: dict[tuple, float] = {}
        self._models: dict[str, Any] = {}

    # --- ADK hooks ---------------------------------------------------------------------------

    async def before_model_callback(self, *, callback_context, llm_request):
        key = (callback_context.invocation_id, callback_context.agent_name)
        self._t0[key] = time.monotonic()
        primary = self.chain[0]
        model = llm_request.model or primary["model"]
        b = self.breaker.blocked([primary["provider"], f"{primary['provider']}:{model}"])
        if b is not None:
            self._log(callback_context, primary["provider"], model, "skipped_breaker",
                      f"{b['key']} open {b['remaining_s']} s")
            return await self._fallback(callback_context, llm_request)
        return None

    async def after_model_callback(self, *, callback_context, llm_response):
        if llm_response.partial or (llm_response.custom_metadata or {}).get("rt_fallback"):
            return None
        primary = self.chain[0]
        model = llm_response.model_version or primary["model"]
        self._log(callback_context, primary["provider"], model, "ok", usage=_usage(llm_response))
        self._provenance(callback_context, primary["provider"], model, degraded=False, fallback=False)
        return None

    async def on_model_error_callback(self, *, callback_context, llm_request, error):
        primary = self.chain[0]
        model = llm_request.model or primary["model"]
        outcome = self._trip(primary["provider"], model, error)
        self._log(callback_context, primary["provider"], model, outcome, str(error)[:200])
        return await self._fallback(callback_context, llm_request)

    # --- fallback chain ----------------------------------------------------------------------

    async def _fallback(self, ctx, llm_request):
        for step in self.chain[1:]:
            provider, model = step["provider"], step["model"]
            b = self.breaker.blocked([provider, f"{provider}:{model}"])
            if b is not None:
                self._log(ctx, provider, model, "skipped_breaker", f"{b['key']} open {b['remaining_s']} s")
                continue
            t0 = time.monotonic()
            # ADK's LiteLlm sends llm_request.model (the Gemini name) if set, so the fallback needs a
            # copy of the request carrying the LiteLLM model id — tools/contents stay as they are.
            req = llm_request.model_copy(update={"model": step["litellm"],
                                                 "contents": without_thought_parts(llm_request.contents)})
            try:
                final = None
                async for resp in self._litellm(step).generate_content_async(req, stream=False):
                    final = resp
                if final is None or final.error_code:
                    raise RuntimeError(f"empty/error response: {getattr(final, 'error_message', None)}")
            except Exception as e:  # noqa: BLE001 - every failure moves down the chain
                outcome = self._trip(provider, model, e)
                self._log(ctx, provider, model, outcome, str(e)[:200], latency_ms=_ms(t0))
                continue
            degraded = bool(self.config["providers"].get(provider, {}).get("degraded", False))
            final.custom_metadata = {**(final.custom_metadata or {}), "rt_fallback": True,
                                     "rt_provider": provider, "rt_model": model, "rt_degraded": degraded}
            self._log(ctx, provider, model, "ok", latency_ms=_ms(t0), usage=_usage(final), degraded=degraded)
            self._provenance(ctx, provider, model, degraded=degraded, fallback=True)
            return final
        log.error("ADK chain exhausted for agent %s", ctx.agent_name)
        return None   # ADK re-raises the primary error: nothing answered, so nothing acts

    def _litellm(self, step: dict):
        from google.adk.models.lite_llm import LiteLlm
        name = step["provider"]
        if name not in self._models:
            p = self.config["providers"][name]
            kw: dict[str, Any] = dict(step.get("extra", {}))
            if p.get("api_key_env"):
                kw["api_key"] = os.environ.get(p["api_key_env"])
            if name == "ollama":
                kw["api_base"] = (os.environ.get(p.get("base_url_env", ""), "") or p.get("base_url_default", "")).rstrip("/")
            if p.get("timeout_s"):
                kw["timeout"] = p["timeout_s"]
            self._models[name] = LiteLlm(model=step["litellm"], **kw)
        return self._models[name]

    def _trip(self, provider: str, model: str, error: Exception) -> str:
        outcome, daily = classify_error(error)
        cb = self.breaker.cfg
        if outcome in ("daily_quota",):
            self.breaker.trip(provider, cb.get("daily_quota_cooldown_s", 21600), str(error)[:120])
        elif outcome == "rate_limited":
            self.breaker.trip(f"{provider}:{model}", cb.get("rate_limit_cooldown_s", 60), str(error)[:120])
        elif outcome == "auth_error":
            self.breaker.trip(provider, cb.get("unavailable_cooldown_s", 1800), str(error)[:120])
        elif outcome == "unavailable":
            self.breaker.trip(f"{provider}:{model}", cb.get("unavailable_cooldown_s", 1800), str(error)[:120])
        elif outcome in ("provider_error", "network"):
            self.breaker.trip(provider, cb.get("error_cooldown_s", 30), str(error)[:120])
        return outcome

    # --- provenance / log ----------------------------------------------------------------------

    def _provenance(self, ctx, provider, model, degraded: bool, fallback: bool) -> None:
        prov = dict(ctx.state.get("llm_provenance") or {})
        prov[ctx.agent_name] = {"provider": provider, "model": model, "degraded": degraded, "fallback": fallback}
        ctx.state["llm_provenance"] = prov
        if degraded:
            ctx.state["llm_degraded"] = True

    def _log(self, ctx, provider, model, outcome, detail="", latency_ms=None, usage=None, degraded=False):
        if latency_ms is None:
            t0 = self._t0.get((ctx.invocation_id, ctx.agent_name))
            latency_ms = _ms(t0) if t0 else None
        if self.call_log is None:
            return
        self.call_log.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": round(self.clock(), 3), "chain": "adk", "agent": ctx.agent_name, "provider": provider,
               "model": model, "outcome": outcome, "detail": detail, "latency_ms": latency_ms,
               "degraded": degraded, "usage": usage}
        with self.call_log.open("a") as f:
            f.write(json.dumps(rec) + "\n")


def _usage(resp) -> dict | None:
    u = getattr(resp, "usage_metadata", None)
    if u is None:
        return None
    return {"prompt_tokens": getattr(u, "prompt_token_count", None),
            "completion_tokens": getattr(u, "candidates_token_count", None)}


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
