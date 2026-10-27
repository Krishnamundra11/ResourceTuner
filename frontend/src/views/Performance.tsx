/** Performance & Evaluation — did the change actually help?
 *
 *  The Evaluation Engine is deterministic: for a discrete metric it compares the measured
 *  after-duration against the before-duration with a ±10% band, and for a continuous metric it
 *  compares PSI with a ±15% band. Anything outside the improving side of that band is a
 *  DEGRADED verdict and the action is rolled back automatically. This view reports those
 *  verdicts; it does not recompute them, and it never averages a verdict into a score.
 *
 *  Where a run recorded no measurement — because no action was applied, so there was nothing to
 *  measure — the cycle is shown as such rather than as a zero. */

import { useMemo } from "react";
import { Link } from "react-router-dom";

import { DivergingBars, GroupedBars, SERIES_1, SERIES_2 } from "@/components/charts";
import { PayloadView } from "@/components/PayloadView";
import { KeyValue, PageHeader, Panel, SectionTitle, StatTile } from "@/components/primitives";
import { RunPicker, useRunSelection } from "@/components/RunPicker";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "@/components/StateViews";
import { Badge } from "@/components/ui/badge";
import { Table, Td, Th } from "@/components/ui/misc";
import { cssVar } from "@/components/charts";
import { DASH, num, seconds, signedPct } from "@/lib/format";
import { verdictTone } from "@/lib/status";
import type { EvaluationPayload, StageEvent } from "@/lib/types";

interface CycleRow {
  cycle: number;
  before: number | null;
  after: number | null;
  reference: number | null;
  verdict: string | null;
  improvement: number | null;
  rolledBack: boolean | null;
  detail: string | null;
  evaluation: EvaluationPayload | null;
  /** Why there is no measurement, when there is none. */
  note: string | null;
}

function buildRecord(e: StageEvent | undefined): { duration: number | null; reference: number | null } {
  const p = (e?.payload ?? {}) as { record?: { duration_s?: number } | null; reference_baseline_s?: number };
  return {
    duration: typeof p.record?.duration_s === "number" ? p.record.duration_s : null,
    reference: typeof p.reference_baseline_s === "number" ? p.reference_baseline_s : null,
  };
}

export default function Performance() {
  const sel = useRunSelection();

  const rows = useMemo<CycleRow[]>(() => {
    const byCycle = new Map<number, Map<string, StageEvent>>();
    for (const e of sel.events) {
      if (e.cycle === null) continue;
      const m = byCycle.get(e.cycle) ?? new Map<string, StageEvent>();
      const prev = m.get(e.stage);
      if (!prev || e.seq > prev.seq) m.set(e.stage, e);
      byCycle.set(e.cycle, m);
    }
    return [...byCycle.entries()].sort(([a], [b]) => a - b).map(([cycle, m]) => {
      const before = buildRecord(m.get("baseline"));
      const after = buildRecord(m.get("post_measure"));
      const evEvent = m.get("evaluation");
      const ev = (evEvent?.payload ?? null) as EvaluationPayload | null;
      const skipped = evEvent?.status === "skipped";
      return {
        cycle,
        before: before.duration,
        after: after.duration,
        reference: before.reference,
        verdict: ev?.verdict ?? null,
        improvement: typeof ev?.improvement_pct === "number" ? ev.improvement_pct : null,
        rolledBack: ev?.rolled_back ?? null,
        detail: ev?.detail ?? null,
        evaluation: ev,
        note: skipped ? (evEvent?.summary || "no action was applied, so nothing was measured") : null,
      };
    });
  }, [sel.events]);

  const measured = rows.filter((r) => r.before !== null && r.after !== null);
  const verdicts = rows.filter((r) => r.verdict);
  const improvedCount = verdicts.filter((r) => r.verdict === "IMPROVED").length;
  const rolledBackCount = verdicts.filter((r) => r.rolledBack).length;
  const best = verdicts.reduce<CycleRow | null>(
    (acc, r) => (r.improvement !== null && (acc === null || acc.improvement === null || r.improvement > acc.improvement) ? r : acc),
    null,
  );

  return (
    <div className="space-y-4">
      <PageHeader
        title="Performance & Evaluation"
        subtitle="Measured build durations before and after each applied action, and the deterministic verdict the Evaluation Engine reached from them. A cycle with no applied action has nothing to measure and is shown that way."
        actions={<RunPicker sel={sel} />}
      />

      {sel.error ? <ErrorPanel error={sel.error} /> : null}

      {!sel.run ? (
        <EmptyPanel title="No runs recorded yet"
                    hint={<>Start one from <Link className="text-accent hover:underline" to="/live-lab">Live Lab</Link>.</>} />
      ) : sel.isPending ? (
        <LoadingPanel rows={6} />
      ) : rows.length === 0 ? (
        <Panel><EmptyPanel title="This run recorded no cycles" /></Panel>
      ) : (
        <>
          <section className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile label="Cycles with a verdict" value={String(verdicts.length)}
                      foot={`${rows.length} cycle(s) in this run`} />
            <StatTile label="Improved" value={verdicts.length ? `${improvedCount}/${verdicts.length}` : DASH}
                      tone={improvedCount > 0 ? "ok" : "muted"}
                      hint="A discrete verdict is IMPROVED only when the after-duration beats the before-duration by more than 10%." />
            <StatTile label="Rolled back" value={verdicts.length ? String(rolledBackCount) : DASH}
                      tone={rolledBackCount > 0 ? "warn" : "muted"}
                      foot="automatic on DEGRADED, TIMEOUT or ERROR" />
            <StatTile label="Best improvement"
                      value={best?.improvement == null ? DASH : signedPct(best.improvement, 2)}
                      tone={best?.improvement != null && best.improvement > 0 ? "ok" : "muted"}
                      foot={best ? `cycle ${best.cycle}` : undefined} />
          </section>

          <section className="grid gap-3 lg:grid-cols-2">
            <Panel title="Build duration, before vs after"
                   hint="Both bars are seconds, so they share one axis. The warm-up build of each cycle is discarded and is not shown.">
              {measured.length === 0 ? (
                <EmptyPanel title="No cycle has both measurements"
                            hint="A before-and-after pair exists only for a cycle in which the Gate approved an action." />
              ) : (
                <GroupedBars
                  label="Measured build duration before and after the applied action, per cycle"
                  unit="s"
                  categories={measured.map((r) => `C${r.cycle}`)}
                  groups={[
                    { name: "Before", color: SERIES_1(), values: measured.map((r) => r.before) },
                    { name: "After", color: SERIES_2(), values: measured.map((r) => r.after) },
                  ]}
                />
              )}
            </Panel>

            <Panel title="Improvement per cycle"
                   hint="Positive means the build got faster. The number is printed on every bar, so the sign never rests on colour alone.">
              {verdicts.length === 0 ? (
                <EmptyPanel title="No verdicts recorded" />
              ) : (
                <DivergingBars
                  label="Percentage improvement per cycle as judged by the Evaluation Engine"
                  unit="%"
                  categories={verdicts.map((r) => `C${r.cycle}`)}
                  values={verdicts.map((r) => r.improvement)}
                  colorFor={(_, i) => {
                    const v = verdicts[i].verdict;
                    return v === "IMPROVED" ? cssVar("--color-ok", "#55c975")
                      : v === "UNCHANGED" ? cssVar("--color-warn", "#f0b135")
                        : cssVar("--color-bad", "#f75d59");
                  }}
                />
              )}
            </Panel>
          </section>

          <section>
            <SectionTitle>Cycle by cycle</SectionTitle>
            <Panel bodyClassName="p-0">
              <Table>
                <thead>
                  <tr>
                    <Th>Cycle</Th><Th>Verdict</Th>
                    <Th className="text-right">Before</Th><Th className="text-right">After</Th>
                    <Th className="text-right">Delta</Th><Th className="text-right">Reference</Th>
                    <Th>Resolution</Th><Th>Detail</Th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.cycle} className="border-t border-border/60">
                      <Td className="nums font-medium text-fg">{r.cycle}</Td>
                      <Td>
                        {r.verdict
                          ? <Badge tone={verdictTone(r.verdict)}>{r.verdict}</Badge>
                          : <span className="text-fg-subtle">not evaluated</span>}
                      </Td>
                      <Td className="nums text-right">{r.before === null ? DASH : seconds(r.before)}</Td>
                      <Td className="nums text-right">{r.after === null ? DASH : seconds(r.after)}</Td>
                      <Td className="nums text-right">{r.improvement === null ? DASH : signedPct(r.improvement, 2)}</Td>
                      <Td className="nums text-right text-fg-subtle">{r.reference === null ? DASH : seconds(r.reference)}</Td>
                      <Td>
                        {r.rolledBack === null ? <span className="text-fg-subtle">{DASH}</span>
                          : r.rolledBack ? <Badge tone="warn">rolled back</Badge>
                            : <Badge tone="ok">kept</Badge>}
                      </Td>
                      <Td className="max-w-[22rem] text-fg-subtle">{r.detail ?? r.note ?? DASH}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Panel>
            <p className="mt-1.5 text-[11px] leading-relaxed text-fg-subtle">
              The reference column is the warm-cache baseline the run was configured with. It is
              context for reading the before-value, not the comparison the verdict is based on —
              the verdict compares this cycle's own before and after.
            </p>
          </section>

          {sel.cycle !== null && (
            <section>
              <SectionTitle>Evaluation record for cycle {sel.cycle}</SectionTitle>
              <Panel>
                {(() => {
                  const r = rows.find((x) => x.cycle === sel.cycle);
                  if (!r) return <EmptyPanel title="No record for this cycle" />;
                  if (!r.evaluation) {
                    return <p className="text-xs text-fg-subtle">{r.note ?? "Nothing was evaluated in this cycle."}</p>;
                  }
                  return (
                    <div className="space-y-3">
                      <KeyValue columns={2} rows={[
                        { k: "Verdict", v: r.verdict ?? DASH },
                        { k: "Metric", v: String((r.evaluation as Record<string, unknown>).metric_name ?? DASH) },
                        { k: "Baseline value", v: num((r.evaluation as Record<string, number>).baseline_value, 2) },
                        { k: "Measured value", v: num((r.evaluation as Record<string, number>).measured_value, 2) },
                        { k: "Improvement", v: r.improvement === null ? DASH : signedPct(r.improvement, 2) },
                        { k: "Controller state", v: String((r.evaluation as Record<string, unknown>).final_controller_state ?? DASH) },
                      ]} />
                      <PayloadView value={r.evaluation} label="Evaluation record as written" />
                    </div>
                  );
                })()}
              </Panel>
            </section>
          )}
        </>
      )}
    </div>
  );
}
