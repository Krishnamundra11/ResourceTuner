"""LLM router: provider fallback chain + circuit breaker + structured output + call log.

The routing itself is deterministic code; only the providers it calls are LLMs.

    router = LLMRouter.from_config()
    result = router.complete("diagnostic", messages, response_model=CpuDiagnosis)
    result.parsed, result.provider, result.model, result.degraded

* Chain: try each step of the configured chain in order (config/llm.yaml).
* Circuit breaker: a 429 opens a breaker for that model (Retry-After or rate_limit_cooldown_s);
  a 429 that signals an exhausted daily/free quota opens it for the whole provider tier for
  daily_quota_cooldown_s; network errors / 5xx open it for the provider for error_cooldown_s.
  Open breakers are skipped without a request, and the state is persisted across runs.
* Structured output: each provider lists its modes, tried in order:
    json_schema  -> response_format {"type": "json_schema", strict schema}
    json_object  -> response_format {"type": "json_object"} + schema in the prompt
  A 400 that rejects the response_format downgrades to the next mode. Every answer is
  validated with Pydantic; invalid output moves on to the next step.
* Degraded: only steps whose provider is marked `degraded` (Ollama) set result.degraded.
* Every attempt is appended to data/llm_calls.jsonl (provider, model, outcome, latency, tokens).

`RouterLLM` adapts this to CrewAI's BaseLLM, so CrewAI agents use the chain transparently.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, PrivateAttr, ValidationError

log = logging.getLogger("resourcetuner.llm")

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO / "config" / "llm.yaml"
DEFAULT_CALL_LOG = REPO / "data" / "llm_calls.jsonl"
_DAILY_RE = re.compile(r"per[- ]day|daily|free-models-per|quota", re.I)


def load_dotenv(path: Path = REPO / ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines); never overrides variables already set."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = (x.strip().strip('"').strip("'") for x in line.split("=", 1))
            if k and v:
                os.environ.setdefault(k, v)


# --- transport errors (raised by Transport implementations) ---------------------------------

class RateLimited(Exception):
    def __init__(self, message: str, retry_after_s: float | None = None, daily: bool = False):
        super().__init__(message)
        self.retry_after_s, self.daily = retry_after_s, daily


class NetworkError(Exception):
    pass


class ProviderError(Exception):          # 5xx and other server-side failures
    pass


class BadRequest(Exception):             # 4xx other than 429 (e.g. unsupported response_format)
    pass


class NoApiKey(Exception):
    pass


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str | None
    structured_modes: list[str]
    extra_body: dict = field(default_factory=dict)
    degraded: bool = False
    timeout_s: float | None = None       # overrides request.timeout_s (local models are slow)
    connect_timeout_s: float | None = None  # fail fast if the host is unreachable (breaker then skips it)


class Transport(Protocol):
    def chat(self, provider: ProviderConfig, model: str, messages: list[dict], response_format: dict | None,
             timeout_s: float, temperature: float, max_tokens: int) -> tuple[str, dict]:
        """Return (content, usage). Raise RateLimited / NetworkError / ProviderError / BadRequest."""


class OpenAITransport:
    """All three providers expose OpenAI-compatible chat completions."""

    def __init__(self):
        self._clients: dict[str, Any] = {}

    def _client(self, p: ProviderConfig):
        from openai import OpenAI
        if p.name not in self._clients:
            self._clients[p.name] = OpenAI(base_url=p.base_url, api_key=p.api_key, max_retries=0)
        return self._clients[p.name]

    def chat(self, provider, model, messages, response_format, timeout_s, temperature, max_tokens):
        import httpx
        import openai
        timeout = httpx.Timeout(timeout_s, connect=provider.connect_timeout_s or min(10.0, timeout_s))
        kwargs: dict[str, Any] = dict(model=model, messages=messages, timeout=timeout,
                                      temperature=temperature, max_tokens=max_tokens)
        if response_format:
            kwargs["response_format"] = response_format
        if provider.extra_body:
            kwargs["extra_body"] = provider.extra_body
        try:
            resp = self._client(provider).chat.completions.create(**kwargs)
        except openai.RateLimitError as e:
            headers = getattr(e.response, "headers", {}) or {}
            retry_after = _float(headers.get("retry-after"))
            reset = _float(headers.get("x-ratelimit-reset"))          # OpenRouter: epoch ms
            if retry_after is None and reset:
                retry_after = max(0.0, reset / 1000 - time.time())
            text = f"{e} {getattr(e, 'body', '')}"
            raise RateLimited(text[:300], retry_after, daily=bool(_DAILY_RE.search(text))) from e
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            raise NetworkError(str(e)[:300]) from e
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                raise ProviderError(f"{e.status_code}: {str(e)[:250]}") from e
            raise BadRequest(f"{e.status_code}: {str(e)[:250]}") from e
        choice = resp.choices[0] if resp.choices else None
        content = (choice.message.content or "") if choice else ""
        usage = resp.usage.model_dump() if getattr(resp, "usage", None) else {}
        usage["finish_reason"] = getattr(choice, "finish_reason", None)   # "length" = truncated
        return content, usage


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --- circuit breaker -------------------------------------------------------------------------

class CircuitBreaker:
    """Maps keys ("openrouter", "openrouter:<model>", "openrouter:free") to open-until times."""

    def __init__(self, cfg: dict, state_file: Path | None = None, clock=time.time):
        self.cfg = cfg
        self.state_file = state_file
        self.clock = clock
        self._lock = threading.Lock()
        self._open: dict[str, dict] = {}
        if state_file and state_file.exists():
            try:
                self._open = json.loads(state_file.read_text())
            except (OSError, ValueError):
                self._open = {}

    def blocked(self, keys: list[str]) -> dict | None:
        now = self.clock()
        with self._lock:
            for k in keys:
                s = self._open.get(k)
                if s and s["until"] > now:
                    return {"key": k, **s, "remaining_s": round(s["until"] - now)}
        return None

    def trip(self, key: str, seconds: float, reason: str) -> None:
        seconds = min(max(seconds, 1.0), self.cfg.get("max_cooldown_s", 86400))
        with self._lock:
            self._open[key] = {"until": self.clock() + seconds, "reason": reason}
            self._save()
        log.warning("circuit open for %s (%.0f s): %s", key, seconds, reason)

    def reset(self, key: str) -> None:
        with self._lock:
            if self._open.pop(key, None) is not None:
                self._save()

    def _save(self) -> None:
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self._open))


# --- the router ------------------------------------------------------------------------------

@dataclass
class Attempt:
    provider: str
    model: str
    outcome: str                     # ok | skipped_breaker | no_api_key | rate_limited | daily_quota | network |
    detail: str = ""                 # provider_error | unavailable | auth_error | bad_request | invalid_output
    structured_mode: str | None = None
    latency_ms: int | None = None


@dataclass
class RoutedResult:
    text: str
    parsed: Any
    provider: str
    model: str
    degraded: bool
    structured_mode: str | None
    attempts: list[Attempt]
    usage: dict = field(default_factory=dict)

    def provenance(self) -> dict:
        return {"provider": self.provider, "model": self.model, "degraded": self.degraded,
                "structured_mode": self.structured_mode,
                "attempts": [f"{a.provider}/{a.model}: {a.outcome}" for a in self.attempts]}


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[Attempt]):
        super().__init__("all providers in the chain failed: " +
                         "; ".join(f"{a.provider}/{a.model}={a.outcome}" for a in attempts))
        self.attempts = attempts


def strict_json_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema -> strict JSON schema (every property required, no extra keys)."""
    schema = copy.deepcopy(model.model_json_schema())

    def fix(node):
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("title", None)
            if node.get("type") == "object" or "properties" in node:
                props = node.get("properties", {})
                node["required"] = list(props)
                node["additionalProperties"] = False
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)
    fix(schema)
    return schema


def _extract_json(text: str) -> str:
    """Tolerate ```json fences or prose around a single JSON object."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t)
    if not t.startswith("{"):
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            t = t[start:end + 1]
    return t


class LLMRouter:
    def __init__(self, config: dict, transport: Transport | None = None, call_log: Path | None = DEFAULT_CALL_LOG,
                 clock=time.time):
        self.config = config
        self.transport = transport or OpenAITransport()
        self.call_log = call_log
        cb = config.get("circuit_breaker", {})
        state = cb.get("state_file")
        self.breaker = CircuitBreaker(cb, (REPO / state) if state else None, clock=clock)
        self.clock = clock
        self._downgraded: dict[str, int] = {}   # "provider:model" -> index of structured mode to use

    @classmethod
    def from_config(cls, path: Path = DEFAULT_CONFIG, **kw) -> "LLMRouter":
        load_dotenv()
        return cls(yaml.safe_load(Path(path).read_text()), **kw)

    def provider(self, name: str) -> ProviderConfig:
        p = self.config["providers"][name]
        base = p.get("base_url") or (os.environ.get(p.get("base_url_env", ""), "") or p.get("base_url_default", ""))
        base = base.rstrip("/") + p.get("base_url_suffix", "")
        key = p.get("api_key") or os.environ.get(p.get("api_key_env", ""), "") or None
        return ProviderConfig(name, base, key, list(p.get("structured_modes", [])), p.get("extra_body", {}),
                              bool(p.get("degraded", False)), p.get("timeout_s"),
                              p.get("connect_timeout_s"))

    def complete(self, chain: str, messages: list[dict], response_model: type[BaseModel] | None = None) -> RoutedResult:
        req = self.config.get("request", {})
        attempts: list[Attempt] = []
        for step in self.config["chains"][chain]:
            p = self.provider(step["provider"])
            if step.get("extra_body"):                       # per-model extras, e.g. reasoning effort
                p = dataclasses.replace(p, extra_body={**p.extra_body, **step["extra_body"]})
            model = step["model"]
            keys = [p.name, f"{p.name}:{model}"] + ([f"{p.name}:free"] if model.endswith(":free") else [])
            if (b := self.breaker.blocked(keys)) is not None:
                attempts.append(self._log(chain, Attempt(p.name, model, "skipped_breaker",
                                                         f"{b['key']} open {b['remaining_s']} s: {b['reason'][:80]}")))
                continue
            if not p.api_key:
                attempts.append(self._log(chain, Attempt(p.name, model, "no_api_key")))
                continue
            modes = p.structured_modes if response_model else [None]
            mode_idx = self._downgraded.get(f"{p.name}:{model}", 0) if response_model else 0
            while mode_idx < len(modes):
                mode = modes[mode_idx]
                msgs, rf = self._format(messages, response_model, mode)
                t0 = time.monotonic()
                try:
                    content, usage = self.transport.chat(p, model, msgs, rf, p.timeout_s or req.get("timeout_s", 60),
                                                         req.get("temperature", 0.1), req.get("max_tokens", 1200))
                except RateLimited as e:
                    if e.daily:
                        key = f"{p.name}:free" if model.endswith(":free") else p.name
                        self.breaker.trip(key, self.breaker.cfg.get("daily_quota_cooldown_s", 21600), str(e)[:120])
                        outcome = "daily_quota"
                    else:
                        cool = max(e.retry_after_s or 0, self.breaker.cfg.get("rate_limit_cooldown_s", 60))
                        self.breaker.trip(f"{p.name}:{model}", cool, str(e)[:120])
                        outcome = "rate_limited"
                    attempts.append(self._log(chain, Attempt(p.name, model, outcome, str(e)[:200], mode, _ms(t0))))
                    break
                except (NetworkError, ProviderError) as e:
                    self.breaker.trip(p.name, self.breaker.cfg.get("error_cooldown_s", 30), str(e)[:120])
                    outcome = "network" if isinstance(e, NetworkError) else "provider_error"
                    attempts.append(self._log(chain, Attempt(p.name, model, outcome, str(e)[:200], mode, _ms(t0))))
                    break
                except BadRequest as e:
                    status = _status(e)
                    if mode and re.search(r"response_format|json_schema|schema|structured", str(e), re.I) \
                            and mode_idx + 1 < len(modes):
                        attempts.append(self._log(chain, Attempt(p.name, model, "bad_request", str(e)[:200], mode, _ms(t0))))
                        mode_idx += 1                                   # downgrade, same model
                        self._downgraded[f"{p.name}:{model}"] = mode_idx
                        continue
                    cool = self.breaker.cfg.get("unavailable_cooldown_s", 1800)
                    if status in (401, 403):                            # key problem: whole provider
                        self.breaker.trip(p.name, cool, str(e)[:120])
                        outcome = "auth_error"
                    elif status == 404:                                 # model/endpoint gone upstream
                        self.breaker.trip(f"{p.name}:{model}", cool, str(e)[:120])
                        outcome = "unavailable"
                    else:
                        outcome = "bad_request"
                    attempts.append(self._log(chain, Attempt(p.name, model, outcome, str(e)[:200], mode, _ms(t0))))
                    break
                parsed = None
                if response_model is not None:
                    try:
                        parsed = response_model.model_validate_json(_extract_json(content))
                    except ValidationError as e:
                        errs = e.errors()
                        detail = (f"finish_reason={usage.get('finish_reason')} len={len(content)} "
                                  f"{len(errs)} error(s): {errs[0]['type']} at {errs[0]['loc']}")
                        attempts.append(self._log(chain, Attempt(p.name, model, "invalid_output", detail[:200],
                                                                 mode, _ms(t0)), usage=usage))
                        break
                ok = self._log(chain, Attempt(p.name, model, "ok", "", mode, _ms(t0)), usage=usage,
                               degraded=p.degraded)
                attempts.append(ok)
                return RoutedResult(content, parsed, p.name, model, p.degraded, mode, attempts, usage)
        raise AllProvidersFailed(attempts)

    @staticmethod
    def _format(messages: list[dict], response_model, mode) -> tuple[list[dict], dict | None]:
        if response_model is None or mode is None:
            return messages, None
        schema = strict_json_schema(response_model)
        if mode == "json_schema":
            return messages, {"type": "json_schema", "json_schema": {
                "name": response_model.__name__, "strict": True, "schema": schema}}
        # json_object: the provider only guarantees *some* JSON, so the schema goes in the prompt
        instr = {"role": "system", "content": "Reply with ONE JSON object and nothing else. It must validate "
                                              "against this JSON schema:\n" + json.dumps(schema)}
        return [instr, *messages], {"type": "json_object"}

    def _log(self, chain: str, a: Attempt, usage: dict | None = None, degraded: bool = False) -> Attempt:
        if self.call_log is not None:
            self.call_log.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": round(self.clock(), 3), "chain": chain, **asdict(a), "degraded": degraded,
                   "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "finish_reason")
                             if usage.get(k) is not None} if usage else None}
            with self.call_log.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        return a


def _status(e: Exception) -> int | None:
    m = re.match(r"\s*(\d{3})\b", str(e))
    return int(m.group(1)) if m else None


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


# --- CrewAI adapter ----------------------------------------------------------------------------

try:
    from crewai import BaseLLM
except ImportError:  # pragma: no cover - crewai is optional for non-agent code
    BaseLLM = None


if BaseLLM is not None:
    class RouterLLM(BaseLLM):
        """A CrewAI LLM whose every call goes through LLMRouter's chain."""
        model: str = "resourcetuner-router"
        _router: LLMRouter = PrivateAttr()
        _chain: str = PrivateAttr(default="diagnostic")
        _last: RoutedResult | None = PrivateAttr(default=None)

        def __init__(self, router: LLMRouter, chain: str = "diagnostic", **data):
            super().__init__(model=f"router:{chain}", **data)
            self._router, self._chain = router, chain

        @property
        def last_result(self) -> RoutedResult | None:
            return self._last

        def call(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None,
                 from_agent=None, response_model=None):
            msgs = [{"role": "user", "content": messages}] if isinstance(messages, str) else \
                [{"role": m["role"], "content": m["content"]} for m in messages]
            self._last = self._router.complete(self._chain, msgs, response_model)
            return self._last.parsed if response_model is not None else self._last.text

        def supports_stop_words(self) -> bool:
            return False

        def get_context_window_size(self) -> int:
            return 32768
