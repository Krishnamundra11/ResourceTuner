/** Interactive React Flow Canvas for the Agent Workflow visualization.
 *
 *  Features:
 *  - Custom Node Types (`workflowNode: AgentWorkflowNode`)
 *  - Animated directional edges representing live data flow
 *  - Zoom / Pan / Fit View controls
 *  - MiniMap themed to the dashboard
 *  - Node selection callback
 */

import { useMemo, useCallback } from "react";
import {
  ReactFlow, Background, Controls, MiniMap,
  type Node, type Edge, type NodeMouseHandler, BackgroundVariant,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { AgentWorkflowNode } from "./AgentWorkflowNode";
import type { WorkflowNodeData } from "./workflowTypes";

interface AgentWorkflowCanvasProps {
  nodes: Node<WorkflowNodeData>[];
  edges: Edge[];
  onSelectNode: (nodeId: string | null) => void;
  selectedNodeId?: string | null;
}

export function AgentWorkflowCanvas({
  nodes,
  edges,
  onSelectNode,
}: AgentWorkflowCanvasProps) {
  const nodeTypes = useMemo(() => ({ workflowNode: AgentWorkflowNode }), []);

  const handleNodeClick: NodeMouseHandler = useCallback(
    (_, node) => {
      onSelectNode(node.id);
    },
    [onSelectNode],
  );

  const handlePaneClick = useCallback(() => {
    onSelectNode(null);
  }, [onSelectNode]);

  return (
    <div className="relative h-[560px] w-full rounded-xl border border-border bg-bg overflow-hidden shadow-inner">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={handleNodeClick}
        onPaneClick={handlePaneClick}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        minZoom={0.2}
        maxZoom={1.8}
        proOptions={{ hideAttribution: true }}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={20}
          size={1.2}
          color="oklch(0.35 0.01 265)"
        />

        <Controls
          className="!bg-panel !border-border !shadow-lg [&>button]:!bg-panel [&>button]:!border-border [&>button]:!text-fg [&>button:hover]:!bg-panel-hover"
        />

        <MiniMap
          nodeColor={(n) => {
            const data = n.data as WorkflowNodeData;
            if (data?.status === "completed") return "var(--color-ok)";
            if (data?.status === "running") return "var(--color-accent)";
            if (data?.status === "failed") return "var(--color-bad)";
            return "var(--color-border-strong)";
          }}
          className="!bg-panel/90 !border-border !rounded-lg overflow-hidden hidden md:block"
          maskColor="rgba(0, 0, 0, 0.6)"
        />
      </ReactFlow>

      {/* Floating Canvas Legend */}
      <div className="absolute bottom-3 left-3 hidden md:flex items-center gap-3 rounded-lg border border-border bg-panel/90 px-3 py-1.5 text-[10px] text-fg-muted backdrop-blur-xs shadow-md">
        <span className="font-semibold text-fg-subtle uppercase">Frameworks:</span>
        <span className="flex items-center gap-1">
          <span className="size-2 rounded-full bg-blue-500" /> Google ADK
        </span>
        <span className="flex items-center gap-1">
          <span className="size-2 rounded-full bg-emerald-500" /> CrewAI
        </span>
        <span className="flex items-center gap-1">
          <span className="size-2 rounded-full bg-purple-500" /> Deterministic
        </span>
        <span className="flex items-center gap-1">
          <span className="size-2 rounded-full bg-amber-500" /> Linux Kernel
        </span>
      </div>
    </div>
  );
}
