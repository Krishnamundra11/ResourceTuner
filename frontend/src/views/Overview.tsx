/** Overview — what the host is doing right now, and whether a tuning run is in flight.
 *
 *  Every number here comes from GET /api/snapshot (the live sampler) or GET /api/runs/active.
 *  A metric the host cannot provide renders as "—" with the reason, never as 0. */

import { Link } from "react-router-dom";
import { Activity, Boxes, Cpu, HardDrive, MemoryStick } from "lucide-react";

import { ClassActivityBars, SERIES_1, SERIES_2, TimeSeriesChart, WorkloadStrip } from "@/components/charts";
import { KeyValue, PageHeader, Panel, SectionTitle, StatTile } from "@/components/primitives";
import { EmptyPanel, ErrorPanel, StaleBanner, Unavailable, WarmingUp } from "@/components/StateViews";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, Td, Th } from "@/components/ui/misc";
import { useActiveRun } from "@/hooks/useRuns";
import { useSnapshot } from "@/hooks/useSnapshot";
import { DASH, dateTime, mib, num, pct, relativeTime } from "@/lib/format";
import { RUN_STATUS_META, workloadColor, workloadTone } from "@/lib/status";
import type { HostMetrics } from "@/lib/types";

/** PSI "some avg10": the share of the last 10s in which at least one task was stalled. */
const PSI_HINT = "Linux pressure stall information, /proc/pressure — 'some avg10': the percentage of the last 10 seconds in which at least one task was stalled waiting for this resource. This is the signal that triggers a tuning cycle.";

function psiTone(v: number | null): "ok" | "warn" | "bad" | "muted" {
  if (v === null) return "muted";
  if (v >= 20) return "bad";
  if (v >= 5) return "warn";
  return "ok";
}

export default function Overview() {
  const { snapshot, warmingUp, error, isStale, ageSeconds, refetch } = useSnapshot();
  const active = useActiveRun();
  const run = active.data?.active ?? null;

  if (!snapshot && warmingUp) return <WarmingUp />;
  if (!snapshot && error) return <ErrorPanel error={error} onRetry={() => void refetch()} />;
  if (!snapshot) return <WarmingUp />;

  const h: HostMetrics = snapshot.host;
  const cls = snapshot.classification;
  const history = snapshot.history ?? [];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Overview"
        subtitle="Live host telemetry and the deterministic workload classification derived from it. Nothing on this page is modelled or predicted — it is the sampler's most recent window."
        actions={
          run ? (
            <Link to="/live-lab">
              <Button size="sm">
                <Activity className="size-3.5" />
                Run {run.run_id} is {RUN_STATUS_META[run.status].label.toLowerCase()}
              </Button>
            </Link>
          ) : (
            <Link to="/live-lab"><Button size="sm" variant="secondary">Open Live Lab</Button></Link>
          )
        }
      />

      {isStale && ageSeconds !== null && <StaleBanner ageSeconds={ageSeconds} />}
      {snapshot.sampler_error && (
        <p className="rounded-md border border-warn/40 bg-warn-dim/30 px-3 py-1.5 text-xs text-warn">
          Sampler reported: {snapshot.sampler_error}
        </p>
      )}

      {/* --- host headline numbers --- */}
      <section>
        <SectionTitle right={<span className="text-[11px] text-fg-subtle">sampled {relativeTime(snapshot.timestamp)}</span>}>
          Host
        </SectionTitle>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label="CPU" value={pct(h.cpu_percent, 1)}
                    tone={h.cpu_percent >= 85 ? "bad" : h.cpu_percent >= 60 ? "warn" : "ok"}
                    foot={`${h.cores} vCPU · load ${num(h.load_1m, 2)}`} />
          <StatTile label="Memory" value={pct(h.memory_percent, 1)}
                    tone={h.memory_percent >= 90 ? "bad" : h.memory_percent >= 75 ? "warn" : "ok"}
                    foot={`${mib(h.memory_total_mib - h.memory_available_mib)} of ${mib(h.memory_total_mib)}`} />
          <StatTile label="I/O wait" value={pct(h.iowait_percent, 1)}
                    hint="Share of CPU time spent idle with at least one outstanding block-I/O request."
                    tone={h.iowait_percent >= 20 ? "bad" : h.iowait_percent >= 8 ? "warn" : "ok"}
                    foot={h.disk_util_percent === null
                      ? "disk utilisation unavailable"
                      : `disk ${pct(h.disk_util_percent, 0)} busy`} />
          <StatTile label="Disk throughput" value={num(h.disk_read_mib_s + h.disk_write_mib_s, 1)} unit="MiB/s"
                    foot={`${num(h.disk_read_mib_s, 1)} read · ${num(h.disk_write_mib_s, 1)} write`} />
        </div>
      </section>

      {/* --- PSI: the trigger signal --- */}
      <section>
        <SectionTitle hint={PSI_HINT}>Pressure stall (some avg10)</SectionTitle>
        <div className="grid gap-2 sm:grid-cols-3">
          {([["CPU", h.psi_cpu, Cpu], ["Memory", h.psi_memory, MemoryStick], ["I/O", h.psi_io, HardDrive]] as const)
            .map(([label, value, Icon]) => (
              <StatTile key={label} label={`PSI ${label}`}
                        value={value === null ? DASH : pct(value, 2)}
                        tone={psiTone(value)}
                        foot={value === null
                          ? "/proc/pressure not readable on this host"
                          : <span className="flex items-center gap-1"><Icon className="size-3" /> stalled share of last 10s</span>} />
            ))}
        </div>
      </section>

      {/* --- recent history --- */}
      <section className="grid gap-3 lg:grid-cols-3">
        <Panel className="lg:col-span-2" title="Host CPU and memory"
               hint="The sampler's rolling window, one point per second. Both series are percentages, so they share a single axis.">
          {history.length < 2 ? (
            <EmptyPanel title="Not enough samples yet" hint="The chart appears once the sampler has collected a few seconds of history." />
          ) : (
            <>
              <TimeSeriesChart
                label="Host CPU and memory percentage over the recent sampling window"
                height={190}
                max={100}
                series={[
                  { name: "CPU", color: SERIES_1(), points: history.map((p) => [p.t * 1000, p.cpu]) },
                  { name: "Memory", color: SERIES_2(), points: history.map((p) => [p.t * 1000, p.mem]) },
                ]}
              />
              <div className="mt-3 border-t border-border pt-3">
                <SectionTitle hint="The class the deterministic rule engine assigned in each sample. It is recorded, not inferred from the chart above.">
                  Classified workload
                </SectionTitle>
                <WorkloadStrip points={history.map((p) => ({ t: p.t, workload: p.workload }))} />
              </div>
            </>
          )}
        </Panel>

        <Panel title="Current classification"
               hint="Produced by resourcetuner/classifier/rules.py. Confidence is min(dominance, persistence) — both are measured ratios.">
          {!cls ? (
            <EmptyPanel title="Classifier has no window yet" />
          ) : (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <span className="size-2.5 rounded-sm" style={{ background: workloadColor(cls.workload) }} />
                <span className="text-base font-semibold text-fg">{cls.workload.replace(/_/g, " ")}</span>
                <Badge tone={workloadTone(cls.workload)}>
                  {/* IDLE carries no confidence: there is no winning class to be confident about. */}
                  {cls.confidence === null ? "no confidence score" : `${pct(cls.confidence * 100, 0)} confidence`}
                </Badge>
              </div>
              <KeyValue rows={[
                { k: "Dominance", v: cls.dominance === null ? <Unavailable /> : pct(cls.dominance * 100, 1),
                  hint: "Share of measured activity belonging to the winning class." },
                { k: "Persistence", v: cls.persistence === null ? <Unavailable /> : pct(cls.persistence * 100, 1),
                  hint: "Share of the window in which that class kept winning." },
                { k: "Window", v: `${cls.window} samples` },
                { k: "Unattributed", v: pct(cls.unattributed_activity_percent, 1),
                  hint: "Activity from processes no signature matched. High values are why the LLM fallback exists." },
              ]} />
              {cls.needs_llm_fallback && (
                <div className="rounded-md border border-warn/40 bg-warn-dim/30 px-2.5 py-2 text-[11px] text-warn">
                  <strong className="font-medium">LLM fallback would be requested.</strong>
                  <ul className="mt-1 list-inside list-disc space-y-0.5">
                    {cls.fallback_reasons.map((r) => <li key={r}>{r}</li>)}
                  </ul>
                </div>
              )}
              {Object.keys(cls.class_activity_percent).length > 0 && (
                <div>
                  <SectionTitle hint="CPU% + I/O-wait% attributed to each class. I/O wait is included because an I/O-starved indexer uses almost no CPU.">
                    Activity by class
                  </SectionTitle>
                  <ClassActivityBars activity={cls.class_activity_percent} />
                </div>
              )}
            </div>
          )}
        </Panel>
      </section>

      {/* --- containers --- */}
      <section>
        <SectionTitle right={
          snapshot.docker.available
            ? <span className="text-[11px] text-fg-subtle">{snapshot.containers.length} running</span>
            : <Badge tone="bad">Docker unavailable</Badge>
        }>
          Containers
        </SectionTitle>
        <Panel bodyClassName="p-0">
          {!snapshot.docker.available ? (
            <EmptyPanel title="Docker is not reachable" icon={<Boxes className="size-6" />}
                        hint={snapshot.docker.error ?? "The telemetry service could not enumerate cgroups."} />
          ) : snapshot.containers.length === 0 ? (
            <EmptyPanel title="No containers running" icon={<Boxes className="size-6" />}
                        hint={<>Start one from <Link className="text-accent hover:underline" to="/live-lab">Live Lab</Link>, which launches a scenario through the backend.</>} />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Container</Th><Th>Image</Th><Th className="text-right">CPU</Th>
                  <Th className="text-right">Memory</Th><Th className="text-right">Read</Th>
                  <Th className="text-right">Write</Th><Th>Classified as</Th><Th>Ground truth</Th>
                </tr>
              </thead>
              <tbody>
                {snapshot.containers.map((c) => (
                  <tr key={c.name} className="border-t border-border/60">
                    <Td className="font-medium text-fg">{c.name}</Td>
                    <Td className="text-fg-subtle">{c.image}</Td>
                    <Td className="nums text-right">{c.cpu_percent === null ? <Unavailable /> : pct(c.cpu_percent, 1)}</Td>
                    <Td className="nums text-right">
                      {c.memory_mib === null ? <Unavailable /> : mib(c.memory_mib)}
                      {c.memory_limit_mib ? <span className="text-fg-subtle"> / {mib(c.memory_limit_mib)}</span> : null}
                    </Td>
                    <Td className="nums text-right">{num(c.io_read_mib_s, 1)}</Td>
                    <Td className="nums text-right">{num(c.io_write_mib_s, 1)}</Td>
                    <Td>
                      {c.classified_as ? (
                        <span className="flex items-center gap-1.5">
                          <span className="size-2 rounded-sm" style={{ background: workloadColor(c.classified_as) }} />
                          {c.classified_as.replace(/_/g, " ")}
                        </span>
                      ) : <Unavailable why="no class assigned in this window" />}
                    </Td>
                    <Td className="text-fg-subtle">{c.ground_truth_label ?? DASH}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Panel>
        <p className="mt-1.5 text-[11px] text-fg-subtle">
          Ground truth comes from the scenario definition and is shown for validation only — the classifier never reads it.
        </p>
      </section>

      {/* --- active run --- */}
      {run && (
        <section>
          <SectionTitle>Active run</SectionTitle>
          <Panel>
            <div className="flex flex-wrap items-center gap-3">
              <Badge tone={RUN_STATUS_META[run.status].tone}>{RUN_STATUS_META[run.status].label}</Badge>
              <span className="nums text-sm text-fg">{run.run_id}</span>
              <span className="text-xs text-fg-subtle">
                {run.scenario}{run.level ? ` · ${run.level}` : ""} · cycle {run.cycles_done}/{run.cycles_requested}
                {" · started "}{dateTime(run.started_at)}
              </span>
              <Link to="/pipeline" className="ml-auto">
                <Button size="sm" variant="secondary">Watch the pipeline</Button>
              </Link>
            </div>
          </Panel>
        </section>
      )}
    </div>
  );
}
