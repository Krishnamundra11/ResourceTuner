/** Live Lab — choose a scenario that exists in the repo and start a real tuning run.
 *
 *  Start posts to POST /api/runs, which hands the request to RunManager → VerticalSlice →
 *  Safety Gate → Resource Controller. The browser never touches Docker, a cgroup or a shell;
 *  the only verbs it has are "start this named scenario" and "stop this run".
 *
 *  Starting a run launches containers and runs real builds, so it is behind an explicit
 *  confirmation that states what will happen. Stop is one click: it is the safe direction. */

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, CircleStop, FlaskConical, Play } from "lucide-react";

import { PageHeader, Panel, ReadOnlyNote } from "@/components/primitives";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "@/components/StateViews";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { Label, Slider, Switch } from "@/components/ui/misc";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Hint } from "@/components/ui/tooltip";
import { useEventStream } from "@/hooks/useEventStream";
import { useActiveRun, useScenarios, useStartRun, useStopRun } from "@/hooks/useRuns";
import { clockTime, duration, ms, seconds } from "@/lib/format";
import { INTERVENTION_META, RUN_STATUS_META, STAGE_LABELS, STAGE_STATUS_META } from "@/lib/status";
import type { Scenario } from "@/lib/types";

export default function LiveLab() {
  const scenarios = useScenarios();
  const active = useActiveRun();
  const start = useStartRun();
  const stop = useStopRun();

  const run = active.data?.active ?? null;
  const list = useMemo(() => scenarios.data?.scenarios ?? [], [scenarios.data]);
  const maxCycles = scenarios.data?.max_cycles ?? 10;

  const [name, setName] = useState<string | null>(null);
  const [level, setLevel] = useState<string | null>(null);
  const [cycles, setCycles] = useState(1);
  const [cooldown, setCooldown] = useState(20);
  const [startScenario, setStartScenario] = useState(true);

  const selected = useMemo<Scenario | null>(
    () => list.find((s) => s.name === name) ?? list[0] ?? null,
    [list, name],
  );
  const levels = selected?.levels ?? [];
  const effectiveLevel = level && levels.includes(level) ? level : selected?.default_level ?? levels[0] ?? null;

  if (scenarios.isPending) return <LoadingPanel rows={5} />;
  if (scenarios.error) return <ErrorPanel error={scenarios.error} onRetry={() => void scenarios.refetch()} />;

  const busy = !!run || start.isPending;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Live Lab"
        subtitle="Start one of the scenarios defined in config/scenarios/. The backend runs the same vertical slice the CLI runs, so what you watch here is the real pipeline — real containers, real builds, real cgroup writes behind the Safety Gate."
        actions={run ? <Badge tone={RUN_STATUS_META[run.status].tone}>{RUN_STATUS_META[run.status].label}</Badge> : null}
      />

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
        {/* --- the request --- */}
        <Panel title="Scenario" hint="Only scenarios that exist as YAML in the repo are listed; the backend rejects anything else.">
          {list.length === 0 ? (
            <EmptyPanel title="No scenarios found" icon={<FlaskConical className="size-6" />}
                        hint="config/scenarios/ is empty or unreadable." />
          ) : (
            <div className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="scenario">Scenario</Label>
                <Select value={selected?.name ?? ""} onValueChange={(v) => { setName(v); setLevel(null); }}>
                  <SelectTrigger id="scenario"><SelectValue placeholder="Pick a scenario" /></SelectTrigger>
                  <SelectContent>
                    {list.map((s) => (
                      <SelectItem key={s.name} value={s.name}>{s.name.replace(/_/g, " ")}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {selected?.description && <p className="text-[11px] leading-relaxed text-fg-subtle">{selected.description}</p>}
                {selected?.error && (
                  <p className="text-[11px] text-bad">This scenario failed to load: {selected.error}</p>
                )}
              </div>

              {levels.length > 0 && (
                <div className="space-y-1.5">
                  <Label htmlFor="level">Intensity level</Label>
                  <Select value={effectiveLevel ?? ""} onValueChange={setLevel}>
                    <SelectTrigger id="level"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {levels.map((l) => <SelectItem key={l} value={l}>{l}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
              )}

              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <Label htmlFor="cycles">Cycles</Label>
                  <span className="nums text-xs text-fg">{cycles}</span>
                </div>
                <Slider id="cycles" aria-label="Cycles" min={1} max={maxCycles} step={1} value={[cycles]}
                        onValueChange={([v]) => setCycles(v)} />
                <p className="text-[11px] text-fg-subtle">
                  Each cycle runs a warm-up build, a measured baseline build and a measured post-action build.
                  The backend caps this at {maxCycles}.
                </p>
              </div>

              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <Label htmlFor="cooldown">Cooldown between cycles</Label>
                  <span className="nums text-xs text-fg">{seconds(cooldown, 0)}</span>
                </div>
                <Slider id="cooldown" aria-label="Cooldown between cycles" min={0} max={120} step={5} value={[cooldown]}
                        onValueChange={([v]) => setCooldown(v)} />
              </div>

              <div className="flex items-start justify-between gap-3 border-t border-border pt-3">
                <div>
                  <Label htmlFor="start-scenario">Launch the scenario containers</Label>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-fg-subtle">
                    Off means the containers are already running and the pipeline should attach to them
                    instead of starting its own.
                  </p>
                </div>
                <Switch id="start-scenario" checked={startScenario} onCheckedChange={setStartScenario} />
              </div>

              {selected && <InterventionNote scenario={selected} />}

              <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
                <StartDialog
                  scenario={selected}
                  level={effectiveLevel}
                  cycles={cycles}
                  disabled={!selected || !!selected.error || busy}
                  onConfirm={() => {
                    if (!selected) return;
                    start.mutate({
                      scenario: selected.name,
                      level: effectiveLevel,
                      cycles,
                      cooldown_s: cooldown,
                      start_scenario: startScenario,
                    });
                  }}
                />
                <Button variant="danger" size="sm" disabled={!run || stop.isPending}
                        onClick={() => run && stop.mutate(run.run_id)}>
                  <CircleStop className="size-3.5" />
                  {stop.isPending ? "Stopping…" : "Stop run"}
                </Button>
                {run && <span className="text-[11px] text-fg-subtle">Only one run may be active at a time.</span>}
              </div>

              {start.error && (
                <p className="rounded-md border border-bad/40 bg-bad-dim/30 px-2.5 py-2 text-[11px] text-bad">
                  {start.error.isConflict
                    ? "Another run is already active — stop it before starting a new one."
                    : start.error.message}
                </p>
              )}
              {stop.data && (
                <p className="rounded-md border border-border bg-bg-raised px-2.5 py-2 text-[11px] text-fg-muted">
                  {stop.data.detail}
                </p>
              )}
            </div>
          )}
        </Panel>

        {/* --- what is happening --- */}
        <div className="space-y-3">
          <Panel title="Run state">
            {!run ? (
              <EmptyPanel title="No run is active"
                          hint="Starting a run launches containers and runs real builds, so it takes minutes, not seconds." />
            ) : (
              <div className="space-y-2 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={RUN_STATUS_META[run.status].tone}>{RUN_STATUS_META[run.status].label}</Badge>
                  <span className="nums font-medium text-fg">{run.run_id}</span>
                  <span className="text-fg-subtle">
                    {run.scenario}{run.level ? ` · ${run.level}` : ""}
                  </span>
                </div>
                <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-fg-muted sm:grid-cols-3">
                  <Field k="Cycle" v={`${run.cycles_done} / ${run.cycles_requested}`} />
                  <Field k="Elapsed" v={duration(run.started_at, run.ended_at)} />
                  <Field k="Worker thread" v={run.thread_alive === false ? "not running" : "alive"} />
                </div>
                {run.stop_requested && (
                  <p className="flex items-center gap-1.5 text-warn">
                    <AlertTriangle className="size-3.5" />
                    Stop requested — the pipeline finishes the current stage, rolls back any unresolved
                    action and removes the scenario containers.
                  </p>
                )}
                {run.error && <p className="text-bad">{run.error}</p>}
                <Link to="/pipeline" className="inline-block pt-1">
                  <Button size="sm" variant="secondary">Open the pipeline view</Button>
                </Link>
              </div>
            )}
          </Panel>

          <LiveFeed runId={run?.run_id} />
        </div>
      </div>

      <ReadOnlyNote>
        The Start and Stop buttons are the only mutating controls in this interface, and both go
        through the backend's run manager. There is no endpoint — and therefore no button — that
        runs a shell command, issues an arbitrary Docker operation or writes a cgroup file directly.
        Every resource change is proposed by an agent, approved by the deterministic Safety Gate and
        applied by the Resource Controller with a readback check.
      </ReadOnlyNote>
    </div>
  );
}

function Field({ k, v }: { k: string; v: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-fg-subtle">{k}</div>
      <div className="nums text-fg">{v}</div>
    </div>
  );
}

function InterventionNote({ scenario }: { scenario: Scenario }) {
  const meta = INTERVENTION_META[scenario.intervention] ?? INTERVENTION_META.unknown;
  return (
    <div className="space-y-2 rounded-md border border-border bg-bg-raised px-2.5 py-2">
      <div className="flex items-center gap-2">
        <Badge tone={meta.tone}>{meta.label}</Badge>
        {scenario.resource_dimensions.map((d) => (
          <span key={d} className="text-[10px] uppercase tracking-wide text-fg-subtle">{d}</span>
        ))}
      </div>
      <p className="text-[11px] leading-relaxed text-fg-subtle">{scenario.intervention_note}</p>
      <dl className="grid gap-1 text-[11px]">
        {Object.entries(scenario.containers).map(([n, image]) => (
          <div key={n} className="flex justify-between gap-3">
            <dt className="text-fg-muted">{n}</dt>
            <dd className="text-fg-subtle">{image}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** Starting a run is slow and touches real containers, so it is confirmed, not one-click. */
function StartDialog({ scenario, level, cycles, disabled, onConfirm }: {
  scenario: Scenario | null; level: string | null; cycles: number;
  disabled: boolean; onConfirm: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" disabled={disabled}>
          <Play className="size-3.5" />
          Start run
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogTitle>Start a real tuning run?</DialogTitle>
        <DialogDescription>
          This is not a simulation. The backend will launch the scenario's containers, run
          {" "}{cycles === 1 ? "one cycle" : `${cycles} cycles`} of warm-up, baseline and post-action
          builds, and let the agents propose resource changes that the Safety Gate may approve and
          the controller may apply to live cgroups.
        </DialogDescription>
        <dl className="mt-3 space-y-1 text-xs">
          <div className="flex justify-between gap-4 border-b border-border/60 pb-1">
            <dt className="text-fg-subtle">Scenario</dt><dd className="text-fg">{scenario?.name ?? "—"}</dd>
          </div>
          <div className="flex justify-between gap-4 border-b border-border/60 pb-1">
            <dt className="text-fg-subtle">Level</dt><dd className="text-fg">{level ?? "—"}</dd>
          </div>
          <div className="flex justify-between gap-4 border-b border-border/60 pb-1">
            <dt className="text-fg-subtle">Cycles</dt><dd className="nums text-fg">{cycles}</dd>
          </div>
        </dl>
        <p className="mt-3 text-[11px] leading-relaxed text-fg-subtle">
          Any action still outstanding when the run ends — or when you press Stop — is rolled back
          to the exact value recorded before it was applied.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <DialogClose asChild><Button size="sm" variant="ghost">Cancel</Button></DialogClose>
          <Button size="sm" onClick={() => { setOpen(false); onConfirm(); }}>
            <Play className="size-3.5" />
            Start the run
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** The raw event tail, newest last. This is the same SSE stream the pipeline graph consumes. */
function LiveFeed({ runId }: { runId?: string }) {
  const { events, state, lastError } = useEventStream({ runId, enabled: true, maxEvents: 300 });
  const tail = events.slice(-40).reverse();

  return (
    <Panel
      title="Event stream"
      hint="GET /api/events/stream (server-sent events). Each event carries a monotonic seq, which is also the SSE id — a reconnect resumes from it, so nothing is lost or repeated."
      right={
        <Badge tone={state === "open" ? "ok" : state === "closed" ? "idle" : "warn"}>
          {state}
        </Badge>
      }
      bodyClassName="p-0"
    >
      {lastError && state !== "open" && (
        <p className="border-b border-border px-3 py-1.5 text-[11px] text-warn">{lastError}</p>
      )}
      {tail.length === 0 ? (
        <EmptyPanel title="No events yet"
                    hint="Events appear as soon as a run emits its first stage. The stream stays connected while idle." />
      ) : (
        <ul className="max-h-80 divide-y divide-border/60 overflow-y-auto text-[11px]">
          {tail.map((e) => {
            const meta = STAGE_STATUS_META[e.status];
            return (
              <li key={e.seq} className="flex items-baseline gap-2 px-3 py-1.5">
                <span className="nums w-10 shrink-0 text-fg-subtle">{e.seq}</span>
                <span className="nums w-16 shrink-0 text-fg-subtle">{clockTime(e.ts)}</span>
                <span className="size-1.5 shrink-0 rounded-full" style={{ background: meta.color }} />
                <span className="w-36 shrink-0 truncate text-fg-muted">
                  {STAGE_LABELS[e.stage] ?? e.stage}
                  {e.cycle !== null && <span className="text-fg-subtle"> · c{e.cycle}</span>}
                </span>
                <Hint text={meta.label}>
                  <span className="min-w-0 flex-1 truncate text-fg">{e.summary || meta.label}</span>
                </Hint>
                {e.duration_ms !== null && <span className="nums shrink-0 text-fg-subtle">{ms(e.duration_ms)}</span>}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
