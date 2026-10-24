# ResourceTuner

An agentic AI system for intelligent resource optimization in developer environments.

## Setup

Python **3.13** (CrewAI does not support 3.14 yet), managed with `uv` — no sudo needed:

```bash
pip install uv            # or any other way to get uv; it was copied to ~/.local/bin/uv here
uv python install 3.13
uv venv venv --python 3.13
uv pip install --python venv/bin/python -r requirements.txt -c constraints.txt
cp .env.example .env      # then fill in the API keys
```
`constraints.txt` pins `opentelemetry-api<=1.42.1` (required by google-adk 2.12); FastAPI is held
at 0.141.x for the same reason. The previous Python 3.14 environment is kept as `venv-py314/`.

## Telemetry (Step 1)

Container metrics are read from cgroup v2 files rather than `container.stats()`; the measured
rationale (~250ms/container for `stats()`, ~1.2s for `containers.list()`, and `docker stats`
reporting an impossible 124% CPU for a 0.5-CPU-capped container) is documented at the top of
`resourcetuner/telemetry/containers.py`.


```bash
./venv/bin/python -m resourcetuner.telemetry.host        # host snapshot x3
./venv/bin/python -m resourcetuner.telemetry.containers  # per-container snapshot x3
./venv/bin/python -m pytest tests/test_telemetry.py -v
```

## Synthetic workloads (Step 2)

| Image | What it does | Intensity knobs (env) | Ground-truth label |
|---|---|---|---|
| `build-sim` | Clean build of zstd 1.5.6 (real C project) in a loop, one JSON line per build | `JOBS`, `ITERATIONS`, `PAUSE_S` | `build` |
| `indexer-sim` | Walks/reads/hashes/tokenizes a synthetic source tree, keeps an in-memory cache, persists an index | `CORPUS_MB`, `RETAIN_MB`, `EVICT_CACHE`, `PASS_PAUSE_S` | `code_indexing` |
| `cpu-hog` | stress-ng CPU workers | `CPU_WORKERS`, `CPU_LOAD` | `cpu_hog` |
| `ram-hog` | stress-ng resident memory (always with a hard `mem_limit`) | `VM_WORKERS`, `VM_BYTES`, `VM_HANG` | `ram_hog` |
| `io-hog` | stress-ng direct (uncached) disk writes | `HDD_WORKERS`, `HDD_BYTES`, `HDD_OPTS` | `io_hog` |
| `test-sim` | Runs a real pytest suite (pytest-xdist workers) in a loop, one JSON line per run | `WORKERS`, `TEST_COUNT`, `TEST_SCALE`, `MB_PER_WORKER`, `ITERATIONS` | `testing` |
| `idle-sim` | `sleep infinity` | – | `idle` |

The `workload=` label is ground truth for calibration only — it is never a classifier input.

```bash
./venv/bin/python scripts/run_scenario.py build                 # build all images (needs network once)
./venv/bin/python scripts/run_scenario.py list                  # scenarios + intensity levels
./venv/bin/python scripts/run_scenario.py start build_vs_cpu --level high
./venv/bin/python scripts/run_scenario.py start build_only --set build.ITERATIONS=3
./venv/bin/python scripts/run_scenario.py start cpu_pressure --level low --duration 60
./venv/bin/python scripts/run_scenario.py status                # live metrics via our telemetry
./venv/bin/python scripts/run_scenario.py stop                  # removes only resourcetuner-managed containers
docker logs rt-build_only-build                                 # per-build JSON (duration_s, success)
```

Safety: memory hogs must declare `mem_limit` (the VM has no swap), and their caps may not
total more than 60% of currently available RAM; one scenario runs at a time.

### Measured behaviour (Step 2 validation, Ryzen 7 7435HS host; rows marked * were re-measured after moving the VM to 6 vCPU)

| Check | Setting | Measured (via our telemetry) |
|---|---|---|
| cpu-hog | 2 / 4 / 8 workers | ~191% / ~405% / ~709% CPU |
| ram-hog | 512M under 1g cap | 516MiB resident, 0% CPU |
| ram-hog | 1200M under 1g cap | 171 limit hits, 4 OOM kills, memory PSI 0% (no swap → kill, not stall) |
| io-hog | 1 / 2 / 4 writers | 23 / 31 / 27 MiB/s (disk ceiling); host I/O PSI 6.9 / 14.3 / 26.9% |
| build-sim | JOBS=4, no contention | 31.2–38.1s over 5 builds (mean ~33.8s, ≈±10% noise) |
| build-sim * | JOBS=4, no contention (6 vCPU) | 26.8s |
| build-sim * | vs cpu-hog 8 workers (6 vCPU) | 46.9s (~1.75×); hog got ~307% (fair cgroup share) |
| indexer-sim | defaults | 256MB read per ~30s pass, ~384MiB working set, ~80% CPU |

Single-run build-time comparisons cannot resolve changes below ~15% on this VM; evaluation
needs repeated runs.

Note: with 8 vCPU the VM suffered whole-VM freezes of 11–113s under CPU load (kernel
clocksource watchdog "Long readout interval"), which inflated one build to 811s, made
`docker update` take 60s, and left cpu.max-throttled containers unkillable. After moving the
VM to 6 vCPU and rebooting, the same tests showed no freezes (20ms poll loop, 30s at full load),
`--cpus 0.5` measured 49.4%, and docker operations took <1s.

## Workload classifier (Step 3)

`resourcetuner/classifier/rules.py` — deterministic, no LLM, never reads the ground-truth label.

* Process identity = executable, or the script/module an interpreter runs (`python -m pytest` → `pytest`).
* Processes without their own signature inherit a workload class from their nearest matching
  ancestor (pytest-xdist workers run as `python -c ...`; only the parent says `pytest`).
* Foreground classes (BUILD, TESTING, DEV_SERVER) outrank CODE_INDEXING, which is reported as
  background activity while a foreground class is active.
* "Confidence" = min(dominance, persistence), both measured ratios; no invented scores.
* `needs_llm_fallback` is mechanical: low confidence, two foreground classes each ≥ 1 core
  (conflict), or ≥ 1.5 cores unattributed with no active class (unknown signature).
* Thresholds: `config/thresholds.yaml` (calibrated in Step 4, see below).
* Activity = CPU + block-I/O wait per process (`delayacct_blkio_ticks`), so I/O-bound work is not
  mistaken for idle. Needs `sudo sysctl kernel.task_delayacct=1` (off by default since Linux 5.14,
  resets on reboot); without it activity is CPU only and the dashboard shows a warning.

```bash
./venv/bin/python -m pytest tests/test_classifier.py -q
./venv/bin/python scripts/validate_classifier.py            # live, all scenarios (~7 min)
```

Live validation (6 vCPU VM, 16 classified ticks per scenario, window 5 @ 1 Hz): host workload,
fallback decision and per-container class matched expectations on 100% of ticks in all 11
scenarios (idle, build/test/indexer alone, build/test vs CPU hog, build vs indexer, build+test
conflict, cpu/io/ram pressure). Lowest margin: the build+test conflict case had confidence
0.51–0.52 (fallback correctly triggered); all single-workload cases had confidence 1.0.

## Threshold calibration (Step 4)

```bash
./venv/bin/python scripts/collect_telemetry.py idle --duration 480 --variant realistic   # VM idle, IDE open
./venv/bin/python scripts/collect_telemetry.py plan                                     # 28 load/congestion runs (~29 min)
./venv/bin/python scripts/calibrate_thresholds.py --write    # -> config/thresholds.yaml + docs/step4_calibration.md
./venv/bin/python scripts/validate_classifier.py
```

2220 labelled 1 Hz samples (960 idle in two baselines, 135 single-workload "load", 1125 congestion at three
intensities). One rule for every threshold: if the uncontended p99 is below the contended p5, take
their midpoint; otherwise maximise Youden's J. CPU contention ground truth = designed CPU demand >
vCPUs (read from the scenario files). Key results: CPU PSI 15.6% (FPR 0.00 / TPR 0.99), I/O PSI
18.4% (0.00 / 1.00), persistence 10 s; two idle baselines (realistic with VS Code open,
clean with it closed) move the CPU trigger by only 0.1 points; CPU utilization is not used (not separable); memory from systemd-oomd / earlyoom conventions because
host memory PSI never rises without swap. Classifier accuracy after recalibration: 100% on the 11
original scenarios; a new I/O-starved-indexer case improved from 12% to 75%, and to 100% after
counting block-I/O wait as activity (all 12 scenarios at 100%). The classifier thresholds were
fitted on CPU-only data; the next collection logs activity, so a rerun will refit them on it. Full methodology,
tables and caveats: `docs/step4_calibration.md`.

## Live dashboard (developer viewer)

```bash
./venv/bin/python -m resourcetuner.dashboard        # then open http://localhost:8000
./venv/bin/python -m resourcetuner.dashboard --host 0.0.0.0 --port 8000   # reachable from the VM host
```

One background thread samples host + container telemetry at 1 Hz and classifies the window;
`GET /api/snapshot` returns the latest result plus 60 s of history, and the page polls it every
second. Shows host CPU / memory / disk I/O cards, the classifier result (confidence =
min(dominance, persistence), LLM-fallback reasons highlighted), a container table with each
container's classified class (ground-truth label shown alongside, for visual validation only),
and a 60 s CPU/memory chart with a per-second workload strip. The chart is drawn on a plain
`<canvas>`, so it works offline. This is a read-only viewer; the §3 pipeline dashboard
(SSE, per-stage streaming, "Analyze & Optimize") comes later.

## MCP tool server (Step 5)

```bash
./venv/bin/python -m resourcetuner.mcp_server        # stdio MCP server (dry-run backend)
./venv/bin/python -m pytest tests/test_mcp_tools.py  # handlers called directly + schema checks
./venv/bin/python scripts/test_mcp_server.py         # live: real MCP client over stdio, real containers
```

The only channel between agents and the OS. Tools: `list_targets` (read-only),
`set_container_cpu_weight` / `set_container_io_weight` (priority shift), `set_container_cpu_limit`
(throttle), `pause_container` (pause). No command/exec/kill/restart/memory/nice tool exists.
Targets are opaque `container:<name>` ids of `resourcetuner.managed=true` containers only;
anything else is an "unknown target" (protected targets are unnameable). Every argument has
hard bounds in the JSON schema and is re-validated in the handler; every action records the exact
old value from the cgroup. A Safety Gate hook runs inside the server before the actuator, and a
state-changing actuator refuses to start without one. Step 5 ships only the dry-run actuator.
`nice`/`ionice` is excluded because restoring a container process's nice fails without
CAP_SYS_NICE (tested), which would break exact rollback.

## Safety Gate (Step 6)

`resourcetuner/safety/gate.py` — deterministic, the only thing that can approve an action, and
wired *inside* the MCP tool layer so no agent can bypass it. Policy: `config/safety.yaml`
(hand-maintained; `thresholds.yaml` is generated). Ten checks run on every action and a
rejection lists every failed one: allowed action/knob, allowed target (+ protected name
patterns), reversibility (exact old value known), policy bounds (weights 10–1000, CPU limit
≥ 0.5 core), jump size (≤ 5× per weight change, ≤ 4× per limit cut), foreground protection
(BUILD/TESTING/DEV_SERVER and the host's current workload are never deprioritised, throttled or
paused; unknown class → reject), pause policy, cooldown (45 s per target), at most one
action in flight system-wide, and degraded source rejection. A check that crashes rejects. Runtime facts come from a
`GateContext` (static in tests; live classifier + Resource Controller action store in Step 7).

```bash
./venv/bin/python -m pytest tests/test_safety_gate.py -v   # 35 hand-written approve/reject cases
```
Every rule was mutation-tested: disabling any one of the ten makes the suite fail.

## Resource Controller + rollback storage (Step 7)

`resourcetuner/controller/resource_controller.py` — the only code that changes the system, and
only reachable as the actuator behind the MCP tool layer and its Safety Gate.

```bash
./venv/bin/python -m resourcetuner.mcp_server --backend docker   # real backend (gate + live classifier)
./venv/bin/python -m pytest tests/test_controller.py -v           # unit (fake ops) + live Docker
./venv/bin/python scripts/test_controller_live.py                 # MCP -> gate -> controller -> cgroups
```

* Write-ahead SQLite store (`data/actions.db`): every action is stored as `pending` with its exact
  old value before anything changes; lifecycle `pending → applied → kept | rolled_back |
  rollback_failed | target_gone` (or `apply_failed`). A container that disappears is recorded as
  `target_gone`, never an exception. `rollback_unresolved()` restores everything after a crash.
* Every apply and rollback is verified by reading the cgroup back; a mismatch restores the old value.
* Measured on this VM (Docker 29.1.3, runc 1.4.0):
  * `cpu.weight` via `--cpu-shares` using runc's non-linear mapping (256→35, 1024→100); every
    weight 10–1000 is exactly reachable; restore verified.
  * `cpu.max` via `--cpu-quota/--cpu-period` — `--cpus 0` is silently ignored and leaves
    `NanoCpus` set, so it is not used; `--cpu-quota -1` restores unlimited with a clean config.
    A 0.5-core cap measured 50% CPU on a busy loop, 100% again after rollback, no stalls (6 vCPU).
  * `freeze` via `docker pause/unpause` (paused busy loop: <5% CPU).
  * `io.weight` disabled: Docker cannot restore it exactly (102 or 91, not 100) and it has no
    effect here (mq-deadline without io.cost: weight 1 vs 100 → 85 vs 85 MiB/s).

## Diagnostic crew — CPU agent (Step 8a)

CrewAI agent `resourcetuner/agents/diagnostic_crew/cpu_agent.py`, run as a crew by `crew.py`
(`run_diagnostic_crew(evidence)` — the ADK Supervisor will call this as one step in 8c).
It gets pre-summarised evidence and returns a `CpuDiagnosis` (no confidence field): contention
yes/no, culprit targets, cited evidence, and at most one action from the enabled MCP tools.
`check_diagnosis` (plain code) rejects unknown targets, out-of-schema actions and any cited number
that is not in the evidence.

LLM calls go through `resourcetuner/agents/llm_router.py` (CrewAI `BaseLLM` adapter), configured in
`config/llm.yaml`: Groq `gpt-oss-120b` → OpenRouter free (`nemotron-3.5-super-vl` → `nemotron-3-super`,
with `require_parameters`, `json_schema`, response-healing) → local Ollama (only this last step is
flagged `degraded`). The prompt is kept compact (~1.1k prompt tokens: the output schema travels in
`response_format`, not in the prompt) and agents run `agent_gap_s` apart, so a CPU+RAM+IO round stays
inside Groq's 8000 tokens/min. A circuit breaker skips a model after a 429
(Retry-After / 60 s), the whole free tier after a daily-quota 429 (6 h), and a provider after a
network/5xx error (30 s); state persists in `data/llm_breaker.json`. Structured output falls back
from `json_schema` to `json_object` + Pydantic validation if a provider rejects the schema. Every
attempt is logged to `data/llm_calls.jsonl` with provider, model, outcome, latency and tokens.

```bash
cp .env.example .env                                     # fill OPENROUTER_API_KEY, GROQ_API_KEY (GOOGLE_API_KEY for 8b)
./venv/bin/python scripts/check_llm_providers.py         # probe each model once (verifies Groq json_schema)
./venv/bin/python scripts/test_diagnostic_crew.py        # real run on hand-written evidence
./venv/bin/python -m pytest tests/test_diagnostic_crew.py  # offline: router + checks + a real CrewAI crew
```

Live check (2026-10-22): `nemotron-3-super:free` returned 404 from its upstream (NVIDIA) and is
skipped by the breaker; `nemotron-3.5-super-vl:free` answered the build-vs-indexer case correctly
(both indexers as culprits, cpu weight 100→50 on one, every cited number verified) in ~25 s;
Groq `gpt-oss-120b` gave the same answer in ~3 s with `json_schema`. OpenRouter's free tier
allows 50 requests/day in total; Groq 1000 requests and 8000 tokens/min.

Local fallback: Ollama (`qwen2.5:3b`) runs on the **Windows host**, reached over the VirtualBox host-only
network (`OLLAMA_BASE_URL=http://192.168.56.1:11434` in `.env`; connect timeout 3 s, so an unreachable
host is skipped by the breaker immediately). It used to run inside the VM, where a diagnosis held the VM
at ~99% CPU for ~75 s — it made the contention it was diagnosing worse — so the VM-local install was
removed. On the host a diagnosis takes 8–18 s. Ollama answers carry `degraded: true` and, by decision
(degraded mode b), their action is dropped. The 3B model's answers are often wrong (BUILD named as
culprit, contention claimed with PSI 4.0), which the deterministic checks in `diagnostic_crew/checks.py`
catch: protected culprit, reducing a protected workload, no-op action, repeated evidence, and a cited
value that does not belong to its metric. (`agents/ollama_local.py` and the `autostart` option are left
over from the VM install and are unused.)


## ADK Supervisor (Step 8b) — in isolation, against a mock crew

`resourcetuner/agents/adk/supervisor.py`: a Google ADK `LlmAgent` that decides **which** diagnostic
specialists to consult (cpu / ram / io) from the trigger summary, per-resource numbers and
pre-summarised history, through one tool `run_diagnostics(resources, reason)`. The tool reads the
evidence from session state, so the model chooses specialists but cannot inject numbers. In 8b the
tool calls `mock_diagnose`; 8c swaps in the real CrewAI crew.

`resourcetuner/agents/adk/model_plugin.py`: `ModelFallbackPlugin`, registered once on the ADK Runner,
keeps ADK's native Gemini flow and adds — through ADK 2.12's `before_model` / `on_model_error` /
`after_model` hooks — the circuit breaker (shared with the CrewAI router), the fallback chain
Gemini → Groq → Ollama (via ADK `LiteLlm`, tool calls preserved), the call log (`chain: "adk"`),
per-agent provenance in session state, and the degraded flag (only when Ollama answered).

```bash
./venv/bin/python scripts/test_adk_supervisor.py --mode gemini     # native ADK + plugin
./venv/bin/python scripts/test_adk_supervisor.py --mode groq       # Groq via LiteLlm as the agent's model
./venv/bin/python scripts/test_adk_supervisor.py --mode fallback   # Gemini 404 -> plugin answers from Groq
./venv/bin/python -m pytest tests/test_adk.py
```
Measured 2026-10-24: all three modes chose cpu+ram for a CPU trigger whose history links RAM
pressure to CPU saturation for BUILD (7/10), and io only for a pure I/O trigger, each with exactly
one tool call. Two integration fixes were needed: LiteLLM pinned to 1.105.0 (above), and model
"thought" parts are stripped from requests sent to Groq (ADK re-sends them as `reasoning_content`,
which Groq rejects on the turn after a tool result). The `GOOGLE_API_KEY` ("AQ." prefix) works in
AI Studio mode, not Vertex (401). Gemini model is provisional: `gemini-3.5-flash-lite`.


## Supervisor → real diagnostic crew, and degraded mode (Step 8c)

The Supervisor's `run_diagnostics` tool now calls `adk/crew_bridge.make_crew_diagnose()` — the real
CrewAI CPU/RAM/I/O agents — instead of the mock. The cycle's single combined evidence dict lives in
session state; each agent gets its own projection (`diagnostic_crew/evidence.py`, ~45–55% of the full
evidence), and its deterministic checks run on that same view. Specialists run one after another,
`agent_gap_s` (20 s) apart, to stay inside Groq's 8000 tokens/min. The crew runs in a worker thread so
it never blocks ADK's event loop. Gemini models come from `config/llm.yaml` (`adk_models`: supervisor
`gemini-3.5-flash-lite`, decision and meta `gemini-3.6-flash`); no `max_output_tokens` is set on them.

Degraded mode (b) is code, not a prompt: if any ADK agent was answered by the local model the plugin
sets `llm_degraded`, and `adk/action_guard.DegradedActionBlocker` (ADK `before_tool_callback`) refuses
every action tool of that cycle before its body runs — so the Safety Gate, the MCP handlers and the
Resource Controller are never reached. Crew findings written by the local model are kept for the log
but are not actionable (`Finding.actionable`); `actions_for_decision` is empty for them.

```bash
./venv/bin/python scripts/test_adk_supervisor.py --mode gemini --crew real     # live, real crew
GOOGLE_API_KEY=x GROQ_API_KEY=x OPENROUTER_API_KEY=x \
  ./venv/bin/python scripts/test_adk_supervisor.py --crew real --scenario cpu_ram   # live degraded path
./venv/bin/python -m pytest tests/test_supervisor_crew.py                      # offline end-to-end
```
Live results (2026-10-24): cloud path chose cpu+ram, both findings passed their checks, and exactly one
action (indexer-1 weight → 50) was offered to the Decision stage; with all three cloud keys invalid, the
Supervisor ran on host Ollama (tool call correct, `llm_degraded=True`), both findings were degraded, and
0 actions were offered. The rule was mutation-tested: disabling the blocker, the flag, or the filter
each makes the 8c tests fail.


## Full Vertical Slice & Prototype Milestone (Step 9)

Phase 2 prototype milestone: the complete end-to-end autonomous optimization cycle executes across all pipeline stages:
Telemetry Trigger (10s CPU PSI >= 15.6%) → ADK Supervisor (`gemini-3.5-flash-lite`) → CrewAI Diagnostic Crew (CPU/RAM/IO)
→ ADK Decision Agent (`gemini-3.6-flash`) → Safety Gate (10 deterministic checks) → Resource Controller (cgroup v2)
→ Evaluation Engine (discrete delta vs baseline & continuous rolling PSI) → Meta-Agent (`gemini-3.6-flash`) → Policy Store.

1. **Policy Store & Schema** (`resourcetuner/policy/schema.sql`, `resourcetuner/policy/store.py`):
   Thread-safe SQLite store (`data/policy.db`) tracking historical outcomes per `(workload, condition, action_tool, action_target, action_param)` tuple.
   Maintains sample counts, success/failure counts, exponential moving average of improvement (`alpha=0.3`), and promotion status.
2. **Statistics Engine** (`resourcetuner/stats/statistics_engine.py`):
   Deterministic Python pre-summarizer for agents. Produces bounded top-3 relevance summaries with success rates, EMA improvements, trends (`improving`, `stable`, `degrading`), and last outcome.
3. **Decision Agent** (`resourcetuner/agents/adk/decision.py`):
   ADK Agent producing structured Pydantic `DecisionOutput` (chosen candidate action + number-citing reasoning, no fabricated confidence).
   Execution is carried out by the deterministic `execute_decision` executor through `ToolHandlers` at the MCP boundary.
4. **Evaluation Engine** (`resourcetuner/evaluation/engine.py`):
   Implements two evaluation paradigms (approved decisions 2-A & 2-D):
   - Discrete delta for `BUILD`/`TESTING`: reads completion `duration_s` directly from the container log stream, comparing against baseline. Thresholds in `config/thresholds.yaml`: `<= -10%` -> `IMPROVED`, `>= +10%` -> `DEGRADED`, otherwise `UNCHANGED`.
   - Rolling window for continuous workloads (`CODE_INDEXING`, `DEV_SERVER`): compares 60s post-action vs pre-action CPU PSI. Thresholds: `<= -15%` -> `IMPROVED`, `>= +10%` -> `DEGRADED`.
   - On `DEGRADED` or `TIMEOUT`, the engine automatically initiates `controller.rollback(action_id)`. On `IMPROVED` or `UNCHANGED`, it executes `controller.keep(action_id)`.
5. **Meta-Agent** (`resourcetuner/agents/adk/meta.py`):
   Governs policy promotion and demotion. Employs deterministic pre-filtering (`sample_count >= 5` floor, degraded mode lockout, `< 50%` reject) to minimize LLM quota consumption, calling the LLM strictly for borderline and promotion reasoning.
6. **Milestone Orchestration** (`scripts/run_vertical_slice.py`):
   Orchestrates the complete loop on a single ADK Runner with sequential agent handoff and shared session state.

```bash
# Baseline variance collection (5 runs without ResourceTuner):
# Result: [54.96, 41.57, 37.26, 36.14, 34.56] -> mean = 40.90s, std dev = 7.99s (warm mean = 37.38s)

# Run full vertical slice milestone:
./venv/bin/python scripts/run_vertical_slice.py --cycles 5 --scenario build_vs_indexer --level high --baseline-duration 37.38
./venv/bin/python -m pytest tests/test_step9_vertical_slice.py -v   # 13 tests passed
```

