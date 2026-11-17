/** Hook that constructs the active agent workflow model from real pipeline events.
 *
 *  Extracts real-time statuses, inputs, outputs, processing details, and A2A lifecycles
 *  from the actual StageEvent payloads emitted by resourcetuner.pipeline.slice.
 */

import { useMemo } from "react";
import type { Edge, Node } from "@xyflow/react";
import type { Run, StageEvent, StageStatus } from "@/lib/types";
import type { WorkflowNodeData, WorkflowTimelineEvent } from "./workflowTypes";
import { clockTime } from "@/lib/format";

interface UseWorkflowModelOptions {
  run: Run | null;
  cycle: number | null;
  events: StageEvent[];
  selectedNodeId: string | null;
}

export function useWorkflowModel({
  run,
  cycle,
  events,
  selectedNodeId,
}: UseWorkflowModelOptions) {
  return useMemo(() => {
    // Filter events for the relevant cycle (or run-level if cycle is null)
    const cycleEvents = events.filter((e) => cycle === null || e.cycle === cycle || e.cycle === null);

    // Map each stage to its latest event
    const stageEventMap = new Map<string, StageEvent>();
    for (const ev of cycleEvents) {
      stageEventMap.set(ev.stage, ev);
    }

    const getStageStatus = (stage: string): StageStatus => {
      const ev = stageEventMap.get(stage);
      if (!ev) return run?.active ? "waiting" : "waiting";
      return ev.status;
    };

    const getStageDuration = (stage: string): number | null => {
      const ev = stageEventMap.get(stage);
      return ev?.duration_ms ?? null;
    };

    // Extract real payloads
    const triggerEv = stageEventMap.get("trigger");
    const classifyEv = stageEventMap.get("classify");
    const supervisorEv = stageEventMap.get("supervisor");
    const diagnosticsEv = stageEventMap.get("diagnostics");
    const decisionEv = stageEventMap.get("decision");
    const gateEv = stageEventMap.get("safety_gate");
    const controllerEv = stageEventMap.get("controller");
    const evalEv = stageEventMap.get("evaluation");
    const policyEv = stageEventMap.get("policy");
    const metaEv = stageEventMap.get("meta");

    // Extract diagnostic specialists
    const findings = Array.isArray(diagnosticsEv?.payload?.findings)
      ? (diagnosticsEv?.payload?.findings as Array<Record<string, unknown>>)
      : [];

    const cpuFinding = findings.find((f) => String(f?.agent).includes("cpu"));
    const ramFinding = findings.find((f) => String(f?.agent).includes("ram"));
    const ioFinding = findings.find((f) => String(f?.agent).includes("io"));

    // Supervisor delegation
    const delegation = supervisorEv?.payload?.delegation as Record<string, unknown> | undefined;
    const delegatedResources: string[] = Array.isArray(delegation?.resources)
      ? (delegation?.resources as string[])
      : [];

    const isCpuDelegated = delegatedResources.length === 0 || delegatedResources.includes("cpu");
    const isRamDelegated = delegatedResources.length === 0 || delegatedResources.includes("ram");
    const isIoDelegated = delegatedResources.length === 0 || delegatedResources.includes("io");

    const diagOverallStatus = getStageStatus("diagnostics");

    const getSpecialistStatus = (finding: Record<string, unknown> | undefined, isDelegated: boolean): StageStatus => {
      if (!isDelegated) return "skipped";
      if (!finding) {
        if (diagOverallStatus === "running") return "running";
        if (diagOverallStatus === "completed") return "completed";
        return diagOverallStatus;
      }
      return diagOverallStatus === "running" ? "running" : "completed";
    };

    // Build the 12 Canonical Workflow Nodes
    const nodes: Node<WorkflowNodeData>[] = [
      // 1. Contention Trigger
      {
        id: "trigger",
        type: "workflowNode",
        position: { x: 30, y: 180 },
        data: {
          id: "trigger",
          name: "Contention Trigger",
          category: "system_input",
          framework: "Linux Kernel",
          responsibility: "Monitors /proc/pressure/cpu and triggers when CPU PSI >= 15.6% for 10s",
          status: getStageStatus("trigger"),
          durationMs: getStageDuration("trigger"),
          inputSummary: "Host Telemetry (CPU, RAM, I/O PSI deltas)",
          outputSummary: triggerEv?.summary || "Trigger event & telemetry snapshot",
          operationsSummary: "Threshold comparator & persistence filter",
          upstreamIds: [],
          downstreamIds: ["classify"],
          rawInput: triggerEv?.payload?.threshold ? { threshold: triggerEv.payload.threshold, metric: triggerEv.payload.metric } : undefined,
          rawOutput: triggerEv?.payload,
          isSelected: selectedNodeId === "trigger",
          isExecuting: getStageStatus("trigger") === "running",
        },
      },
      // 2. Workload Classifier
      {
        id: "classify",
        type: "workflowNode",
        position: { x: 270, y: 180 },
        data: {
          id: "classify",
          name: "Workload Classifier",
          category: "system_classifier",
          framework: "Deterministic",
          responsibility: "Identifies active foreground workload (BUILD, TESTING, etc.) via heuristic rules",
          status: getStageStatus("classify"),
          durationMs: getStageDuration("classify"),
          inputSummary: "Process tree, cmdline signatures & container telemetry",
          outputSummary: classifyEv?.summary || "Classified workload + confidence score",
          operationsSummary: "Signature matching, inheritance & dominance rule engine",
          upstreamIds: ["trigger"],
          downstreamIds: ["supervisor"],
          rawInput: classifyEv?.payload?.host,
          rawOutput: classifyEv?.payload?.workload,
          isSelected: selectedNodeId === "classify",
          isExecuting: getStageStatus("classify") === "running",
        },
      },
      // 3. Supervisor Agent (ADK)
      {
        id: "supervisor",
        type: "workflowNode",
        position: { x: 520, y: 180 },
        data: {
          id: "supervisor",
          name: "Supervisor Agent",
          category: "agent_supervisor",
          framework: "Google ADK",
          model: "gemini-3.5-flash-lite",
          responsibility: "High-level orchestration: correlates cross-resource contention & delegates to specialists",
          status: getStageStatus("supervisor"),
          durationMs: getStageDuration("supervisor"),
          inputSummary: "Deterministic trigger summary & multi-resource evidence",
          outputSummary: supervisorEv?.summary || (delegatedResources.length ? `Delegated to: [${delegatedResources.join(", ")}]` : "Pending routing"),
          operationsSummary: "Cross-resource anomaly correlation; dispatches A2A Task to CrewAI Diagnostic Crew",
          upstreamIds: ["classify"],
          downstreamIds: ["diag_cpu", "diag_ram", "diag_io"],
          rawInput: supervisorEv?.payload,
          rawOutput: delegation,
          provenance: supervisorEv?.payload?.provenance as WorkflowNodeData["provenance"],
          a2aState: getStageStatus("supervisor") === "running" ? "SUBMITTED" : getStageStatus("supervisor") === "completed" ? "COMPLETED" : null,
          isSelected: selectedNodeId === "supervisor",
          isExecuting: getStageStatus("supervisor") === "running",
        },
      },
      // 4a. CPU Specialist (CrewAI)
      {
        id: "diag_cpu",
        type: "workflowNode",
        position: { x: 800, y: 40 },
        data: {
          id: "diag_cpu",
          name: "CPU Specialist",
          category: "agent_specialist",
          framework: "CrewAI",
          model: "groq/openai/gpt-oss-120b",
          responsibility: "Diagnoses CPU contention, pinpoints culprit containers & proposes weight/limit tuning",
          status: getSpecialistStatus(cpuFinding, isCpuDelegated),
          durationMs: getStageDuration("diagnostics"),
          inputSummary: "Projected CPU metrics & container cgroup shares",
          outputSummary: cpuFinding
            ? `Contention: ${(cpuFinding.diagnosis as Record<string, unknown>)?.contention ? "YES" : "NO"} | Culprit: ${((cpuFinding.diagnosis as Record<string, unknown>)?.culprits as string[])?.join(", ") || "none"}`
            : isCpuDelegated ? "Specialist invoked via A2A" : "Not delegated this cycle",
          operationsSummary: "Analyzes CPU saturation, throttled periods; runs deterministic checks.py",
          upstreamIds: ["supervisor"],
          downstreamIds: ["action_guard"],
          rawInput: isCpuDelegated ? { resource: "cpu", projected: true } : undefined,
          rawOutput: cpuFinding,
          checks: {
            ok: cpuFinding?.check_ok !== false,
            issues: Array.isArray(cpuFinding?.check_issues) ? (cpuFinding?.check_issues as string[]) : [],
          },
          a2aState: getSpecialistStatus(cpuFinding, isCpuDelegated) === "running" ? "WORKING" : "COMPLETED",
          isSelected: selectedNodeId === "diag_cpu",
          isExecuting: getSpecialistStatus(cpuFinding, isCpuDelegated) === "running",
        },
      },
      // 4b. RAM Specialist (CrewAI)
      {
        id: "diag_ram",
        type: "workflowNode",
        position: { x: 800, y: 180 },
        data: {
          id: "diag_ram",
          name: "RAM Specialist",
          category: "agent_specialist",
          framework: "CrewAI",
          model: "groq/openai/gpt-oss-120b",
          responsibility: "Diagnoses memory pressure & working set saturation (strictly non-mutating)",
          status: getSpecialistStatus(ramFinding, isRamDelegated),
          durationMs: getStageDuration("diagnostics"),
          inputSummary: "Projected RAM metrics, working set bytes & OOM counters",
          outputSummary: ramFinding
            ? `Contention: ${(ramFinding.diagnosis as Record<string, unknown>)?.contention ? "YES" : "NO"} | (No action permitted)`
            : isRamDelegated ? "Specialist invoked via A2A" : "Not delegated this cycle",
          operationsSummary: "Detects memory pressure; adheres to non-reversible memory constraint",
          upstreamIds: ["supervisor"],
          downstreamIds: ["action_guard"],
          rawInput: isRamDelegated ? { resource: "ram", projected: true } : undefined,
          rawOutput: ramFinding,
          checks: {
            ok: ramFinding?.check_ok !== false,
            issues: Array.isArray(ramFinding?.check_issues) ? (ramFinding?.check_issues as string[]) : [],
          },
          a2aState: getSpecialistStatus(ramFinding, isRamDelegated) === "running" ? "WORKING" : "COMPLETED",
          isSelected: selectedNodeId === "diag_ram",
          isExecuting: getSpecialistStatus(ramFinding, isRamDelegated) === "running",
        },
      },
      // 4c. I/O Specialist (CrewAI)
      {
        id: "diag_io",
        type: "workflowNode",
        position: { x: 800, y: 320 },
        data: {
          id: "diag_io",
          name: "I/O Specialist",
          category: "agent_specialist",
          framework: "CrewAI",
          model: "groq/openai/gpt-oss-120b",
          responsibility: "Diagnoses disk I/O contention & iowait spikes; proposes container pause if needed",
          status: getSpecialistStatus(ioFinding, isIoDelegated),
          durationMs: getStageDuration("diagnostics"),
          inputSummary: "Projected I/O throughput (read/write MiB/s) & io.psi",
          outputSummary: ioFinding
            ? `Contention: ${(ioFinding.diagnosis as Record<string, unknown>)?.contention ? "YES" : "NO"}`
            : isIoDelegated ? "Specialist invoked via A2A" : "Not delegated this cycle",
          operationsSummary: "Evaluates disk bottlenecks; proposes pause only if hog detected",
          upstreamIds: ["supervisor"],
          downstreamIds: ["action_guard"],
          rawInput: isIoDelegated ? { resource: "io", projected: true } : undefined,
          rawOutput: ioFinding,
          checks: {
            ok: ioFinding?.check_ok !== false,
            issues: Array.isArray(ioFinding?.check_issues) ? (ioFinding?.check_issues as string[]) : [],
          },
          a2aState: getSpecialistStatus(ioFinding, isIoDelegated) === "running" ? "WORKING" : "COMPLETED",
          isSelected: selectedNodeId === "diag_io",
          isExecuting: getSpecialistStatus(ioFinding, isIoDelegated) === "running",
        },
      },
      // 5. Action Guard & Verification
      {
        id: "action_guard",
        type: "workflowNode",
        position: { x: 1080, y: 180 },
        data: {
          id: "action_guard",
          name: "Action Guard & Verifier",
          category: "system_guard",
          framework: "Deterministic",
          responsibility: "Filters specialist findings: strips hallucinations (checks.py) and degraded model actions",
          status: getStageStatus("diagnostics") === "completed" ? "completed" : getStageStatus("diagnostics"),
          durationMs: 5,
          inputSummary: "Raw findings from CPU, RAM, and I/O specialists",
          outputSummary: "Verified candidate actions pool",
          operationsSummary: "actions_for_decision() verification pipeline",
          upstreamIds: ["diag_cpu", "diag_ram", "diag_io"],
          downstreamIds: ["decision"],
          rawInput: findings,
          isSelected: selectedNodeId === "action_guard",
          isExecuting: false,
        },
      },
      // 6. Decision Agent (ADK)
      {
        id: "decision",
        type: "workflowNode",
        position: { x: 1320, y: 180 },
        data: {
          id: "decision",
          name: "Decision Agent",
          category: "agent_decision",
          framework: "Google ADK",
          model: "gemini-3.6-flash",
          responsibility: "Action Selection: compares verified candidate actions with historical policy stats",
          status: getStageStatus("decision"),
          durationMs: getStageDuration("decision"),
          inputSummary: "Candidate actions + PolicyStore historical statistics",
          outputSummary: decisionEv?.summary || "Pending decision",
          operationsSummary: "Risk vs reward balancing; outputs at most ONE ProposedAction or no_action",
          upstreamIds: ["action_guard"],
          downstreamIds: ["safety_gate"],
          rawInput: decisionEv?.payload,
          rawOutput: decisionEv?.payload?.chosen_action,
          isSelected: selectedNodeId === "decision",
          isExecuting: getStageStatus("decision") === "running",
        },
      },
      // 7. Safety Gate (10-Check Deterministic)
      {
        id: "safety_gate",
        type: "workflowNode",
        position: { x: 1580, y: 180 },
        data: {
          id: "safety_gate",
          name: "Safety Gate",
          category: "system_gate",
          framework: "Deterministic",
          responsibility: "10 Hard Checks: bounds, reversibility, protected workloads (BUILD), cooldowns, jump limits",
          status: getStageStatus("safety_gate"),
          durationMs: getStageDuration("safety_gate"),
          inputSummary: "ProposedAction from Decision Agent",
          outputSummary: gateEv?.summary || "Safety verdict pending",
          operationsSummary: "100% deterministic code; never trusts LLM; halts unauthorized mutations",
          upstreamIds: ["decision"],
          downstreamIds: ["controller"],
          rawInput: gateEv?.payload?.proposed_action,
          rawOutput: gateEv?.payload,
          isSelected: selectedNodeId === "safety_gate",
          isExecuting: getStageStatus("safety_gate") === "running",
        },
      },
      // 8. Resource Controller (Actuator)
      {
        id: "controller",
        type: "workflowNode",
        position: { x: 1840, y: 180 },
        data: {
          id: "controller",
          name: "Resource Controller",
          category: "system_actuator",
          framework: "Linux Kernel",
          responsibility: "Applies knob change via Linux cgroups v2 (cpu.weight, cpu.max, freeze) with write-ahead log",
          status: getStageStatus("controller"),
          durationMs: getStageDuration("controller"),
          inputSummary: "Approved action parameters",
          outputSummary: controllerEv?.summary || "Cgroup actuation status",
          operationsSummary: "Write-ahead logging in actions.db + cgroup v2 write & readback verification",
          upstreamIds: ["safety_gate"],
          downstreamIds: ["evaluation"],
          rawInput: controllerEv?.payload,
          rawOutput: controllerEv?.payload,
          isSelected: selectedNodeId === "controller",
          isExecuting: getStageStatus("controller") === "running",
        },
      },
      // 9. Evaluation Engine
      {
        id: "evaluation",
        type: "workflowNode",
        position: { x: 2100, y: 180 },
        data: {
          id: "evaluation",
          name: "Evaluation Engine",
          category: "system_eval",
          framework: "Deterministic",
          responsibility: "Measures after-performance vs before-baseline; triggers automatic rollback if degraded",
          status: getStageStatus("evaluation"),
          durationMs: getStageDuration("evaluation"),
          inputSummary: "Post-action build duration / PSI vs baseline duration",
          outputSummary: evalEv?.summary || "Performance evaluation",
          operationsSummary: "Calculates speedup ratio; commits if improved, triggers instant rollback if degraded",
          upstreamIds: ["controller"],
          downstreamIds: ["policy"],
          rawInput: evalEv?.payload,
          rawOutput: evalEv?.payload,
          isSelected: selectedNodeId === "evaluation",
          isExecuting: getStageStatus("evaluation") === "running",
        },
      },
      // 10. Policy Store & Learning
      {
        id: "policy",
        type: "workflowNode",
        position: { x: 2360, y: 180 },
        data: {
          id: "policy",
          name: "Policy Store",
          category: "system_store",
          framework: "Deterministic",
          responsibility: "Records granular cycle outcome in policy.db; tracks historical success rates & trends",
          status: getStageStatus("policy"),
          durationMs: getStageDuration("policy"),
          inputSummary: "Evaluation verdict, action applied & workload context",
          outputSummary: policyEv?.summary || "Policy record updated",
          operationsSummary: "SQLite append-only outcomes + moving average success tracking",
          upstreamIds: ["evaluation"],
          downstreamIds: ["meta"],
          rawInput: policyEv?.payload,
          rawOutput: policyEv?.payload,
          isSelected: selectedNodeId === "policy",
          isExecuting: getStageStatus("policy") === "running",
        },
      },
      // 11. Meta-Agent (ADK)
      {
        id: "meta",
        type: "workflowNode",
        position: { x: 2620, y: 180 },
        data: {
          id: "meta",
          name: "Meta-Agent",
          category: "agent_meta",
          framework: "Google ADK",
          model: "gemini-3.6-flash",
          responsibility: "Policy Governance: evaluates long-term statistics (>=10 samples, >=80% success) for promotion",
          status: getStageStatus("meta"),
          durationMs: getStageDuration("meta"),
          inputSummary: "Long-term policy statistics & empirical success rates",
          outputSummary: metaEv?.summary || (getStageStatus("meta") === "skipped" ? "Skipped: insufficient samples" : "Meta verdict"),
          operationsSummary: "Gated by deterministic floor check; promotes/demotes policies in system knowledge",
          upstreamIds: ["policy"],
          downstreamIds: [],
          rawInput: metaEv?.payload,
          rawOutput: metaEv?.payload,
          isSelected: selectedNodeId === "meta",
          isExecuting: getStageStatus("meta") === "running",
        },
      },
    ];

    // Directional Edges with animated states when data is in flight or completed
    const makeEdge = (
      id: string,
      source: string,
      target: string,
      label?: string,
      isConditional = false,
    ): Edge => {
      const sourceNode = nodes.find((n) => n.id === source);
      const targetNode = nodes.find((n) => n.id === target);

      const sourceActive = sourceNode?.data.status === "running" || sourceNode?.data.status === "completed";
      const targetActive = targetNode?.data.status === "running";
      const isAnimated = sourceActive && (targetActive || sourceNode?.data.status === "running");

      return {
        id,
        source,
        target,
        animated: isAnimated,
        label,
        type: "smoothstep",
        style: {
          stroke: isConditional ? "var(--color-warn)" : isAnimated ? "var(--color-accent)" : "var(--color-border-strong)",
          strokeWidth: isAnimated ? 2 : 1.5,
          strokeDasharray: isConditional ? "5,5" : undefined,
        },
        labelStyle: {
          fill: "var(--color-fg-muted)",
          fontSize: 10,
          fontWeight: 500,
        },
        labelBgStyle: {
          fill: "var(--color-panel)",
          fillOpacity: 0.9,
        },
      };
    };

    const edges: Edge[] = [
      makeEdge("e-trig-class", "trigger", "classify", "telemetry"),
      makeEdge("e-class-sup", "classify", "supervisor", "evidence dict"),
      makeEdge("e-sup-cpu", "supervisor", "diag_cpu", "A2A Task"),
      makeEdge("e-sup-ram", "supervisor", "diag_ram", "A2A Task"),
      makeEdge("e-sup-io", "supervisor", "diag_io", "A2A Task"),
      makeEdge("e-cpu-guard", "diag_cpu", "action_guard", "CpuDiagnosis"),
      makeEdge("e-ram-guard", "diag_ram", "action_guard", "RamDiagnosis"),
      makeEdge("e-io-guard", "diag_io", "action_guard", "IoDiagnosis"),
      makeEdge("e-guard-dec", "action_guard", "decision", "candidate actions"),
      makeEdge("e-dec-gate", "decision", "safety_gate", "ProposedAction"),
      makeEdge("e-gate-ctrl", "safety_gate", "controller", "Approved"),
      makeEdge("e-ctrl-eval", "controller", "evaluation", "post-tuning"),
      makeEdge("e-eval-pol", "evaluation", "policy", "outcome"),
      makeEdge("e-pol-meta", "policy", "meta", "sample floor check", true),
    ];

    // Build timeline events from actual StageEvents
    const timelineEvents: WorkflowTimelineEvent[] = cycleEvents.map((e, idx) => {
      let eventType: WorkflowTimelineEvent["type"] = "processing";
      if (e.stage.includes("start") || e.status === "running") eventType = "start";
      else if (e.stage === "trigger") eventType = "input";
      else if (e.stage === "supervisor" || e.stage === "diagnostics") eventType = "handoff";
      else if (e.stage === "safety_gate") eventType = "check";
      else if (e.status === "failed") eventType = "error";

      return {
        id: `tl-${idx}-${e.stage}-${e.seq}`,
        timestamp: e.ts,
        timeFormatted: clockTime(e.ts),
        type: eventType,
        sourceNodeId: e.stage,
        sourceName: e.stage.replace("_", " ").toUpperCase(),
        title: `${e.stage} — ${e.status.toUpperCase()}`,
        description: e.summary || `Stage ${e.stage} reached ${e.status}`,
        status: e.status,
        payloadSnippet: e.payload ? JSON.stringify(e.payload).slice(0, 100) : undefined,
        rawPayload: e.payload,
      };
    });

    // Compute Agent Counts & Statistics
    const agentNodes = nodes.filter((n) => n.data.category.startsWith("agent_"));
    const agentCounts = {
      total: agentNodes.length,
      completed: agentNodes.filter((n) => n.data.status === "completed").length,
      running: agentNodes.filter((n) => n.data.status === "running").length,
      pending: agentNodes.filter((n) => n.data.status === "waiting").length,
      failed: agentNodes.filter((n) => n.data.status === "failed" || n.data.status === "rejected").length,
      skipped: agentNodes.filter((n) => n.data.status === "skipped").length,
    };

    const selectedNode = selectedNodeId ? nodes.find((n) => n.id === selectedNodeId) ?? null : null;

    return {
      nodes,
      edges,
      timelineEvents,
      agentCounts,
      selectedNode,
    };
  }, [run, cycle, events, selectedNodeId]);
}
