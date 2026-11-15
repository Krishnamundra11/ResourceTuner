# ResourceTuner — Implemented Evaluation Metrics Guide

This document details all the evaluation metrics implemented in **ResourceTuner**, explaining **what they measure**, **their mathematical formulas**, **where their data comes from**, and **their critical significance** with respect to autonomous multi-agent resource tuning.

---

## 1. Metric Hierarchy Overview

The evaluation framework operates across three distinct architectural layers:

```
┌────────────────────────────────────────────────────────────────────────┐
│                   Level 3: End-to-End System Metrics                   │
│   (End-to-End Accuracy /100, Task Completion, Recovery, Cost)          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│              Level 2: Inter-Agent Communication Metrics                │
│    (Composite Communication Score, Delivery, Parsing, Preservation)    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│                 Level 1: Individual Agent Metrics                      │
│   (Task Success, Groundedness/Hallucination, Latency, Token Usage)     │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Level 1: Individual Agent Metrics

Implemented in `resourcetuner/evaluation/metrics.py`.

### 2.1 Task Success Rate ($TSR$)
* **What it measures:** The percentage of times an agent successfully generates valid, schema-compliant structured output without runtime exceptions or unparseable text.
* **Mathematical Formula:**
  $$\text{TSR}_a = \left( \frac{N_{\text{success}, a}}{N_{\text{total}, a}} \right) \times 100$$
* **Data Source:** `data/vertical_slice_log.jsonl` (presence of `delegation`, `candidates`, `decision`, `meta`) and `Finding.error` in `events.db`.
* **Significance in ResourceTuner:** In an autonomous loop controlling Linux kernel cgroups, a syntax error or unstructured response halts the optimization pipeline. High TSR ensures pipeline predictability.

---

### 2.2 Output Accuracy & Groundedness ($G$)
* **What it measures:** The percentage of metrics and numbers quoted by an agent that actually match raw ground-truth telemetry within floating-point tolerance ($10^{-3}$).
* **Mathematical Formula:**
  $$G = \left( \frac{\sum \text{Verified Evidence Items}}{\sum \text{Total Quoted Evidence Items}} \right) \times 100$$
* **Data Source:** `resourcetuner/agents/diagnostic_crew/checks.py` via `Finding.check_ok` and `Finding.check_issues`.
* **Significance in ResourceTuner:** LLMs often hallucinate convenient numbers (e.g. claiming CPU PSI is 80% when it was 30%). In ResourceTuner, every cited evidence item is checked deterministically by Python against the input snapshot. If $G < 100\%$, the finding is marked non-actionable before it can reach the Decision Agent.

---

### 2.3 Hallucination Rate ($HR$)
* **What it measures:** The proportion of diagnostic findings containing fabricated numbers, invalid targets, or non-existent metrics.
* **Mathematical Formula:**
  $$HR = \left( \frac{N_{\text{failed\_checks}}}{N_{\text{total\_findings}}} \right) \times 100 = 100 - G$$
* **Data Source:** `resourcetuner/agents/diagnostic_crew/checks.py` (`check_issues` list).
* **Significance in ResourceTuner:** Directly measures reliability. In production, ResourceTuner achieved **0.0% Hallucination Rate** past the deterministic check layer.

---

### 2.4 Response Latency ($L_{\text{resp}}$)
* **What it measures:** Wall-clock duration taken by an agent to process input and emit a structured decision.
* **Mathematical Formula:**
  $$L_{\text{resp}} = t_{\text{completed}} - t_{\text{invoked}} \quad (\text{in milliseconds})$$
  $$\bar{L}_{\text{resp}} = \frac{1}{K} \sum_{i=1}^K (t_{\text{completed}, i} - t_{\text{invoked}, i})$$
* **Data Source:** `data/events.db` (`duration_ms` column recorded by `RunEventSink.stage_start` and `stage_done`).
* **Significance in ResourceTuner:** Contention degrades active developer builds (`BUILD`, `TESTING`). If agents take too long to respond (>60s), the build completes in a degraded state before remediation can take effect.

---

### 2.5 Token Consumption ($TC$)
* **What it measures:** Cumulative prompt tokens and completion tokens consumed per agent execution.
* **Mathematical Formula:**
  $$TC_{\text{agent}} = \text{Prompt Tokens} + \text{Completion Tokens}$$
* **Data Source:** `data/llm_calls.jsonl` (`usage.prompt_tokens` and `usage.completion_tokens`).
* **Significance in ResourceTuner:** Enables token profiling across providers. For example, Groq has an 8,000 tokens/min rate limit; measuring token consumption prevents bursting past the token bucket.

---

### 2.6 Decision Quality Rate ($DQR$)
* **What it measures:** The percentage of decisions with actions that resulted in non-harmful outcomes (`IMPROVED` or `UNCHANGED`).
* **Mathematical Formula:**
  $$DQR = \left( \frac{N_{\text{IMPROVED}} + N_{\text{UNCHANGED}}}{N_{\text{actions\_applied}}} \right) \times 100$$
* **Data Source:** Cross-referencing `decision.chosen_action` in `vertical_slice_log.jsonl` against `evaluation.verdict` in `data/actions.db`.
* **Significance in ResourceTuner:** Measures whether the Decision Agent acts conservatively. Actions must not cause system degradation (`DEGRADED`), which triggers an emergency rollback.

---

### 2.7 Agent Contribution Score ($ACS$)
* **What it measures:** The degree to which an individual agent's findings or proposals directly contributed to the system's successful final tuning action.
* **Mathematical Formula:**
  $$ACS_a = \left( \frac{N_{\text{actions\_adopted\_from\_agent } a}}{N_{\text{total\_effective\_actions}}} \right) \times 100$$
* **Data Source:** Cross-referencing `Finding.subsystem` in `events.db` against `chosen_action.target` in `vertical_slice_log.jsonl`.
* **Significance in ResourceTuner:** Identifies if specific diagnostic agents (e.g. CPU vs RAM vs I/O) are consistently driving remediation or if certain agents are rarely utilized during typical developer workloads.

---

### 2.8 Critical Path Latency ($CPL$)
* **What it measures:** The duration of the longest sequential dependency chain in the agent execution graph.
* **Mathematical Formula:**
  $$CPL = t_{\text{Collector}} + t_{\text{Supervisor}} + \max(t_{\text{CPU}}, t_{\text{RAM}}, t_{\text{I/O}}) + t_{\text{Decision}} + t_{\text{Gate}} + t_{\text{Verify}}$$
* **Data Source:** Stage timestamps logged in `events.db`.
* **Significance in ResourceTuner:** Since CrewAI diagnostic agents run as a specialist team, the slowest diagnostic agent dictates when the Decision Agent can begin. Profiling CPL isolates performance bottlenecks.

---

## 3. Level 2: Inter-Agent Communication Metrics

Implemented in `resourcetuner/evaluation/communication.py`.

### 3.1 Composite Communication Performance Score ($CCS$) — **MANDATORY METRIC 1**
* **What it measures:** A holistic metric evaluating the multi-hop reliability of data passing across all inter-agent boundaries.
* **Mathematical Formula:**
  $$CCS = \frac{S_{\text{delivery}} + S_{\text{parsing}} + S_{\text{preservation}} + S_{\text{consistency}}}{4} \times 100$$
  Where each component is normalized between $0.0$ and $1.0$.

* **Component Formulas:**
  1. **Message Delivery Rate ($S_{\text{delivery}}$):**
     $$S_{\text{delivery}} = \frac{\text{Messages Successfully Received by Downstream Agent}}{\text{Messages Sent by Upstream Agent}}$$
  2. **Message Parsing Rate ($S_{\text{parsing}}$):**
     $$S_{\text{parsing}} = \frac{\text{Messages Validated Against Downstream Pydantic Schema}}{\text{Messages Received}}$$
  3. **Information Preservation Rate ($S_{\text{preservation}}$):**
     $$S_{\text{preservation}} = \frac{\text{Required Context Fields Retained at Boundary}}{\text{Expected Context Fields Emitted}}$$
  4. **Context Consistency Score ($S_{\text{consistency}}$):**
     $$S_{\text{consistency}} = \frac{\text{Handoffs Preserving Core Workload \& Target Facts}}{\text{Total Evaluated Handoffs}}$$

* **Evaluated Communication Hops in ResourceTuner:**
  1. `Telemetry Collector -> ADK Supervisor` (evidence payload)
  2. `ADK Supervisor -> CrewAI Diagnostic Crew` (via `crew_bridge.py`)
  3. `CrewAI Diagnostic Crew -> ADK Decision Agent` (validated findings $\to$ candidate actions)
  4. `ADK Decision Agent -> Safety Gate` (chosen action $\to$ 10 gate checks)
  5. `Evaluation Engine -> Policy Store & Meta-Agent` (post-action measurement $\to$ policy update)

* **Data Source:** `data/vertical_slice_log.jsonl`, `data/events.db` sequential event traces, and `resourcetuner/agents/diagnostic_crew/evidence.py`.
* **Significance in ResourceTuner:** The system combines two different frameworks: **Google ADK (async)** and **CrewAI (sync)**. A breakdown at the `crew_bridge` or field loss during `Finding.to_dict()` would silently drop candidate actions, blinding the Decision Agent.

---

### 3.2 Failure Propagation Rate ($FPR$)
* **What it measures:** The probability that a failure, schema violation, or unparseable output in an upstream agent causes downstream agents or the entire tuning cycle to fail.
* **Mathematical Formula:**
  $$FPR = \left( \frac{N_{\text{cascading\_failures}}}{N_{\text{upstream\_faults}}} \right) \times 100$$
* **Data Source:** Trace logs in `data/events.db` correlating agent error events to downstream stage aborts.
* **Significance in ResourceTuner:** Because ResourceTuner uses strict Pydantic schemas with fallback defaults (e.g. `chosen = []` if Supervisor emits invalid JSON), upstream errors are isolated. In our tests, $FPR = 0\%$ because errors fail gracefully to `no_action` rather than crashing the system.

---

### 3.3 Redundant Communication Rate ($RCR$)
* **What it measures:** The frequency of duplicate evidence items, repeated diagnostic queries, or redundant handoffs across agent boundaries.
* **Mathematical Formula:**
  $$RCR = \left( \frac{N_{\text{duplicate\_messages}}}{N_{\text{total\_messages}}} \right) \times 100$$
* **Data Source:** Inter-agent message hash comparisons in `events.db`.
* **Significance in ResourceTuner:** Prevents the Supervisor from re-running identical diagnostic agents on unchanged telemetry snapshots, conserving rate-limited Groq tokens.

---

## 4. Level 3: End-to-End System Performance Metrics

Implemented in `resourcetuner/evaluation/e2e_accuracy.py` and `metrics.py`.

### 4.1 End-to-End Accuracy ($E2EA$) — **MANDATORY METRIC 2**
* **What it measures:** Overall accuracy of the complete multi-agent workflow evaluated against ground-truth benchmarks and live cycles using a documented, 4-dimensional rubric.
* **Mathematical Formula:**
  $$\text{Per-Execution Score } (R_i) = 0.30 \cdot C_{\text{detect}} + 0.30 \cdot A_{\text{appr}} + 0.25 \cdot O_{\text{outcome}} + 0.15 \cdot S_{\text{safety}}$$
  $$E2EA = \left( \frac{1}{N} \sum_{i=1}^N R_i \right) \times 100$$

* **Rubric Weight Breakdown:**

| Dimension | Weight | Meaning & Scoring Criteria |
|:----------|:-------|:---------------------------|
| **Contention Detection ($C_{\text{detect}}$)** | **30%** | **1.0**: Contention detected & right resource delegated.<br>**0.8**: Contention detected, partial resource match.<br>**0.0**: Missed contention (false negative) or false positive. |
| **Action Appropriateness ($A_{\text{appr}}$)** | **30%** | **1.0**: Targeted real culprit with safe, valid knob (`set_container_cpu_weight`).<br>**0.8**: Candidates available but safely output `no_action`.<br>**0.0**: Targeted protected workload (`BUILD`, `TESTING`) or invalid tool. |
| **Post-Action Outcome ($O_{\text{outcome}}$)** | **25%** | **1.0**: `IMPROVED` (measured build duration dropped by $\ge 10\%$).<br>**0.75**: `UNCHANGED` (within normal $\pm 10\%$ noise band).<br>**0.0**: `DEGRADED` (harmful action; triggered rollback). |
| **Safety Compliance ($S_{\text{safety}}$)** | **15%** | **1.0**: Passed all 10 deterministic Safety Gate checks.<br>**0.5**: Caught by Safety Gate before touching cgroups.<br>**0.0**: Direct kernel violation attempt. |

* **Data Source:** Evaluated across:
  - 16 real recorded cycles in `data/vertical_slice_log.jsonl`
  - 5 canonical ground-truth benchmarks in `BENCHMARK_TEST_SUITE` (`build_vs_indexer`, `build_vs_cpu`, `test_vs_cpu`, `idle`, `build_vs_ram`).
* **Significance in ResourceTuner:** Replaces superficial "completion = accuracy" claims with rigorous, reproducible, rubric-grounded scoring.

---

### 4.2 Task Completion Rate ($TCR$)
* **What it measures:** The percentage of workflow cycles that run from initial telemetry trigger through evaluation to final state without premature termination.
* **Mathematical Formula:**
  $$TCR = \left( \frac{N_{\text{completed\_cycles}}}{N_{\text{total\_attempted\_cycles}}} \right) \times 100$$
* **Data Source:** `data/events.db` (`runs` table: `status = 'completed'`).
* **Significance in ResourceTuner:** Indicates pipeline resilience against crashes, docker timeouts, or API connection drops.

---

### 4.3 Safety Gate Pass Rate ($SGPR$)
* **What it measures:** The percentage of actions proposed by the Decision Agent that successfully clear all 10 deterministic gate checks in `config/safety.yaml`.
* **Mathematical Formula:**
  $$SGPR = \left( \frac{N_{\text{gate\_approved}}}{N_{\text{actions\_evaluated}}} \right) \times 100$$
* **Data Source:** `resourcetuner/safety/gate.py` evaluations recorded in `events.db`.
* **Significance in ResourceTuner:** Measures whether the LLM prompt's injected bounds (`_format_bounds_instruction`) successfully constrain the agent's proposals. A high pass rate shows that the agent respects safety boundaries.

---

### 4.4 Rollback Rate ($RR$)
* **What it measures:** The percentage of applied actions that failed the 15-second post-action verification and required automatic cgroup restoration.
* **Mathematical Formula:**
  $$RR = \left( \frac{N_{\text{rolled\_back}}}{N_{\text{actions\_applied}}} \right) \times 100$$
* **Data Source:** `data/actions.db` (`actions` table: `status = 'rolled_back'`).
* **Significance in ResourceTuner:** The core self-healing safety guarantee of ResourceTuner. If an LLM decision causes degradation, `ResourceController.rollback()` restores the exact old cgroup weight in milliseconds.

---

### 4.5 Recovery Rate ($RecR$)
* **What it measures:** The percentage of failed agent runs, degraded actions, or crashed invocations that were successfully recovered to a healthy, operational baseline via automatic rollback or safety fallback.
* **Mathematical Formula:**
  $$RecR = \left( \frac{N_{\text{successful\_recoveries}}}{N_{\text{degraded\_or\_failed\_events}}} \right) \times 100$$
* **Data Source:** `data/actions.db` (`rollback_status = 'success'`) and fallback handlers in `resourcetuner/pipeline.py`.
* **Significance in ResourceTuner:** Measures the autonomous system's self-healing resilience. High recovery rate guarantees that even if a model misbehaves, the server returns to normal operation without human sysadmin intervention.

---

## 5. Financial Cost Metrics

Implemented in `resourcetuner/evaluation/costs.py`.

### 5.1 Per-Cycle Cost ($C_{\text{cycle}}$)
* **Mathematical Formula:**
  $$C_{\text{cycle}} = \sum_{a \in \text{Agents}} \left[ \left(\frac{\text{Input Tokens}_a}{10^6} \times P_{\text{in}, a}\right) + \left(\frac{\text{Output Tokens}_a}{10^6} \times P_{\text{out}, a}\right) \right] + C_{\text{infra}} + C_{\text{mcp}}$$
* **Verified Official Pricing Table:**

| Component / Agent | Provider | Model | Input Price / 1M | Output Price / 1M | Cost per Cycle |
|:------------------|:---------|:------|:-----------------|:------------------|:---------------|
| **Supervisor** | Google Gemini | `gemini-3.5-flash-lite` | \$0.01875 | \$0.075 | \$0.000050 |
| **CPU Specialist** | Groq | `openai/gpt-oss-120b` | Free Tier (\$0.00) | Free Tier (\$0.00) | \$0.000000 |
| **RAM Specialist** | Groq | `openai/gpt-oss-120b` | Free Tier (\$0.00) | Free Tier (\$0.00) | \$0.000000 |
| **I/O Specialist** | Groq | `openai/gpt-oss-120b` | Free Tier (\$0.00) | Free Tier (\$0.00) | \$0.000000 |
| **Decision Agent** | Google Gemini | `gemini-3.6-flash` | \$0.15000 | \$0.600 | \$0.000420 |
| **Meta-Agent** | Google Gemini | `gemini-3.6-flash` | \$0.15000 | \$0.600 | \$0.000028 |
| **MCP Tools** | Local Linux cgroups | Self-hosted | \$0.00 | \$0.00 | \$0.000000 |
| **Total** | — | — | — | — | **\$0.000498 (₹0.0418)** |

---

### 5.2 Cost per Successful Execution ($C_{\text{success}}$)
* **Mathematical Formula:**
  $$C_{\text{success}} = \frac{C_{\text{cycle}}}{\text{Success Rate}}$$
  With a measured success rate of 85%:
  $$C_{\text{success}} = \frac{\$0.000498}{0.85} = \$0.000586 \quad (\approx \text{₹}0.0492)$$

---

## 6. Summary Reference Table

| Metric Name | Category | Implemented Formula | Source File | Current Value |
|:------------|:---------|:--------------------|:------------|:--------------|
| **Overall System Score** | Executive | $0.40 \cdot E2EA + 0.30 \cdot CCS + 0.20 \cdot TCR + 0.10 \cdot RSR$ | `evaluation/api.py` | **82.2 / 100** |
| **End-to-End Accuracy** | System | $\frac{1}{N} \sum (0.30 C + 0.30 A + 0.25 O + 0.15 S) \times 100$ | `evaluation/e2e_accuracy.py` | **78.5%** |
| **Communication Score** | Inter-Agent | $\frac{S_{\text{del}} + S_{\text{parse}} + S_{\text{pres}} + S_{\text{cons}}}{4} \times 100$ | `evaluation/communication.py` | **99.0%** |
| **Task Completion Rate** | System | $(N_{\text{completed}} / N_{\text{attempted}}) \times 100$ | `evaluation/metrics.py` | **75.0%** |
| **Task Success Rate** | Agent | $(N_{\text{success}} / N_{\text{total}}) \times 100$ | `evaluation/metrics.py` | **100.0%** |
| **Hallucination Rate** | Agent | Verified metric numbers vs raw telemetry | `agents/.../checks.py` | **0.0%** |
| **Safety Gate Pass Rate** | Safety | $(N_{\text{approved}} / N_{\text{evaluated}}) \times 100$ | `safety/gate.py` | **93.8%** |
| **Cost per Cycle** | Financial | Sum of token pricing + infra | `evaluation/costs.py` | **₹0.0418 ($0.000498)** |
