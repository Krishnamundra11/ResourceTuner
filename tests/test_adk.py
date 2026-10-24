"""Step 8b, offline: the ADK ModelFallbackPlugin (breaker, fallback, provenance, degraded flag,
thought stripping) and the Supervisor's run_diagnostics tool. No network, no real models."""

import asyncio
import json

import pytest
from google.adk.models import LlmRequest, LlmResponse
from google.genai import types

from resourcetuner.agents.adk import model_plugin as MP
from resourcetuner.agents.adk.supervisor import make_run_diagnostics, mock_diagnose


def cfg(tmp_path):
    return {
        "providers": {"gemini": {"api_key_env": "X"}, "groq": {"api_key_env": "X"},
                      "ollama": {"base_url_default": "http://h:11434", "degraded": True}},
        "chains": {"adk": [{"provider": "gemini", "model": "gemini-x"},
                           {"provider": "groq", "model": "gpt-oss", "litellm": "groq/gpt-oss"},
                           {"provider": "ollama", "model": "qwen", "litellm": "ollama_chat/qwen"}]},
        "circuit_breaker": {"rate_limit_cooldown_s": 60, "daily_quota_cooldown_s": 21600, "error_cooldown_s": 30,
                            "unavailable_cooldown_s": 1800, "max_cooldown_s": 86400,
                            "state_file": str(tmp_path / "breaker.json")},
    }


class Ctx:
    def __init__(self):
        self.invocation_id, self.agent_name, self.state = "inv1", "supervisor", {}


class FakeModel:
    def __init__(self, result):
        self.result, self.requests = result, []

    async def generate_content_async(self, req, stream=False):
        self.requests.append(req)
        if isinstance(self.result, Exception):
            raise self.result
        yield self.result


class Err(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def text_response(t):
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=t)]))


def request():
    return LlmRequest(model="gemini-x", contents=[
        types.Content(role="user", parts=[types.Part(text="hi")]),
        types.Content(role="model", parts=[types.Part(text="thinking...", thought=True),
                                           types.Part(function_call=types.FunctionCall(name="run_diagnostics", args={}))]),
        types.Content(role="model", parts=[types.Part(text="only a thought", thought=True)]),
    ])


def plugin(tmp_path, groq=None, ollama=None):
    p = MP.ModelFallbackPlugin(cfg(tmp_path), call_log=tmp_path / "calls.jsonl")
    if groq is not None:
        p._models["groq"] = groq
    if ollama is not None:
        p._models["ollama"] = ollama
    return p


def rows(tmp_path):
    return [json.loads(l) for l in (tmp_path / "calls.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("exc,expected", [
    (Err("429 RESOURCE_EXHAUSTED quota PerDay", 429), ("daily_quota", True)),
    (Err("429 RESOURCE_EXHAUSTED per minute", 429), ("rate_limited", False)),
    (Err("401 bad key", 401), ("auth_error", False)),
    (Err("404 model not found", 404), ("unavailable", False)),
    (Err("503 overloaded", 503), ("provider_error", False)),
    (Err("Connection reset"), ("network", False)),
])
def test_classify_error(exc, expected):
    assert MP.classify_error(exc) == expected


def test_thought_parts_are_removed_and_empty_turns_dropped():
    out = MP.without_thought_parts(request().contents)
    assert len(out) == 2 and all(not getattr(p, "thought", False) for c in out for p in c.parts)
    assert out[1].parts[0].function_call.name == "run_diagnostics"


def test_primary_error_falls_back_to_groq_with_model_swapped_and_thoughts_stripped(tmp_path):
    groq = FakeModel(text_response("from groq"))
    p, ctx = plugin(tmp_path, groq=groq), Ctx()
    resp = asyncio.run(p.on_model_error_callback(callback_context=ctx, llm_request=request(),
                                                 error=Err("404 model not found", 404)))
    assert resp.content.parts[0].text == "from groq"
    assert resp.custom_metadata["rt_provider"] == "groq" and resp.custom_metadata["rt_degraded"] is False
    sent = groq.requests[0]
    assert sent.model == "groq/gpt-oss" and len(sent.contents) == 2          # model swapped, thoughts gone
    assert ctx.state["llm_provenance"]["supervisor"]["provider"] == "groq" and "llm_degraded" not in ctx.state
    assert [(r["provider"], r["outcome"]) for r in rows(tmp_path)] == [("gemini", "unavailable"), ("groq", "ok")]


def test_open_breaker_skips_primary_before_the_call(tmp_path):
    groq = FakeModel(text_response("from groq"))
    p, ctx = plugin(tmp_path, groq=groq), Ctx()
    asyncio.run(p.on_model_error_callback(callback_context=ctx, llm_request=request(),
                                          error=Err("429 RESOURCE_EXHAUSTED quota PerDay", 429)))
    resp = asyncio.run(p.before_model_callback(callback_context=ctx, llm_request=request()))
    assert resp is not None and resp.custom_metadata["rt_provider"] == "groq"
    assert rows(tmp_path)[-2]["outcome"] == "skipped_breaker"


def test_ollama_answer_sets_degraded(tmp_path):
    p = plugin(tmp_path, groq=FakeModel(Err("Connection timed out")), ollama=FakeModel(text_response("local")))
    ctx = Ctx()
    resp = asyncio.run(p.on_model_error_callback(callback_context=ctx, llm_request=request(), error=Err("503", 503)))
    assert resp.custom_metadata["rt_degraded"] is True and ctx.state["llm_degraded"] is True
    assert ctx.state["llm_provenance"]["supervisor"] == {"provider": "ollama", "model": "qwen", "degraded": True,
                                                         "fallback": True}


def test_everything_failing_returns_none_so_adk_raises(tmp_path):
    p = plugin(tmp_path, groq=FakeModel(Err("x")), ollama=FakeModel(Err("y")))
    assert asyncio.run(p.on_model_error_callback(callback_context=Ctx(), llm_request=request(),
                                                 error=Err("503", 503))) is None


def test_after_model_logs_primary_but_not_our_fallback_responses(tmp_path):
    p, ctx = plugin(tmp_path), Ctx()
    asyncio.run(p.after_model_callback(callback_context=ctx, llm_response=text_response("native")))
    fb = text_response("fb")
    fb.custom_metadata = {"rt_fallback": True}
    asyncio.run(p.after_model_callback(callback_context=ctx, llm_response=fb))
    assert [(r["provider"], r["outcome"]) for r in rows(tmp_path)] == [("gemini", "ok")]
    assert ctx.state["llm_provenance"]["supervisor"]["fallback"] is False


# --- Supervisor tool ---------------------------------------------------------------------------

class ToolCtx:
    def __init__(self, state):
        self.state = state


def test_run_diagnostics_uses_state_evidence_and_rejects_unknown_resources():
    seen = []

    def diagnose(resources, evidence):
        seen.append((resources, evidence))
        return mock_diagnose(resources, evidence)

    tool = make_run_diagnostics(diagnose)
    tc = ToolCtx({"evidence": {"trigger": {"value": 41.7}}})
    out = asyncio.run(tool(["cpu", "ram", "gpu", "cpu"], "PSI 41.7 and history 7/10", tc))   # tool is async since 8c
    assert out["consulted"] == ["cpu", "ram"] and out["ignored_unknown"] == ["gpu"]
    assert seen == [(["cpu", "ram"], {"trigger": {"value": 41.7}})]        # numbers came from state
    assert tc.state["delegation"]["resources"] == ["cpu", "ram"] and len(tc.state["findings"]) == 2
