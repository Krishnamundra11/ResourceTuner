/** Agent Workflow — End-to-End AI Agent Execution Pipeline Visualization.
 *
 *  Interactive, real-time observability of the complete multi-agent system:
 *  - Displays all 6 agents (ADK Supervisor, 3 CrewAI Specialists, Decision, Meta)
 *  - Visualizes parallel execution, A2A protocol handoffs, and conditional branches
 *  - Node-level inspection for exact inputs, operations, and outputs
 *  - Live execution updates via Server-Sent Events (SSE)
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/primitives";
import { RunPicker, useRunSelection } from "@/components/RunPicker";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "@/components/StateViews";

import { useWorkflowModel } from "@/components/workflow/useWorkflowModel";
import { WorkflowOverviewHeader } from "@/components/workflow/WorkflowOverviewHeader";
import { AgentWorkflowCanvas } from "@/components/workflow/AgentWorkflowCanvas";
import { AgentInspectorDrawer } from "@/components/workflow/AgentInspectorDrawer";
import { AgentTimelineStream } from "@/components/workflow/AgentTimelineStream";

export default function AgentWorkflow() {
  const sel = useRunSelection();
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  const {
    nodes,
    edges,
    timelineEvents,
    agentCounts,
    selectedNode,
  } = useWorkflowModel({
    run: sel.run,
    cycle: sel.cycle,
    events: sel.events,
    selectedNodeId,
  });

  return (
    <div className="space-y-4">
      {/* Top Page Header */}
      <PageHeader
        title="Agent Workflow"
        subtitle="End-to-End interactive observability of the multi-agent pipeline: trace how Google ADK and CrewAI agents collaborate, exchange inputs and outputs, verify constraints, and execute autonomous cgroup resource tuning."
        actions={<RunPicker sel={sel} />}
      />

      {sel.error ? <ErrorPanel error={sel.error} /> : null}

      {!sel.run ? (
        <EmptyPanel
          title="No runs recorded yet"
          hint={
            <>
              Launch a tuning cycle from <Link className="text-accent hover:underline" to="/live-lab">Live Lab</Link> to visualize live agent execution.
            </>
          }
        />
      ) : sel.isPending ? (
        <LoadingPanel rows={6} />
      ) : (
        <>
          {/* Section 1: Execution Overview Header */}
          <WorkflowOverviewHeader sel={sel} agentCounts={agentCounts} />

          {/* Section 2: Interactive Agent Flowchart Canvas */}
          <section className="space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-semibold text-fg uppercase tracking-wider">
                Multi-Agent Execution Pipeline Flowchart
              </h3>
              <span className="text-[11px] text-fg-subtle">
                Click any agent node to inspect inputs, processing & outputs
              </span>
            </div>

            <AgentWorkflowCanvas
              nodes={nodes}
              edges={edges}
              onSelectNode={setSelectedNodeId}
              selectedNodeId={selectedNodeId}
            />
          </section>

          {/* Section 3: Chronological Execution Timeline */}
          <section>
            <AgentTimelineStream
              events={timelineEvents}
              onSelectNode={setSelectedNodeId}
            />
          </section>

          {/* Section 4: Detailed Side Inspector Drawer */}
          <AgentInspectorDrawer
            node={selectedNode ? selectedNode.data : null}
            onClose={() => setSelectedNodeId(null)}
          />
        </>
      )}
    </div>
  );
}
