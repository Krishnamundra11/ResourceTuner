/** System Health / Logs — is the machinery itself working?
 *
 *  This view answers the questions that are not about a workload: is the sampler producing
 *  samples, is Docker reachable, is per-process I/O accounting switched on, is the event stream
 *  connected, which LLM providers have been answering, and are there runs the server left behind.
 *
 *  It shows capability, not configuration. No API key, provider credential or .env value is
 *  exposed by any endpoint it reads — the event store scrubs secrets before writing, and what
 *  reaches the browser is the provider and model name only. */

import { useMemo } from "react";

import { KeyValue, PageHeader, Panel, SectionTitle, StatTile } from "@/components/primitives";
import { EmptyPanel, ErrorPanel, LoadingPanel, StaleBanner, WarmingUp } from "@/components/StateViews";
import { Badge } from "@/components/ui/badge";
import { Table, Td, Th } from "@/components/ui/misc";
import { Hint } from "@/components/ui/tooltip";
import { useEventStream } from "@/hooks/useEventStream";
import { useRunList } from "@/hooks/useRuns";
import { useSnapshot } from "@/hooks/useSnapshot";
import { DASH, clockTime, dateTime, duration, mib, num, pct, relativeTime } from "@/lib/format";
import { RUN_STATUS_META, STAGE_LABELS, STAGE_STATUS_META } from "@/lib/status";
import type { Provenance, StageEvent } from "@/lib/types";

export default function SystemHealth() {
  const { snapshot, warmingUp, error, isStale, ageSeconds, refetch } = useSnapshot();
  const runs = useRunList({ limit: 50 });
  const stream = useEventStream({ enabled: true, maxEvents: 400 });

  const h = snapshot?.host ?? null;

  // Provider provenance is recorded on the supervisor and diagnostics stages. Collect what has
  // come through this session's stream — provider and model names only.
  const providers = useMemo(() => providerTally(stream.events), [stream.events]);

  const stale = runs.data?.runs.filter((r) => r.status === "failed" && /interrupted/i.test(r.error ?? "")) ?? [];

  return (
    <div className="space-y-5">
      <PageHeader
        title="System Health"
        subtitle="The state of the measurement and orchestration machinery itself: the sampler, the container runtime, kernel accounting, the event stream and the LLM provider chain."
      />

      {isStale && ageSeconds !== null && <StaleBanner ageSeconds={ageSeconds} />}

      <section>
        <SectionTitle>Services</SectionTitle>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label="Telemetry sampler"
                    value={snapshot ? "sampling" : warmingUp ? "warming up" : "unavailable"}
                    tone={snapshot ? (isStale ? "warn" : "ok") : warmingUp ? "warn" : "bad"}
                    foot={snapshot ? `last sample ${relativeTime(snapshot.timestamp)}` : undefined} />
          <StatTile label="Docker"
                    value={snapshot ? (snapshot.docker.available ? "reachable" : "unavailable") : DASH}
                    tone={snapshot?.docker.available ? "ok" : snapshot ? "bad" : "muted"}
                    foot={snapshot?.docker.error ?? (snapshot ? `${snapshot.containers.length} container(s)` : undefined)} />
          <StatTile label="Event stream"
                    value={stream.state}
                    tone={stream.state === "open" ? "ok" : stream.state === "closed" ? "muted" : "warn"}
                    hint="Server-sent events from /api/events/stream. A reconnect resumes from the last seq, so no event is lost or repeated."
                    foot={`cursor at seq ${stream.lastSeq}`} />
          <StatTile label="I/O delay accounting"
                    value={h ? (h.delayacct_enabled ? "enabled" : h.delayacct_enabled === false ? "disabled" : DASH) : DASH}
                    tone={h?.delayacct_enabled ? "ok" : h?.delayacct_enabled === false ? "warn" : "muted"}
                    hint="kernel.task_delayacct. Without it, per-process block-I/O wait cannot be read, and an I/O-starved process looks idle because it uses almost no CPU."
                    foot={h?.delayacct_enabled === false ? "run: sudo sysctl kernel.task_delayacct=1" : undefined} />
        </div>
      </section>

      {!snapshot && warmingUp && <WarmingUp />}
      {!snapshot && error && <ErrorPanel error={error} onRetry={() => void refetch()} />}

      {snapshot && h && (
        <section className="grid gap-3 lg:grid-cols-2">
          <Panel title="Host capability"
                 hint="What this host can and cannot report. A null metric means the kernel interface is absent, not that the value is zero.">
            <KeyValue columns={2} rows={[
              { k: "Logical CPUs", v: h.cores },
              { k: "Load (1m)", v: num(h.load_1m, 2) },
              { k: "Memory total", v: mib(h.memory_total_mib) },
              { k: "Memory available", v: mib(h.memory_available_mib) },
              { k: "PSI cpu", v: h.psi_cpu === null ? "not readable" : pct(h.psi_cpu, 2) },
              { k: "PSI memory", v: h.psi_memory === null ? "not readable" : pct(h.psi_memory, 2) },
              { k: "PSI io", v: h.psi_io === null ? "not readable" : pct(h.psi_io, 2) },
              { k: "Disk utilisation", v: h.disk_util_percent === null ? "not readable" : pct(h.disk_util_percent, 0) },
            ]} />
            {snapshot.sampler_error && (
              <p className="mt-3 rounded-md border border-warn/40 bg-warn-dim/30 px-2.5 py-2 text-[11px] text-warn">
                Sampler reported: {snapshot.sampler_error}
              </p>
            )}
          </Panel>

          <Panel title="LLM provider chain"
                 hint="Collected from the provenance recorded on this session's stage events. Only the provider and model names are ever sent to the browser — no key, token or credential is exposed by any endpoint.">
            {providers.length === 0 ? (
              <EmptyPanel title="No agent calls seen in this session"
                          hint="Provider names appear once a run reaches the Supervisor or the diagnostic crew." />
            ) : (
              <Table>
                <thead>
                  <tr><Th>Provider</Th><Th>Model</Th><Th className="text-right">Answers</Th><Th>Mode</Th></tr>
                </thead>
                <tbody>
                  {providers.map((p) => (
                    <tr key={`${p.provider}-${p.model}`} className="border-t border-border/60">
                      <Td className="font-medium text-fg">{p.provider}</Td>
                      <Td className="nums text-fg-muted">{p.model}</Td>
                      <Td className="nums text-right">{p.count}</Td>
                      <Td>
                        {p.degraded
                          ? <Hint text="A degraded answer is kept for explanation but carries no action: the backend blocks it before the Safety Gate.">
                              <Badge tone="warn">degraded</Badge>
                            </Hint>
                          : <Badge tone="ok">normal</Badge>}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Panel>
        </section>
      )}

      <section>
        <SectionTitle right={runs.data ? <span className="text-[11px] text-fg-subtle">{runs.data.total} total</span> : null}>
          Runs
        </SectionTitle>
        {stale.length > 0 && (
          <p className="mb-2 rounded-md border border-warn/40 bg-warn-dim/30 px-3 py-2 text-[11px] leading-relaxed text-warn">
            {stale.length} run(s) were marked failed because the server restarted while they were
            active. Their worker threads were gone, so nothing could ever finish them; the run
            manager reaps them at startup rather than leaving them blocking new runs forever.
          </p>
        )}
        <Panel bodyClassName="p-0">
          {runs.isPending ? <LoadingPanel rows={4} />
            : runs.error ? <ErrorPanel error={runs.error} onRetry={() => void runs.refetch()} />
              : !runs.data?.runs.length ? <EmptyPanel title="No runs recorded" />
                : (
                  <Table>
                    <thead>
                      <tr><Th>Run</Th><Th>Started by</Th><Th>Scenario</Th><Th>Status</Th>
                          <Th className="text-right">Cycles</Th><Th>Created</Th><Th>Duration</Th><Th>Error</Th></tr>
                    </thead>
                    <tbody>
                      {runs.data.runs.map((r) => (
                        <tr key={r.run_id} className="border-t border-border/60">
                          <Td className="nums font-medium text-fg">{r.run_id}</Td>
                          <Td className="text-fg-subtle">{r.kind === "cli" ? "command line" : "dashboard"}</Td>
                          <Td className="text-fg-muted">{r.scenario ?? DASH}{r.level ? ` · ${r.level}` : ""}</Td>
                          <Td><Badge tone={RUN_STATUS_META[r.status].tone}>{RUN_STATUS_META[r.status].label}</Badge></Td>
                          <Td className="nums text-right">{r.cycles_done} / {r.cycles_requested}</Td>
                          <Td className="text-fg-subtle">{dateTime(r.created_at)}</Td>
                          <Td className="nums text-fg-subtle">{r.started_at ? duration(r.started_at, r.ended_at) : DASH}</Td>
                          <Td className="max-w-[20rem] truncate text-fg-subtle">{r.error ?? DASH}</Td>
                        </tr>
                      ))}
                    </tbody>
                  </Table>
                )}
        </Panel>
      </section>

      <section>
        <SectionTitle hint="The tail of the live event stream, across every run. This is the stream itself, not a server log file: the browser is never given shell or file access.">
          Event log
        </SectionTitle>
        <Panel bodyClassName="p-0">
          {stream.events.length === 0 ? (
            <EmptyPanel title="No events in this session"
                        hint="The stream stays connected while idle; events appear as soon as a run emits a stage." />
          ) : (
            <ul className="max-h-96 divide-y divide-border/60 overflow-y-auto text-[11px]">
              {[...stream.events].reverse().slice(0, 100).map((e) => {
                const meta = STAGE_STATUS_META[e.status];
                return (
                  <li key={e.seq} className="flex items-baseline gap-2 px-3 py-1.5">
                    <span className="nums w-10 shrink-0 text-fg-subtle">{e.seq}</span>
                    <span className="nums w-16 shrink-0 text-fg-subtle">{clockTime(e.ts)}</span>
                    <span className="nums w-28 shrink-0 truncate text-fg-subtle">{e.run_id}</span>
                    <span className="size-1.5 shrink-0 rounded-full" style={{ background: meta.color }} />
                    <span className="w-36 shrink-0 truncate text-fg-muted">
                      {STAGE_LABELS[e.stage] ?? e.stage}
                      {e.cycle !== null && <span className="text-fg-subtle"> · c{e.cycle}</span>}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-fg">{e.summary || meta.label}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>
        {stream.lastError && stream.state !== "open" && (
          <p className="mt-1.5 text-[11px] text-warn">{stream.lastError}</p>
        )}
      </section>
    </div>
  );
}

interface ProviderRow { provider: string; model: string; count: number; degraded: boolean }

/** Counts provider/model pairs seen on stage provenance. Reads names only. */
function providerTally(events: StageEvent[]): ProviderRow[] {
  const map = new Map<string, ProviderRow>();
  const add = (p: Provenance | undefined) => {
    if (!p?.provider && !p?.model) return;
    const key = `${p.provider ?? "?"}|${p.model ?? "?"}`;
    const row = map.get(key) ?? {
      provider: p.provider ?? "unknown", model: p.model ?? "unknown", count: 0, degraded: false,
    };
    row.count += 1;
    row.degraded = row.degraded || !!p.degraded;
    map.set(key, row);
  };
  for (const e of events) {
    const payload = e.payload as { provenance?: Provenance; findings?: { provenance?: Provenance }[] };
    add(payload.provenance);
    for (const f of payload.findings ?? []) add(f.provenance);
  }
  return [...map.values()].sort((a, b) => b.count - a.count);
}
