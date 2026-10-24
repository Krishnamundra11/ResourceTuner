"""Step 8a, offline: LLM router (chain, circuit breaker, structured modes, degraded flag, call log),
the CPU agent's deterministic checks, and a real CrewAI crew driven by a fake transport."""

import json

import pytest

from resourcetuner.agents import llm_router as R
from resourcetuner.agents.diagnostic_crew.cpu_agent import check_diagnosis
from resourcetuner.agents.diagnostic_crew.sample_evidence import BUILD_VS_INDEXER
from resourcetuner.agents.diagnostic_crew.schemas import CpuDiagnosis

M1, M2, M3 = "nvidia/nemotron-3-super-120b-a12b:free", "nvidia/nemotron-3.5-super-vl:free", "xingchen-agi/xing4.0-29b-a4b:free"
GROQ, OLLAMA = "openai/gpt-oss-120b", "llama3.2:3b"
IDX1 = "container:rt-build_vs_indexer-indexer-1"

GOOD = {
    "resource": "cpu", "contention": True,
    "summary": "Host CPU PSI 41.7% while the BUILD runs; two indexers take ~1.3 cores.",
    "culprit_target_ids": [IDX1, "container:rt-build_vs_indexer-indexer-2"],
    "evidence": [{"metric": "host.cpu_psi", "value": 41.7, "statement": "tasks wait for CPU 41.7% of the time"},
                 {"metric": "indexer-1.cpu_percent", "value": 65.2, "statement": "indexer-1 uses 0.65 core"}],
    "candidate_action": {"tool": "set_container_cpu_weight", "target_id": IDX1, "weight": 25, "cpus": None,
                         "rationale": "lower the background indexer's CPU share"},
    "historical_basis": "BUILD/cpu_contention -> set_container_cpu_weight on CODE_INDEXING: 8/10 successes",
}


def config(tmp_path, chain=None):
    return {
        "providers": {
            "openrouter": {"base_url": "http://or", "api_key": "k", "structured_modes": ["json_schema"],
                           "extra_body": {"provider": {"require_parameters": True}}},
            "groq": {"base_url": "http://groq", "api_key": "k", "structured_modes": ["json_schema", "json_object"]},
            "ollama": {"base_url": "http://ollama/v1", "api_key": "ollama", "structured_modes": ["json_schema", "json_object"],
                       "degraded": True},
        },
        "chains": {"diagnostic": chain or [
            {"provider": "openrouter", "model": M1}, {"provider": "openrouter", "model": M2},
            {"provider": "openrouter", "model": M3}, {"provider": "groq", "model": GROQ},
            {"provider": "ollama", "model": OLLAMA}]},
        "circuit_breaker": {"rate_limit_cooldown_s": 60, "daily_quota_cooldown_s": 21600, "error_cooldown_s": 30,
                            "max_cooldown_s": 86400, "state_file": str(tmp_path / "breaker.json")},
        "request": {"timeout_s": 5, "temperature": 0.1, "max_tokens": 500},
    }


class FakeTransport:
    """script: {model: [response-or-exception, ...]} consumed in order; default = GOOD JSON."""

    def __init__(self, script=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []

    def chat(self, provider, model, messages, response_format, timeout_s, temperature, max_tokens):
        self.calls.append({"provider": provider.name, "model": model, "rf": response_format, "messages": messages,
                           "extra_body": provider.extra_body})
        queue = self.script.get(model)
        item = queue.pop(0) if queue else json.dumps(GOOD)
        if isinstance(item, Exception):
            raise item
        return item, {"prompt_tokens": 100, "completion_tokens": 50}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def router(tmp_path, script=None, chain=None, clock=None):
    t = FakeTransport(script)
    return R.LLMRouter(config(tmp_path, chain), transport=t, call_log=tmp_path / "calls.jsonl",
                       clock=clock or Clock()), t


MSG = [{"role": "user", "content": "diagnose"}]


# --- router -------------------------------------------------------------------------------

def test_strict_schema_requires_every_field():
    s = R.strict_json_schema(CpuDiagnosis)
    assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
    action = s["$defs"]["CandidateAction"]
    assert action["additionalProperties"] is False and "weight" in action["required"]


def test_first_provider_answers_with_json_schema_and_require_parameters(tmp_path):
    r, t = router(tmp_path)
    res = r.complete("diagnostic", MSG, CpuDiagnosis)
    assert (res.provider, res.model, res.degraded, res.structured_mode) == ("openrouter", M1, False, "json_schema")
    assert isinstance(res.parsed, CpuDiagnosis)
    assert t.calls[0]["rf"]["type"] == "json_schema" and t.calls[0]["rf"]["json_schema"]["strict"] is True
    assert t.calls[0]["extra_body"]["provider"]["require_parameters"] is True


def test_daily_quota_skips_all_free_models_and_is_remembered(tmp_path):
    r, t = router(tmp_path, {M1: [R.RateLimited("429 free-models-per-day exceeded", daily=True)]})
    res = r.complete("diagnostic", MSG, CpuDiagnosis)
    assert res.provider == "groq"
    assert [a.outcome for a in res.attempts] == ["daily_quota", "skipped_breaker", "skipped_breaker", "ok"]
    n = len(t.calls)
    again = r.complete("diagnostic", MSG, CpuDiagnosis)          # no request to OpenRouter at all
    assert again.provider == "groq" and [c["model"] for c in t.calls[n:]] == [GROQ]
    r2, t2 = router(tmp_path)                                    # new process: state file persisted
    assert r2.complete("diagnostic", MSG, CpuDiagnosis).provider == "groq" and t2.calls[0]["model"] == GROQ


def test_per_minute_rate_limit_only_skips_that_model(tmp_path):
    clock = Clock()
    r, t = router(tmp_path, {M1: [R.RateLimited("429 rate limited", retry_after_s=20)]}, clock=clock)
    assert r.complete("diagnostic", MSG, CpuDiagnosis).model == M2
    assert r.complete("diagnostic", MSG, CpuDiagnosis).model == M2   # M1 still cooling down
    clock.t += 61
    assert r.complete("diagnostic", MSG, CpuDiagnosis).model == M1   # breaker closed again


def test_network_error_skips_the_whole_provider(tmp_path):
    r, _ = router(tmp_path, {M1: [R.NetworkError("connect timed out")]})
    res = r.complete("diagnostic", MSG, CpuDiagnosis)
    assert res.provider == "groq" and [a.outcome for a in res.attempts][:3] == ["network", "skipped_breaker", "skipped_breaker"]


def test_unsupported_json_schema_downgrades_to_json_object_with_schema_in_prompt(tmp_path):
    chain = [{"provider": "groq", "model": GROQ}]
    r, t = router(tmp_path, {GROQ: [R.BadRequest("400: response_format json_schema is not supported")]}, chain=chain)
    res = r.complete("diagnostic", MSG, CpuDiagnosis)
    assert res.provider == "groq" and res.structured_mode == "json_object"
    assert t.calls[1]["rf"] == {"type": "json_object"} and "JSON schema" in t.calls[1]["messages"][0]["content"]
    r.complete("diagnostic", MSG, CpuDiagnosis)
    assert t.calls[2]["rf"] == {"type": "json_object"}                # downgrade remembered


def test_invalid_output_moves_on(tmp_path):
    r, _ = router(tmp_path, {M1: ['{"resource": "cpu"}']})
    res = r.complete("diagnostic", MSG, CpuDiagnosis)
    assert res.model == M2 and res.attempts[0].outcome == "invalid_output"


def test_fenced_json_is_accepted(tmp_path):
    r, _ = router(tmp_path, {M1: ["Here you go:\n```json\n" + json.dumps(GOOD) + "\n```"]})
    assert r.complete("diagnostic", MSG, CpuDiagnosis).model == M1


def test_only_ollama_is_degraded(tmp_path):
    fail = R.ProviderError("503")
    r, _ = router(tmp_path, {M1: [fail], GROQ: [R.NetworkError("down")]})
    res = r.complete("diagnostic", MSG, CpuDiagnosis)
    assert (res.provider, res.degraded) == ("ollama", True)
    r2, _ = router(tmp_path / "b", {M1: [R.ProviderError("503")]})
    (tmp_path / "b").mkdir(exist_ok=True)
    res2 = r2.complete("diagnostic", MSG, CpuDiagnosis)
    assert (res2.provider, res2.degraded) == ("groq", False)


def test_all_fail_raises_with_every_attempt(tmp_path):
    chain = [{"provider": "groq", "model": GROQ}, {"provider": "ollama", "model": OLLAMA}]
    r, _ = router(tmp_path, {GROQ: [R.NetworkError("x")], OLLAMA: [R.NetworkError("y")]}, chain=chain)
    with pytest.raises(R.AllProvidersFailed) as e:
        r.complete("diagnostic", MSG, CpuDiagnosis)
    assert [a.outcome for a in e.value.attempts] == ["network", "network"]


def test_missing_api_key_is_skipped(tmp_path):
    cfg = config(tmp_path)
    cfg["providers"]["openrouter"].pop("api_key")
    cfg["providers"]["openrouter"]["api_key_env"] = "RT_TEST_NO_SUCH_KEY"
    r = R.LLMRouter(cfg, transport=FakeTransport(), call_log=None, clock=Clock())
    res = r.complete("diagnostic", MSG, CpuDiagnosis)
    assert res.provider == "groq" and [a.outcome for a in res.attempts][:3] == ["no_api_key"] * 3


def test_every_attempt_is_logged_with_provider_and_model(tmp_path):
    r, _ = router(tmp_path, {M1: [R.RateLimited("429", retry_after_s=5)]})
    r.complete("diagnostic", MSG, CpuDiagnosis)
    rows = [json.loads(l) for l in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [(x["provider"], x["model"], x["outcome"]) for x in rows] == [("openrouter", M1, "rate_limited"),
                                                                          ("openrouter", M2, "ok")]
    assert rows[1]["usage"] == {"prompt_tokens": 100, "completion_tokens": 50} and rows[1]["degraded"] is False


# --- deterministic checks on the diagnosis -------------------------------------------------------

def test_good_diagnosis_passes_checks():
    assert check_diagnosis(CpuDiagnosis.model_validate(GOOD), BUILD_VS_INDEXER).ok


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["evidence"].append({"metric": "made.up", "value": 93.3, "statement": "x"}), "neither a section nor a target"),
    (lambda d: d["evidence"].append({"metric": "host.cpu_wizardry", "value": 93.3, "statement": "x"}), "does not exist"),
    (lambda d: d["culprit_target_ids"].append("container:not-there"), "not a target"),
    (lambda d: d["candidate_action"].update(target_id="container:rt-elsewhere"), "action target"),
    (lambda d: d["candidate_action"].update(weight=0), "outside"),
    (lambda d: d.update(contention=False), "contention=false"),
])
def test_checks_catch_bad_answers(mutate, expect):
    d = json.loads(json.dumps(GOOD))
    mutate(d)
    res = check_diagnosis(CpuDiagnosis.model_validate(d), BUILD_VS_INDEXER)
    assert not res.ok and any(expect in i for i in res.issues)


def test_schema_has_no_confidence_field_and_only_enabled_tools():
    from resourcetuner.mcp_server.tools import ENABLED_TOOLS
    assert "confidence" not in json.dumps(CpuDiagnosis.model_json_schema())
    tools = CpuDiagnosis.model_json_schema()["$defs"]["CandidateAction"]["properties"]["tool"]["enum"]
    assert set(tools) <= set(ENABLED_TOOLS) and "set_container_io_weight" not in tools


# --- a real CrewAI crew, offline -------------------------------------------------------------------

def test_real_crewai_crew_with_fake_transport(tmp_path):
    from resourcetuner.agents.diagnostic_crew.crew import run_diagnostic_crew
    r, t = router(tmp_path, {M1: [R.RateLimited("429", retry_after_s=5)]})
    findings = run_diagnostic_crew(BUILD_VS_INDEXER, router=r)
    f = findings[0]
    assert f.error is None, f.error
    assert f.diagnosis.candidate_action.target_id == IDX1 and f.check_ok
    assert f.provenance["provider"] == "openrouter" and f.provenance["model"] == M2
    assert f.provenance["attempts"][0].endswith("rate_limited")
    assert any("Evidence: {" in m["content"] for m in t.calls[-1]["messages"])   # the task reached the LLM


def test_upstream_404_skips_that_model_for_a_while(tmp_path):
    r, t = router(tmp_path, {M1: [R.BadRequest("404: Provider returned error: Function not found for account")]})
    assert r.complete("diagnostic", MSG, CpuDiagnosis).model == M2
    n = len(t.calls)
    r.complete("diagnostic", MSG, CpuDiagnosis)
    assert [c["model"] for c in t.calls[n:]] == [M2]           # M1 not retried while unavailable


def test_auth_error_skips_the_whole_provider(tmp_path):
    r, _ = router(tmp_path, {M1: [R.BadRequest("401: invalid api key")]})
    res = r.complete("diagnostic", MSG, CpuDiagnosis)
    assert res.provider == "groq" and [a.outcome for a in res.attempts][:3] == ["auth_error", "skipped_breaker", "skipped_breaker"]


def test_per_step_extra_body_is_merged(tmp_path):
    chain = [{"provider": "openrouter", "model": M1, "extra_body": {"reasoning": {"effort": "low"}}}]
    r, t = router(tmp_path, chain=chain)
    r.complete("diagnostic", MSG, CpuDiagnosis)
    assert t.calls[0]["extra_body"] == {"provider": {"require_parameters": True}, "reasoning": {"effort": "low"}}


def test_checks_catch_the_real_ollama_answer():
    """Verbatim shape of llama3.2:3b's live answer (2026-10-22): BUILD as culprit, no-op action on it,
    one metric repeated. The first version of check_diagnosis passed it."""
    bad = {
        "resource": "cpu", "contention": True, "summary": "CPU contention.",
        "culprit_target_ids": ["container:rt-build_vs_indexer-build", "container:rt-build_vs_indexer-indexer-2"],
        "evidence": [{"metric": "host.cpu_psi", "value": 41.7, "statement": "high"}] * 6,
        "candidate_action": {"tool": "set_container_cpu_weight", "target_id": "container:rt-build_vs_indexer-build",
                             "weight": 100, "cpus": None, "rationale": "Lowering the weight will reduce contention."},
        "historical_basis": None,
    }
    res = check_diagnosis(CpuDiagnosis.model_validate(bad), BUILD_VS_INDEXER)
    text = " | ".join(res.issues)
    assert not res.ok
    assert "protected/active workload BUILD" in text and "repeated" in text and "does not change anything" in text


def test_reducing_the_build_is_flagged_even_with_a_real_change():
    d = json.loads(json.dumps(GOOD))
    d["candidate_action"].update(target_id="container:rt-build_vs_indexer-build", weight=50)
    res = check_diagnosis(CpuDiagnosis.model_validate(d), BUILD_VS_INDEXER)
    assert any("reduce resources of the protected workload BUILD" in i for i in res.issues)


def test_agents_run_one_after_another_with_a_gap(tmp_path, monkeypatch):
    from resourcetuner.agents.diagnostic_crew import crew as C
    monkeypatch.setitem(C.AGENT_BUILDERS, "cpu2", C.AGENT_BUILDERS["cpu"])
    slept = []
    r, t = router(tmp_path)
    out = C.run_diagnostic_crew(BUILD_VS_INDEXER, router=r, agents=("cpu", "cpu2"), gap_s=20, sleep=slept.append)
    assert slept == [20] and len(out) == 2 and len(t.calls) == 2      # gap only BETWEEN agents


def test_prompt_is_compact_and_has_no_duplicated_schema(tmp_path):
    from resourcetuner.agents.diagnostic_crew.crew import run_diagnostic_crew
    r, t = router(tmp_path)
    run_diagnostic_crew(BUILD_VS_INDEXER, router=r)
    msgs = t.calls[-1]["messages"]
    assert "OpenAPI schema" not in msgs[-1]["content"]                  # schema travels in response_format only
    assert sum(len(m["content"]) for m in msgs) < 3000                  # was 7025 chars before compaction


def test_degraded_mode_b_keeps_diagnosis_but_suppresses_action(tmp_path):
    from resourcetuner.agents.diagnostic_crew.crew import run_diagnostic_crew
    chain = [{"provider": "groq", "model": GROQ}, {"provider": "ollama", "model": OLLAMA}]
    r, _ = router(tmp_path, {GROQ: [R.NetworkError("down")]}, chain=chain)
    f = run_diagnostic_crew(BUILD_VS_INDEXER, router=r)[0]
    assert f.degraded and f.check_ok and f.diagnosis.candidate_action is not None   # diagnosis kept
    assert not f.actionable and f.action_for_decision is None                       # ...but no action
    assert f.to_dict()["not_actionable_reason"].startswith("degraded")


def test_cloud_answer_that_passes_checks_is_actionable(tmp_path):
    from resourcetuner.agents.diagnostic_crew.crew import run_diagnostic_crew
    r, _ = router(tmp_path)
    f = run_diagnostic_crew(BUILD_VS_INDEXER, router=r)[0]
    assert f.actionable and f.action_for_decision.target_id == IDX1


# --- RAM and I/O agents (Step 8a, second half) ---------------------------------------------------

from resourcetuner.agents.diagnostic_crew import sample_evidence as SE  # noqa: E402
from resourcetuner.agents.diagnostic_crew.io_agent import check_io_diagnosis  # noqa: E402
from resourcetuner.agents.diagnostic_crew.ram_agent import check_ram_diagnosis  # noqa: E402
from resourcetuner.agents.diagnostic_crew.schemas import IoDiagnosis, RamDiagnosis  # noqa: E402

HOG_RAM, HOG_IO, IDX_IO = "container:rt-ram_pressure-hog", "container:rt-indexer_vs_io-hog", "container:rt-indexer_vs_io-indexer"
RAM_GOOD = {"resource": "memory", "contention": True, "scope": "container",
            "summary": "The hog container is OOM-killed in a loop inside its 1 GiB limit; host memory is fine.",
            "culprit_target_ids": [HOG_RAM],
            "evidence": [{"metric": "targets.oom_kills_delta", "value": 42, "statement": "42 OOM kills in 45 s"},
                         {"metric": "host.mem_avail_pct", "value": 55.0, "statement": "host has 55% available"}]}
IO_GOOD = {"resource": "io", "contention": True, "summary": "The io-hog's writes starve the indexer.",
           "culprit_target_ids": [HOG_IO],
           "evidence": [{"metric": "host.io_psi", "value": 45.1, "statement": "tasks wait for disk 45% of the time"},
                        {"metric": "hog.io_write_mib_s", "value": 137.6, "statement": "hog writes 137.6 MiB/s"}],
           "candidate_action": {"tool": "pause_container", "target_id": HOG_IO, "rationale": "stop the background writer"},
           "historical_basis": None}


def test_ram_schema_cannot_carry_an_action():
    assert "candidate_action" not in RamDiagnosis.model_fields
    strict = R.strict_json_schema(RamDiagnosis)
    assert strict["additionalProperties"] is False and "candidate_action" not in strict["properties"]


def test_good_ram_and_io_answers_pass():
    assert check_ram_diagnosis(RamDiagnosis.model_validate(RAM_GOOD), SE.RAM_PRESSURE).ok
    assert check_io_diagnosis(IoDiagnosis.model_validate(IO_GOOD), SE.IO_CONTENTION).ok


@pytest.mark.parametrize("mutate,evidence,expect", [
    (lambda d: d.update(scope="host"), "RAM_PRESSURE", "host memory is within"),
    (lambda d: d.update(scope="none", contention=False, culprit_target_ids=[]), "RAM_PRESSURE", "scope=none"),
    (lambda d: None, "RAM_OK", "no target shows OOM"),
    (lambda d: d.update(contention=False), "RAM_PRESSURE", "contention=false"),
])
def test_ram_checks_catch_wrong_claims(mutate, evidence, expect):
    d = json.loads(json.dumps(RAM_GOOD))
    mutate(d)
    res = check_ram_diagnosis(RamDiagnosis.model_validate(d), getattr(SE, evidence))
    assert not res.ok and any(expect in i for i in res.issues), res.issues


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["candidate_action"].update(target_id=IDX_IO), "protected/active workload CODE_INDEXING"),
    (lambda d: d.update(culprit_target_ids=[IDX_IO]), "protected/active"),
    (lambda d: d.update(contention=False), "contention=false"),
    (lambda d: d["candidate_action"].update(target_id="container:nope"), "not in the evidence"),
])
def test_io_checks_catch_wrong_answers(mutate, expect):
    d = json.loads(json.dumps(IO_GOOD))
    mutate(d)
    res = check_io_diagnosis(IoDiagnosis.model_validate(d), SE.IO_CONTENTION)
    assert not res.ok and any(expect in i for i in res.issues), res.issues


def test_io_pause_must_hit_the_biggest_background_disk_user():
    ev = json.loads(json.dumps(SE.IO_CONTENTION))
    ev["targets"].append({"target_id": "container:rt-x-small", "workload": "UNKNOWN", "state": "running",
                          "io_read_mib_s": 1.0, "io_write_mib_s": 0.5})
    d = json.loads(json.dumps(IO_GOOD))
    d["candidate_action"]["target_id"] = "container:rt-x-small"
    res = check_io_diagnosis(IoDiagnosis.model_validate(d), ev)
    assert any("moving the most disk data" in i for i in res.issues)


def test_io_schema_only_allows_pause():
    with pytest.raises(Exception):
        IoDiagnosis.model_validate({**IO_GOOD, "candidate_action": {**IO_GOOD["candidate_action"],
                                                                    "tool": "set_container_cpu_weight"}})


def test_all_three_agents_run_through_crewai_with_gap(tmp_path):
    from resourcetuner.agents.diagnostic_crew.crew import run_diagnostic_crew
    script = {M1: [json.dumps(GOOD), json.dumps(RAM_GOOD), json.dumps(IO_GOOD)]}
    r, t = router(tmp_path, script)
    slept = []
    ev = {**SE.IO_CONTENTION, "targets": SE.IO_CONTENTION["targets"] + BUILD_VS_INDEXER["targets"]
          + SE.RAM_PRESSURE["targets"], "host": {**SE.RAM_PRESSURE["host"], **SE.IO_CONTENTION["host"]},
          "thresholds": SE.RAM_PRESSURE["thresholds"], "workload": {"class": "CODE_INDEXING"}}
    out = run_diagnostic_crew(ev, router=r, agents=("cpu", "ram", "io"), gap_s=20, sleep=slept.append)
    assert [f.agent for f in out] == ["cpu", "ram", "io"] and slept == [20, 20]
    assert [type(f.diagnosis).__name__ for f in out] == ["CpuDiagnosis", "RamDiagnosis", "IoDiagnosis"]
    assert out[1].action_for_decision is None                       # RAM never proposes an action


# --- the number exists in the evidence, but not for THAT metric (checks.py fix, 2026-10-24) -----------

@pytest.mark.parametrize("metric,value,expect", [
    ("host.cpu_psi", 98.8, "does not match metric"),               # 98.8 is host.cpu_percent, not cpu_psi
    ("workload.class", 1.0, "does not exist"),                     # class is the string BUILD; 1.0 is confidence
    ("indexer-1.cpu_percent", 327.0, "does not match metric"),     # 327 is the BUILD container's CPU
    ("container:rt-build_vs_indexer-build.cpu_percent", 65.2, "does not match metric"),
    ("history.avg_improvement_pct", 41.7, "does not match metric"),
])
def test_cited_value_must_belong_to_its_metric(metric, value, expect):
    d = json.loads(json.dumps(GOOD))
    d["evidence"].append({"metric": metric, "value": value, "statement": "x"})
    res = check_diagnosis(CpuDiagnosis.model_validate(d), BUILD_VS_INDEXER)
    assert not res.ok and any(expect in i for i in res.issues), res.issues


@pytest.mark.parametrize("metric,value", [
    ("host.cpu_psi", 41.7), ("trigger.threshold", 15.6), ("workload.confidence", 1.0),
    ("container:rt-build_vs_indexer-build.cpu_percent", 327.0),
    ("container:rt-build_vs_indexer-build cpu_percent", 327.0),    # the form Nemotron wrote
    ("indexer-1.cpu_percent", 65.2), ("targets.cpu_percent", 62.7), ("history.avg_improvement_pct", 12.4),
    ("host.cpu_psi", 41.65),                                       # within rounding tolerance
])
def test_metric_naming_forms_models_actually_use_are_accepted(metric, value):
    d = json.loads(json.dumps(GOOD))
    d["evidence"] = [{"metric": metric, "value": value, "statement": "x"}]
    assert check_diagnosis(CpuDiagnosis.model_validate(d), BUILD_VS_INDEXER).ok


def test_prompts_tell_the_model_how_to_name_metrics():
    from resourcetuner.agents.diagnostic_crew import cpu_agent, io_agent, ram_agent
    for mod in (cpu_agent, ram_agent, io_agent):
        assert "section.field" in mod.TASK_TEMPLATE


def test_trigger_metric_value_pair_may_be_cited_even_if_the_projection_lacks_that_field():
    from resourcetuner.agents.diagnostic_crew.evidence import project_evidence
    ram_view = project_evidence("ram", SE.COMBINED_CPU_RAM)            # no host.cpu_psi in the RAM view...
    assert "cpu_psi" not in ram_view["host"] and ram_view["trigger"]["metric"] == "host.cpu_psi"
    d = json.loads(json.dumps(RAM_GOOD_FOR_COMBINED))
    d["evidence"].append({"metric": "host.cpu_psi", "value": 41.7, "statement": "the trigger"})
    assert check_ram_diagnosis(RamDiagnosis.model_validate(d), ram_view).ok
    d["evidence"][-1]["value"] = 98.8                                  # ...but not with a different value
    assert not check_ram_diagnosis(RamDiagnosis.model_validate(d), ram_view).ok


RAM_GOOD_FOR_COMBINED = {"resource": "memory", "contention": False, "scope": "none", "culprit_target_ids": [],
                         "summary": "No memory pressure.",
                         "evidence": [{"metric": "host.mem_avail_pct", "value": 18.0, "statement": "18% available"}]}


def test_citing_a_string_field_as_a_number_is_rejected():
    d = json.loads(json.dumps(GOOD))
    d["evidence"].append({"metric": "history.action", "value": 8.0, "statement": "x"})
    res = check_diagnosis(CpuDiagnosis.model_validate(d), BUILD_VS_INDEXER)
    assert any("history.action" in i and "does not exist" in i for i in res.issues)


def test_prompts_have_no_angle_bracket_placeholders():
    """A 3B model copied "<target_id>.cpu_percent" literally (live degraded run, 2026-10-24)."""
    from resourcetuner.agents.diagnostic_crew import cpu_agent, io_agent, ram_agent
    for mod in (cpu_agent, ram_agent, io_agent):
        assert "<" not in mod.TASK_TEMPLATE.replace("{evidence}", "").replace("{tools}", "")
