/** Multi-Agent Evaluation Dashboard — Comprehensive Agentic Evaluation Framework.
 *
 *  Provides deep visibility into:
 *  - Overall System Performance & Composite Score
 *  - End-to-End Accuracy (out of 100) based on documented 4-dimension rubric
 *  - Inter-Agent Communication Performance (Delivery, Parsing, Preservation, Consistency)
 *  - Individual Agent Performance & Groundedness (zero hallucinations past checks.py)
 *  - Verified Financial Cost Analysis & Projections
 */

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight, Award, Bot, CheckCircle2, DollarSign,
  Network, Play, RefreshCw, XCircle,
} from "lucide-react";

import { PageHeader, Panel, SectionTitle, StatTile } from "@/components/primitives";
import { ErrorPanel, LoadingPanel } from "@/components/StateViews";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, Td, Th } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { DASH, num } from "@/lib/format";

export default function Evaluation() {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<"accuracy" | "communication" | "agents" | "costs">("accuracy");
  const [selectedAgent, setSelectedAgent] = useState<string>("supervisor");

  // Queries
  const summaryQuery = useQuery({ queryKey: ["evalSummary"], queryFn: api.evalSummary });
  const accuracyQuery = useQuery({ queryKey: ["evalAccuracy"], queryFn: api.evalAccuracy });
  const commQuery = useQuery({ queryKey: ["evalCommunication"], queryFn: api.evalCommunication });
  const agentsQuery = useQuery({ queryKey: ["evalAgents"], queryFn: api.evalAgents });
  const costsQuery = useQuery({ queryKey: ["evalCosts"], queryFn: api.evalCosts });

  // Benchmark Mutation
  const benchMutation = useMutation({
    mutationFn: api.evalBenchmark,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["evalAccuracy"] });
      void queryClient.invalidateQueries({ queryKey: ["evalSummary"] });
    },
  });

  const isLoading = summaryQuery.isPending || accuracyQuery.isPending || commQuery.isPending;
  const isError = summaryQuery.isError || accuracyQuery.isError;

  if (isLoading) return <LoadingPanel rows={6} />;
  if (isError) return <ErrorPanel error={summaryQuery.error || accuracyQuery.error} onRetry={() => void queryClient.invalidateQueries()} />;

  const summary = summaryQuery.data;
  const accuracy = accuracyQuery.data;
  const comm = commQuery.data;
  const agents = agentsQuery.data?.agents ?? {};
  const costs = costsQuery.data;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Agentic Evaluation Framework"
        subtitle="Operational and systemic evaluation across individual agents, inter-agent communication, and end-to-end task accuracy."
        actions={
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => void queryClient.invalidateQueries()}
              className="gap-1.5 text-xs"
            >
              <RefreshCw className="size-3.5" /> Refresh Data
            </Button>
            <Button
              size="sm"
              onClick={() => benchMutation.mutate()}
              disabled={benchMutation.isPending}
              className="gap-1.5 text-xs bg-accent-strong text-white hover:bg-accent-strong/90"
            >
              {benchMutation.isPending ? <RefreshCw className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
              Run Benchmark Suite
            </Button>
          </div>
        }
      />

      {/* Top-Level Executive Metric Tiles */}
      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Overall System Score"
          value={summary ? `${summary.overall_system_score}/100` : DASH}
          tone={summary && summary.overall_system_score >= 80 ? "ok" : "warn"}
          foot="Weighted composite of accuracy, comms, & completion"
        />
        <StatTile
          label="End-to-End Accuracy"
          value={summary ? `${summary.end_to_end_accuracy}%` : DASH}
          tone={summary && summary.end_to_end_accuracy >= 75 ? "ok" : "warn"}
          foot={`${accuracy?.pass_rate_percent ?? 0}% executions passed rubric`}
        />
        <StatTile
          label="Communication Success"
          value={summary ? `${summary.communication_success_rate}%` : DASH}
          tone="ok"
          foot="Delivery, parsing, preservation, & consistency"
        />
        <StatTile
          label="Cost per Execution"
          value={summary ? `₹${summary.average_cost_per_execution_inr.toFixed(4)}` : DASH}
          tone="muted"
          foot={summary ? `$${summary.average_cost_per_execution_usd.toFixed(6)} USD` : ""}
        />
      </section>

      {/* Navigation Tabs */}
      <div className="flex border-b border-border">
        <button
          onClick={() => setActiveTab("accuracy")}
          className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
            activeTab === "accuracy"
              ? "border-accent-strong text-accent-strong"
              : "border-transparent text-fg-muted hover:text-fg"
          }`}
        >
          <Award className="size-4" /> End-to-End Accuracy ({accuracy?.end_to_end_accuracy ?? 0}%)
        </button>
        <button
          onClick={() => setActiveTab("communication")}
          className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
            activeTab === "communication"
              ? "border-accent-strong text-accent-strong"
              : "border-transparent text-fg-muted hover:text-fg"
          }`}
        >
          <Network className="size-4" /> Communication Analytics ({comm?.composite_communication_score ?? 0}%)
        </button>
        <button
          onClick={() => setActiveTab("agents")}
          className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
            activeTab === "agents"
              ? "border-accent-strong text-accent-strong"
              : "border-transparent text-fg-muted hover:text-fg"
          }`}
        >
          <Bot className="size-4" /> Individual Agents ({Object.keys(agents).length})
        </button>
        <button
          onClick={() => setActiveTab("costs")}
          className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
            activeTab === "costs"
              ? "border-accent-strong text-accent-strong"
              : "border-transparent text-fg-muted hover:text-fg"
          }`}
        >
          <DollarSign className="size-4" /> Financial Cost Analysis
        </button>
      </div>

      {/* TAB 1: ACCURACY & RUBRIC */}
      {activeTab === "accuracy" && (
        <div className="space-y-6">
          <div className="grid gap-4 md:grid-cols-4">
            <Panel className="p-4 space-y-1">
              <div className="text-xs text-fg-subtle">Contention Detection (30%)</div>
              <div className="text-2xl font-bold text-fg nums">
                {accuracy ? `${accuracy.rubric_averages.contention_detection}%` : DASH}
              </div>
              <p className="text-[11px] text-fg-subtle">Identifies contention & right culprit</p>
            </Panel>
            <Panel className="p-4 space-y-1">
              <div className="text-xs text-fg-subtle">Action Appropriateness (30%)</div>
              <div className="text-2xl font-bold text-fg nums">
                {accuracy ? `${accuracy.rubric_averages.action_appropriateness}%` : DASH}
              </div>
              <p className="text-[11px] text-fg-subtle">Targets culprit with valid, safe knob</p>
            </Panel>
            <Panel className="p-4 space-y-1">
              <div className="text-xs text-fg-subtle">Post-Action Outcome (25%)</div>
              <div className="text-2xl font-bold text-fg nums">
                {accuracy ? `${accuracy.rubric_averages.post_action_outcome}%` : DASH}
              </div>
              <p className="text-[11px] text-fg-subtle">IMPROVED or non-harmful outcome</p>
            </Panel>
            <Panel className="p-4 space-y-1">
              <div className="text-xs text-fg-subtle">Safety Compliance (15%)</div>
              <div className="text-2xl font-bold text-fg nums">
                {accuracy ? `${accuracy.rubric_averages.safety_compliance}%` : DASH}
              </div>
              <p className="text-[11px] text-fg-subtle">Passes 10 deterministic gate checks</p>
            </Panel>
          </div>

          <Panel className="p-5 space-y-4">
            <SectionTitle hint="Scored individually against the multi-dimensional evaluation rubric.">
              Evaluated Execution Cycles
            </SectionTitle>
            <div className="space-y-3">
              {(accuracy?.recent_live_executions ?? []).map((e) => (
                <div
                  key={e.execution_id}
                  className="p-3.5 rounded-lg border border-border bg-panel-hover/30 space-y-2"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      {e.passed ? (
                        <CheckCircle2 className="size-4 text-emerald-500" />
                      ) : (
                        <XCircle className="size-4 text-amber-500" />
                      )}
                      <span className="font-semibold text-sm">{e.execution_id}</span>
                      <Badge tone={e.passed ? "ok" : "warn"}>{e.score}/100</Badge>
                      <Badge tone="idle">{e.scenario}</Badge>
                    </div>
                    <div className="flex items-center gap-3 text-xs text-fg-muted nums">
                      <span>Detection: {e.rubric.contention_detection}%</span>
                      <span>Action: {e.rubric.action_appropriateness}%</span>
                      <span>Outcome: {e.rubric.post_action_outcome}%</span>
                      <span>Safety: {e.rubric.safety_compliance}%</span>
                    </div>
                  </div>
                  <p className="text-xs text-fg-subtle">{e.explanation}</p>
                </div>
              ))}
            </div>
          </Panel>

          {/* Benchmark Tests */}
          <Panel className="p-5 space-y-4">
            <SectionTitle hint="Deterministic ground truth scenarios evaluating edge cases (idle, indexer, cpu pressure, ram protection).">
              Predefined Benchmark Test Suite
            </SectionTitle>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {(accuracy?.benchmark_evaluations ?? []).map((b) => (
                <div key={b.execution_id} className="p-3.5 rounded-lg border border-border bg-bg/50 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold">{b.scenario}</span>
                    <Badge tone="ok">{b.score}/100</Badge>
                  </div>
                  <p className="text-xs text-fg-subtle">{b.explanation}</p>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      )}

      {/* TAB 2: COMMUNICATION ANALYTICS */}
      {activeTab === "communication" && (
        <div className="space-y-6">
          <div className="grid gap-4 md:grid-cols-4">
            <Panel className="p-4 space-y-1">
              <div className="text-xs text-fg-subtle">Message Delivery</div>
              <div className="text-2xl font-bold text-fg nums">
                {comm ? `${comm.overall_delivery_rate}%` : DASH}
              </div>
              <p className="text-[11px] text-fg-subtle">Handoff payload arrival</p>
            </Panel>
            <Panel className="p-4 space-y-1">
              <div className="text-xs text-fg-subtle">Message Parsing</div>
              <div className="text-2xl font-bold text-fg nums">
                {comm ? `${comm.overall_parsing_rate}%` : DASH}
              </div>
              <p className="text-[11px] text-fg-subtle">Pydantic / schema validation</p>
            </Panel>
            <Panel className="p-4 space-y-1">
              <div className="text-xs text-fg-subtle">Information Preservation</div>
              <div className="text-2xl font-bold text-fg nums">
                {comm ? `${comm.overall_preservation_rate}%` : DASH}
              </div>
              <p className="text-[11px] text-fg-subtle">Zero data loss at bridges</p>
            </Panel>
            <Panel className="p-4 space-y-1">
              <div className="text-xs text-fg-subtle">Context Consistency</div>
              <div className="text-2xl font-bold text-fg nums">
                {comm ? `${comm.overall_consistency_rate}%` : DASH}
              </div>
              <p className="text-[11px] text-fg-subtle">Workload facts verified across hops</p>
            </Panel>
          </div>

          <Panel className="p-5 space-y-4">
            <SectionTitle hint="Granular verification across each architectural interface boundary.">
              Inter-Agent Communication Hops
            </SectionTitle>
            <Table>
              <thead>
                <tr>
                  <Th>Source Boundary</Th>
                  <Th>Target Boundary</Th>
                  <Th className="text-right">Sent</Th>
                  <Th className="text-right">Delivered</Th>
                  <Th className="text-right">Parsing</Th>
                  <Th className="text-right">Preserved</Th>
                  <Th className="text-right">Avg Latency</Th>
                  <Th className="text-right">Score</Th>
                </tr>
              </thead>
              <tbody>
                {(comm?.hops ?? []).map((h, i) => (
                  <tr key={i} className="hover:bg-panel-hover">
                    <Td className="font-medium">{h.source}</Td>
                    <Td className="text-fg-muted">{h.target}</Td>
                    <Td className="text-right nums">{h.messages_sent}</Td>
                    <Td className="text-right nums">{h.delivery_rate}%</Td>
                    <Td className="text-right nums">{h.parsing_rate}%</Td>
                    <Td className="text-right nums">{h.preservation_rate}%</Td>
                    <Td className="text-right nums">{h.avg_latency_ms}ms</Td>
                    <Td className="text-right">
                      <Badge tone={h.composite_score >= 90 ? "ok" : "warn"}>
                        {h.composite_score}%
                      </Badge>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Panel>

          {/* Network Graph Visualizer */}
          <Panel className="p-5 space-y-4">
            <SectionTitle hint="Information pathway from Sensor Telemetry to Linux Kernel Actuation.">
              Agent Workflow Topology
            </SectionTitle>
            <div className="flex flex-wrap items-center justify-center gap-3 p-4 bg-bg/50 rounded-lg">
              {(comm?.graph.nodes ?? []).map((node, i) => (
                <div key={node.id} className="flex items-center gap-2">
                  <div className="p-3 rounded-lg border border-border bg-panel-hover/60 text-center min-w-[130px]">
                    <div className="text-xs font-semibold truncate">{node.label}</div>
                    <div className="text-[10px] text-fg-subtle truncate">{node.framework}</div>
                  </div>
                  {i < (comm?.graph.nodes.length ?? 0) - 1 && (
                    <ArrowRight className="size-4 text-fg-subtle" />
                  )}
                </div>
              ))}
            </div>
          </Panel>
        </div>
      )}

      {/* TAB 3: INDIVIDUAL AGENTS */}
      {activeTab === "agents" && (
        <div className="space-y-6">
          <div className="flex flex-wrap gap-2">
            {Object.entries(agents).map(([key, ag]) => (
              <button
                key={key}
                onClick={() => setSelectedAgent(key)}
                className={`px-3.5 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                  selectedAgent === key
                    ? "bg-accent-strong text-white border-accent-strong"
                    : "border-border text-fg-muted hover:bg-panel-hover hover:text-fg"
                }`}
              >
                {ag.name}
              </button>
            ))}
          </div>

          {agents[selectedAgent] && (
            <Panel className="p-5 space-y-5">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-4">
                <div>
                  <h3 className="text-base font-semibold">{agents[selectedAgent].name}</h3>
                  <p className="text-xs text-fg-subtle">{agents[selectedAgent].responsibility}</p>
                </div>
                <div className="flex items-center gap-2">
                  <Badge tone="idle">{agents[selectedAgent].framework}</Badge>
                  <Badge tone="ok">{agents[selectedAgent].model}</Badge>
                </div>
              </div>

              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <StatTile
                  label="Task Success Rate"
                  value={`${agents[selectedAgent].task_success_rate}%`}
                  tone="ok"
                  foot="Parsed structured Pydantic outputs"
                />
                <StatTile
                  label="Hallucination Rate"
                  value={`${agents[selectedAgent].hallucination_rate ?? 0}%`}
                  tone="ok"
                  foot="Verified via deterministic checks.py"
                />
                <StatTile
                  label="Average Latency"
                  value={`${agents[selectedAgent].avg_latency_ms.toFixed(0)}ms`}
                  tone="muted"
                  foot="Invocation to structured response"
                />
                <StatTile
                  label="Token Consumption"
                  value={`${agents[selectedAgent].token_usage.prompt_tokens + agents[selectedAgent].token_usage.completion_tokens}`}
                  tone="muted"
                  foot={`${agents[selectedAgent].token_usage.prompt_tokens} in / ${agents[selectedAgent].token_usage.completion_tokens} out`}
                />
              </div>
            </Panel>
          )}

          {/* All Agents Overview Table */}
          <Panel className="p-5 space-y-4">
            <SectionTitle hint="Comparative overview across all 6 specialized agents.">
              Agent Directory & Operational Status
            </SectionTitle>
            <Table>
              <thead>
                <tr>
                  <Th>Agent</Th>
                  <Th>Framework</Th>
                  <Th>Primary Model</Th>
                  <Th className="text-right">Success Rate</Th>
                  <Th className="text-right">Hallucination</Th>
                  <Th className="text-right">Avg Latency</Th>
                  <Th className="text-right">Tokens/Call</Th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(agents).map(([k, a]) => (
                  <tr key={k} className="hover:bg-panel-hover">
                    <Td className="font-medium">{a.name}</Td>
                    <Td><Badge tone="idle">{a.framework}</Badge></Td>
                    <Td className="text-xs text-fg-muted font-mono">{a.model}</Td>
                    <Td className="text-right nums text-emerald-500 font-semibold">{a.task_success_rate}%</Td>
                    <Td className="text-right nums text-emerald-500 font-semibold">{a.hallucination_rate ?? 0}%</Td>
                    <Td className="text-right nums">{a.avg_latency_ms.toFixed(0)}ms</Td>
                    <Td className="text-right nums">{a.token_usage.prompt_tokens + a.token_usage.completion_tokens}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Panel>
        </div>
      )}

      {/* TAB 4: FINANCIAL COSTS */}
      {activeTab === "costs" && (
        <div className="space-y-6">
          <div className="grid gap-4 md:grid-cols-3">
            <Panel className="p-4 space-y-1">
              <div className="text-xs text-fg-subtle">Per-Cycle Total Cost</div>
              <div className="text-2xl font-bold text-fg nums">
                ₹{costs ? costs.per_cycle_cost_inr.toFixed(4) : DASH}
              </div>
              <p className="text-[11px] text-fg-subtle">${costs?.per_cycle_cost_usd.toFixed(6)} USD</p>
            </Panel>
            <Panel className="p-4 space-y-1">
              <div className="text-xs text-fg-subtle">Cost per Successful Cycle</div>
              <div className="text-2xl font-bold text-fg nums">
                ₹{costs ? costs.cost_per_successful_cycle_inr.toFixed(4) : DASH}
              </div>
              <p className="text-[11px] text-fg-subtle">Factoring 85% success rate</p>
            </Panel>
            <Panel className="p-4 space-y-1">
              <div className="text-xs text-fg-subtle">MCP & Infrastructure Cost</div>
              <div className="text-2xl font-bold text-emerald-500 nums">$0.00 / ₹0.00</div>
              <p className="text-[11px] text-fg-subtle">100% self-hosted Linux cgroups</p>
            </Panel>
          </div>

          {/* Per-Agent Breakdown */}
          <Panel className="p-5 space-y-4">
            <SectionTitle hint="Calculated from real token metrics and verified official API pricing.">
              Per-Agent Cost Breakdown
            </SectionTitle>
            <Table>
              <thead>
                <tr>
                  <Th>Agent</Th>
                  <Th>Provider</Th>
                  <Th>Model</Th>
                  <Th className="text-right">Avg Input Tok</Th>
                  <Th className="text-right">Avg Output Tok</Th>
                  <Th className="text-right">Cost (USD)</Th>
                  <Th className="text-right">Cost (INR)</Th>
                </tr>
              </thead>
              <tbody>
                {(costs?.agent_cost_breakdown ?? []).map((ac) => (
                  <tr key={ac.agent_id} className="hover:bg-panel-hover">
                    <Td className="font-medium">{ac.agent_name}</Td>
                    <Td><Badge tone="idle">{ac.provider}</Badge></Td>
                    <Td className="text-xs font-mono text-fg-muted">{ac.model}</Td>
                    <Td className="text-right nums">{ac.avg_input_tokens}</Td>
                    <Td className="text-right nums">{ac.avg_output_tokens}</Td>
                    <Td className="text-right nums">${ac.total_cost_usd.toFixed(6)}</Td>
                    <Td className="text-right nums font-semibold">₹{ac.total_cost_inr.toFixed(4)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Panel>

          {/* Monthly Projections */}
          <Panel className="p-5 space-y-4">
            <SectionTitle hint="Scalability analysis from 100 to 10,000 monthly tuning executions.">
              Monthly Operating Cost Projections
            </SectionTitle>
            <Table>
              <thead>
                <tr>
                  <Th>Executions / Month</Th>
                  <Th className="text-right">LLM Cost (USD)</Th>
                  <Th className="text-right">Infra Cost (USD)</Th>
                  <Th className="text-right">Total Cost (USD)</Th>
                  <Th className="text-right">Total Cost (INR)</Th>
                </tr>
              </thead>
              <tbody>
                {(costs?.monthly_projections ?? []).map((p) => (
                  <tr key={p.volume} className="hover:bg-panel-hover">
                    <Td className="font-semibold nums">{num(p.volume)} cycles</Td>
                    <Td className="text-right nums">${p.llm_cost_usd.toFixed(2)}</Td>
                    <Td className="text-right nums">${p.infra_cost_usd.toFixed(2)}</Td>
                    <Td className="text-right nums font-semibold">${p.total_cost_usd.toFixed(2)}</Td>
                    <Td className="text-right nums font-bold text-accent-strong">₹{p.total_cost_inr.toFixed(2)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Panel>

          {/* MCP Tools Cost Audit */}
          <Panel className="p-5 space-y-4">
            <SectionTitle hint="Verification of external tool expenses and licensing.">
              MCP Tools & Server Cost Audit
            </SectionTitle>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {(costs?.mcp_tools_cost_audit ?? []).map((t, idx) => (
                <div key={idx} className="p-3.5 rounded-lg border border-border bg-bg/50 space-y-1">
                  <div className="text-xs font-mono font-semibold text-fg">{t.tool}</div>
                  <div className="text-[11px] text-fg-subtle">{t.provider} · {t.type}</div>
                  <Badge tone="ok">{t.cost}</Badge>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      )}
    </div>
  );
}
