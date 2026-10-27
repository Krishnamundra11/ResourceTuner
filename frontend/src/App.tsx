import { Suspense, lazy } from "react";
import { Route, Routes } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AppLayout } from "@/components/AppLayout";
import { LoadingPanel } from "@/components/StateViews";

// Routes are split so that the two heavy libraries load only for the views that draw with them:
// ECharts for the charted views, React Flow for the pipeline graph and the replay player.
const Overview = lazy(() => import("@/views/Overview"));
const LiveLab = lazy(() => import("@/views/LiveLab"));
const AgentPipeline = lazy(() => import("@/views/AgentPipeline"));
const Diagnosis = lazy(() => import("@/views/Diagnosis"));
const SafetyActions = lazy(() => import("@/views/SafetyActions"));
const Performance = lazy(() => import("@/views/Performance"));
const PolicyLearning = lazy(() => import("@/views/PolicyLearning"));
const HistoryReplay = lazy(() => import("@/views/HistoryReplay"));
const SystemHealth = lazy(() => import("@/views/SystemHealth"));

export default function App() {
  return (
    <TooltipProvider delayDuration={200}>
      <AppLayout>
        <Suspense fallback={<LoadingPanel rows={5} />}>
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/live-lab" element={<LiveLab />} />
            <Route path="/pipeline" element={<AgentPipeline />} />
            <Route path="/diagnosis" element={<Diagnosis />} />
            <Route path="/safety" element={<SafetyActions />} />
            <Route path="/performance" element={<Performance />} />
            <Route path="/policy" element={<PolicyLearning />} />
            <Route path="/history" element={<HistoryReplay />} />
            <Route path="/system" element={<SystemHealth />} />
            <Route path="*" element={<Overview />} />
          </Routes>
        </Suspense>
      </AppLayout>
    </TooltipProvider>
  );
}
