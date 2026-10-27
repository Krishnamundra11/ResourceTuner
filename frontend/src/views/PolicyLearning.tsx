/** Policy Learning — what the system has learned, and what it has been allowed to keep.
 *
 *  The split matters here. The Policy Store is deterministic: it counts successes and failures
 *  per (workload, condition, action) and keeps an EMA of the improvement with α = 0.3. The
 *  promotion floor is deterministic too — at least 10 samples, at least an 80% success rate, and
 *  a positive average improvement. Only a record that already clears that floor is ever shown to
 *  the Meta-Agent, whose single job is to judge whether clearing it is actually meaningful.
 *
 *  So a record can be "eligible but not promoted": the numbers qualified and the Meta-Agent
 *  declined. That is a real state, and this view shows it as one. */

import { useMemo } from "react";

import { CategoryBars } from "@/components/charts";
import { PayloadView } from "@/components/PayloadView";
import { PageHeader, Panel, ReadOnlyNote, SectionTitle, StatTile } from "@/components/primitives";
import { RunPicker, useRunSelection } from "@/components/RunPicker";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "@/components/StateViews";
import { Badge } from "@/components/ui/badge";
import { Table, Td, Th } from "@/components/ui/misc";
import { Hint } from "@/components/ui/tooltip";
import { latestStageStatus } from "@/hooks/useEventStream";
import { useHistoryPolicies } from "@/hooks/useHistory";
import { DASH, num, pct, relativeTime, signedPct } from "@/lib/format";
import { STAGE_STATUS_META, verdictTone, workloadColor } from "@/lib/status";
import type { MetaPayload, PolicyRecord } from "@/lib/types";

export default function PolicyLearning() {
  const policies = useHistoryPolicies();
  const sel = useRunSelection();
  const stages = useMemo(() => latestStageStatus(sel.events, sel.cycle), [sel.events, sel.cycle]);

  const policyStage = stages.get("policy");
  const metaStage = stages.get("meta");
  const meta = (metaStage?.payload ?? {}) as MetaPayload;

  const floor = policies.data?.promotion_floor;
  const rows = useMemo(() => policies.data?.policies ?? [], [policies.data]);
  const promoted = rows.filter((r) => r.promoted);

  const eligible = useMemo(
    () => (floor ? rows.filter((r) => !r.promoted && clearsFloor(r, floor)) : []),
    [rows, floor],
  );

  return (
    <div className="space-y-4">
      <PageHeader
        title="Policy Learning"
        subtitle="The learned policy store and the promotion decisions made from it. Counting and the promotion floor are deterministic; the Meta-Agent only judges whether a record that already clears the floor deserves to become a default."
        actions={<RunPicker sel={sel} />}
      />

      {policies.error && <ErrorPanel error={policies.error} onRetry={() => void policies.refetch()} />}

      <section className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile label="Policy records" value={policies.isPending ? DASH : String(rows.length)}
                  foot="one per workload / condition / action" />
        <StatTile label="Promoted" value={policies.isPending ? DASH : String(promoted.length)}
                  tone={promoted.length ? "ok" : "muted"}
                  hint="A promoted record is the system's learned default for that situation." />
        <StatTile label="Eligible, not promoted" value={policies.isPending ? DASH : String(eligible.length)}
                  tone={eligible.length ? "warn" : "muted"}
                  hint="The deterministic floor was cleared, but the Meta-Agent has not promoted the record." />
        <StatTile label="Promotion floor"
                  value={floor ? `${floor.min_samples} / ${pct(floor.min_success_rate * 100, 0)}` : DASH}
                  hint={floor
                    ? `At least ${floor.min_samples} samples, at least a ${pct(floor.min_success_rate * 100, 0)} success rate${floor.requires_positive_improvement ? ", and a positive average improvement" : ""}. Checked in code before any model is asked.`
                    : undefined}
                  foot="samples / success rate" />
      </section>

      <section>
        <SectionTitle hint="data/policy.db. avg_improvement is an exponential moving average with α = 0.3, so a recent outcome moves it more than an old one.">
          Learned policies
        </SectionTitle>
        <Panel bodyClassName="p-0">
          {policies.isPending ? (
            <LoadingPanel rows={4} />
          ) : rows.length === 0 ? (
            <EmptyPanel title="Nothing has been learned yet"
                        hint="A record appears the first time an applied action is evaluated." />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Workload</Th><Th>Condition</Th><Th>Action</Th><Th>Target</Th><Th>Parameter</Th>
                  <Th className="text-right">Samples</Th><Th className="text-right">Success</Th>
                  <Th className="text-right">EMA improvement</Th><Th>Last outcome</Th><Th>State</Th>
                  <Th>Updated</Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={`${r.workload}-${r.condition}-${r.action_tool}-${r.action_target}-${i}`}
                      className="border-t border-border/60">
                    <Td>
                      <span className="flex items-center gap-1.5">
                        <span className="size-2 rounded-sm" style={{ background: workloadColor(r.workload) }} />
                        {r.workload.replace(/_/g, " ")}
                      </span>
                    </Td>
                    <Td className="text-fg-subtle">{r.condition}</Td>
                    <Td className="text-fg">{r.action_tool}</Td>
                    <Td className="nums text-fg-muted">{r.action_target}</Td>
                    <Td className="nums text-fg-muted">{fmtParam(r.action_param)}</Td>
                    <Td className="nums text-right">{r.sample_count}</Td>
                    <Td className="nums text-right">
                      {r.success_rate === null ? DASH : pct(r.success_rate * 100, 0)}
                      <span className="text-fg-subtle"> ({r.success_count}/{r.sample_count})</span>
                    </Td>
                    <Td className="nums text-right">
                      {r.avg_improvement === null ? DASH : signedPct(r.avg_improvement, 2)}
                    </Td>
                    <Td>{r.last_outcome
                      ? <Badge tone={verdictTone(r.last_outcome)}>{r.last_outcome}</Badge>
                      : <span className="text-fg-subtle">{DASH}</span>}</Td>
                    <Td>
                      {r.promoted ? <Badge tone="ok">promoted</Badge>
                        : floor && clearsFloor(r, floor)
                          ? <Hint text="The numbers clear the deterministic floor. The Meta-Agent has not promoted this record.">
                              <Badge tone="warn">eligible</Badge>
                            </Hint>
                          : <Badge tone="idle">learning</Badge>}
                    </Td>
                    <Td className="text-fg-subtle">{relativeTime(r.last_updated)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Panel>
      </section>

      {rows.length > 0 && (
        <section className="grid gap-3 lg:grid-cols-2">
          <Panel title="Samples per policy"
                 hint="How much evidence each record rests on. The promotion floor is drawn from this count, not from the success rate alone.">
            <CategoryBars
              label="Number of recorded samples per learned policy"
              unit=""
              bars={rows.slice(0, 10).map((r) => ({
                key: `${r.action_tool}-${r.action_target}`,
                label: `${r.workload} · ${r.action_tool.replace("set_container_", "")}`,
                value: r.sample_count,
                color: workloadColor(r.workload),
              }))}
            />
          </Panel>
          <Panel title="Average improvement per policy"
                 hint="The EMA of the improvement each policy achieved. A record with few samples can still show a large number — read it alongside the sample count.">
            <CategoryBars
              label="Exponential moving average of improvement per learned policy"
              bars={rows.slice(0, 10).map((r) => ({
                key: `${r.action_tool}-${r.action_target}-imp`,
                label: `${r.workload} · ${r.action_tool.replace("set_container_", "")}`,
                value: r.avg_improvement,
                color: workloadColor(r.workload),
              }))}
            />
          </Panel>
        </section>
      )}

      <section className="grid gap-3 lg:grid-cols-2">
        <Panel title="Policy write for this cycle"
               hint="The deterministic store update the Evaluation Engine's verdict produced.">
          {sel.isPending ? <LoadingPanel rows={3} />
            : !policyStage ? <EmptyPanel title="Nothing recorded for this cycle" />
              : (
                <div className="space-y-2">
                  <div className="flex items-center gap-2">
                    <Badge tone={STAGE_STATUS_META[policyStage.status].tone}>
                      {STAGE_STATUS_META[policyStage.status].label}
                    </Badge>
                    <span className="text-xs text-fg-muted">{policyStage.summary}</span>
                  </div>
                  <PayloadView value={policyStage.payload} label="Policy record as written" />
                </div>
              )}
        </Panel>

        <Panel title="Meta-Agent — promotion reasoning"
               hint="Google ADK. It is only consulted once the deterministic floor is already cleared, so its judgement is the last gate on a promotion, never the first.">
          {sel.isPending ? <LoadingPanel rows={3} />
            : !metaStage ? <EmptyPanel title="Nothing recorded for this cycle" />
              : (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={STAGE_STATUS_META[metaStage.status].tone}>
                      {STAGE_STATUS_META[metaStage.status].label}
                    </Badge>
                    {meta.promote !== undefined && (
                      <Badge tone={meta.promote ? "ok" : "idle"}>
                        {meta.promote ? "promoted" : "not promoted"}
                      </Badge>
                    )}
                  </div>
                  {metaStage.status === "skipped" && (
                    <p className="text-[11px] leading-relaxed text-fg-subtle">
                      The Meta-Agent was not called: {metaStage.summary}. Skipping it is the normal
                      case — asking a model to reason about a record that cannot be promoted anyway
                      would spend a call for nothing.
                    </p>
                  )}
                  {meta.reasoning && <p className="text-xs leading-relaxed text-fg-muted">{meta.reasoning}</p>}
                  <PayloadView value={metaStage.payload} label="Meta decision as recorded" />
                </div>
              )}
        </Panel>
      </section>

      {sel.error ? <ErrorPanel error={sel.error} /> : null}

      <ReadOnlyNote>
        Promotion is written by the pipeline, not from this interface. There is no endpoint that
        promotes, demotes or edits a policy from the browser, so nothing here can change what the
        system has learned.
      </ReadOnlyNote>
    </div>
  );
}

/** Mirrors the deterministic floor the backend reports; displayed, not re-decided. */
function clearsFloor(
  r: PolicyRecord,
  floor: { min_samples: number; min_success_rate: number; requires_positive_improvement: boolean },
): boolean {
  if (r.sample_count < floor.min_samples) return false;
  if ((r.success_rate ?? 0) < floor.min_success_rate) return false;
  if (floor.requires_positive_improvement && !((r.avg_improvement ?? 0) > 0)) return false;
  return true;
}

function fmtParam(p: PolicyRecord["action_param"]): string {
  if (typeof p === "string") return p;
  const entries = Object.entries(p ?? {});
  if (!entries.length) return DASH;
  return entries.map(([k, v]) => `${k} ${num(v, k === "cpus" ? 2 : 0)}`).join(", ");
}
