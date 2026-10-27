import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import {
  Activity, Brain, ChevronLeft, FlaskConical, Gauge, History, LayoutDashboard, Menu,
  PanelLeftClose, PanelLeftOpen, ShieldCheck, Stethoscope, TrendingUp, Workflow, X,
  type LucideIcon,
} from "lucide-react";
import { useSnapshot } from "@/hooks/useSnapshot";
import { useActiveRun } from "@/hooks/useRuns";
import { Badge } from "./ui/badge";
import { Separator } from "./ui/misc";
import { Tooltip, TooltipContent, TooltipTrigger } from "./ui/tooltip";
import { RUN_STATUS_META, workloadTone } from "@/lib/status";
import { pct, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Match this route exactly — only the index route needs it. */
  end?: boolean;
}

export const NAV: NavItem[] = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/live-lab", label: "Live Lab", icon: FlaskConical },
  { to: "/pipeline", label: "Agent Pipeline", icon: Workflow },
  { to: "/diagnosis", label: "Diagnosis", icon: Stethoscope },
  { to: "/safety", label: "Safety & Actions", icon: ShieldCheck },
  { to: "/performance", label: "Performance", icon: TrendingUp },
  { to: "/policy", label: "Policy Learning", icon: Brain },
  { to: "/history", label: "Runs & Replay", icon: History },
  { to: "/system", label: "System Health", icon: Activity },
] as const;

export function AppLayout({ children }: { children: React.ReactNode }) {
  // `collapsed` is the desktop icon-only rail; `mobileOpen` is the below-`lg` off-canvas drawer.
  // They are independent: a 232px rail has nowhere to go on a 393px phone, so below `lg` the
  // sidebar is removed from the layout flow entirely and slides in as an overlay instead.
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const current = NAV.find((n) => (n.end ? location.pathname === n.to : location.pathname.startsWith(n.to)));

  return (
    <div className="flex min-h-screen bg-bg">
      {mobileOpen && (
        <div className="fixed inset-0 z-30 bg-black/60 lg:hidden"
             onClick={() => setMobileOpen(false)} aria-hidden="true" />
      )}

      <aside className={cn(
        "fixed inset-y-0 left-0 z-40 flex h-screen w-[232px] flex-col border-r border-border bg-bg-raised",
        "transition-transform duration-200 lg:sticky lg:top-0 lg:shrink-0 lg:translate-x-0 lg:transition-[width]",
        mobileOpen ? "translate-x-0" : "-translate-x-full",
        collapsed && "lg:w-[60px]",
      )}>
        <div className="flex h-14 items-center gap-2.5 px-3">
          <div className="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-dim text-accent-strong">
            <Gauge className="size-4.5" />
          </div>
          {!collapsed && (
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold leading-tight">ResourceTuner</div>
              <div className="truncate text-[10px] uppercase tracking-wide text-fg-subtle">Control Center</div>
            </div>
          )}
          <button onClick={() => setMobileOpen(false)} aria-label="Close navigation"
                  className="ml-auto shrink-0 rounded-md p-1.5 text-fg-subtle hover:bg-panel-hover hover:text-fg lg:hidden">
            <X className="size-4" />
          </button>
        </div>
        <Separator />
        <nav className="flex-1 space-y-0.5 overflow-y-auto p-2">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} onClick={() => setMobileOpen(false)}
              className={({ isActive }) => cn(
                "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-[13px] font-medium transition-colors",
                isActive ? "bg-accent-dim/50 text-accent-strong" : "text-fg-muted hover:bg-panel-hover hover:text-fg",
                collapsed && "lg:justify-center lg:px-0")}
              title={collapsed ? label : undefined}>
              <Icon className="size-4 shrink-0" />
              <span className={cn("truncate", collapsed && "lg:hidden")}>{label}</span>
            </NavLink>
          ))}
        </nav>
        <Separator />
        <button onClick={() => setCollapsed((c) => !c)}
          className="hidden items-center gap-2 px-3 py-2.5 text-xs text-fg-subtle hover:text-fg lg:flex"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}>
          {collapsed ? <PanelLeftOpen className="size-4" /> : <PanelLeftClose className="size-4" />}
          {!collapsed && <span>Collapse</span>}
        </button>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <SystemStatusBar title={current?.label ?? "ResourceTuner"} onOpenNav={() => setMobileOpen(true)} />
        <main className="min-w-0 flex-1 overflow-x-hidden p-4 lg:p-6">{children}</main>
      </div>
    </div>
  );
}

function SystemStatusBar({ title, onOpenNav }: { title: string; onOpenNav: () => void }) {
  const { snapshot, warmingUp, offline, isStale, ageSeconds } = useSnapshot();
  const { data: activeData } = useActiveRun();
  const active = activeData?.active ?? null;
  const cls = snapshot?.classification ?? null;

  return (
    <header className="sticky top-0 z-20 flex h-14 flex-wrap items-center gap-x-4 gap-y-1 border-b border-border bg-bg/92 px-4 backdrop-blur lg:px-6">
      <button onClick={onOpenNav} aria-label="Open navigation"
              className="shrink-0 rounded-md p-1.5 text-fg-muted hover:bg-panel-hover hover:text-fg lg:hidden">
        <Menu className="size-4.5" />
      </button>
      <h1 className="text-sm font-semibold">{title}</h1>
      <div className="ml-auto flex flex-wrap items-center gap-2 text-xs">
        {active ? (
          <NavLink to="/live-lab">
            <Badge tone={RUN_STATUS_META[active.status].tone} className="gap-1.5">
              <span className="relative flex size-1.5">
                <span className="absolute inline-flex size-full animate-ping rounded-full bg-current opacity-70" />
                <span className="relative inline-flex size-1.5 rounded-full bg-current" />
              </span>
              Run {active.cycles_done}/{active.cycles_requested} · {active.scenario}
            </Badge>
          </NavLink>
        ) : (
          <Badge tone="idle">No run active</Badge>
        )}

        {cls && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Badge tone={workloadTone(cls.workload)}>
                {cls.workload}
                {cls.confidence !== null && <span className="opacity-70">· {cls.confidence.toFixed(2)}</span>}
              </Badge>
            </TooltipTrigger>
            <TooltipContent>
              Classified workload. Confidence = min(dominance, persistence) over a {cls.window}s window.
              {cls.needs_llm_fallback && " LLM fallback is flagged for this window."}
            </TooltipContent>
          </Tooltip>
        )}
        {cls?.needs_llm_fallback && <Badge tone="warn">LLM fallback</Badge>}

        {snapshot && (
          <span className="hidden items-center gap-3 text-fg-subtle nums sm:flex">
            <span>CPU {pct(snapshot.host.cpu_percent)}</span>
            <span>PSI {pct(snapshot.host.psi_cpu)}</span>
          </span>
        )}

        <Tooltip>
          <TooltipTrigger asChild>
            <Badge tone={offline ? "bad" : warmingUp ? "warn" : isStale ? "warn" : "ok"} className="gap-1.5">
              <span className={cn("size-1.5 rounded-full bg-current", !offline && !isStale && "animate-pulse")} />
              {offline ? "Backend down" : warmingUp ? "Warming up" : isStale ? "Stale" : "Live"}
            </Badge>
          </TooltipTrigger>
          <TooltipContent>
            {offline ? "Cannot reach the dashboard API."
              : warmingUp ? "The sampler is filling its first window."
              : `Newest sample ${relativeTime(snapshot?.timestamp)} (${ageSeconds}s).`}
          </TooltipContent>
        </Tooltip>
      </div>
    </header>
  );
}

export function BackLink({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink to={to} className="inline-flex items-center gap-1 text-xs text-fg-subtle hover:text-fg">
      <ChevronLeft className="size-3.5" />{children}
    </NavLink>
  );
}
