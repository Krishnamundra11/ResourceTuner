/** Safety & Actions — what the Gate decided, and what the controller actually did.
 *
 *  The Safety Gate is the only thing in the system that can approve a resource change. It runs
 *  ten deterministic checks and rejects if any one of them fails; a check that raises also
 *  rejects. This view shows the Gate's verdict for the selected cycle and the full action
 *  ledger from data/actions.db, which records every apply, rollback and failure to roll back. */

import { useMemo } from "react";
import { Link } from "react-router-dom";
import { ShieldCheck } from "lucide-react";

import { PayloadView } from "@/components/PayloadView";
import { KeyValue, PageHeader, Panel, ReadOnlyNote, SectionTitle } from "@/components/primitives";
import { RunPicker, useRunSelection } from "@/components/RunPicker";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "@/components/StateViews";
import { Badge } from "@/components/ui/badge";
import { Table, Td, Th } from "@/components/ui/misc";
import { Hint } from "@/components/ui/tooltip";
import { latestStageStatus } from "@/hooks/useEventStream";
import { useHistoryActions } from "@/hooks/useHistory";
import { DASH, dateTime, relativeTime } from "@/lib/format";
import { actionStatusTone, executionTone, STAGE_STATUS_META } from "@/lib/status";
import type { ExecutionPayload } from "@/lib/types";

/** The ten checks, in the order gate.py runs them, each with what it protects against. */
const CHECKS: { id: string; label: string; why: string }[] = [
  { id: "allowed_action", label: "Allowed action", why: "The tool must be on the enabled surface, and may only change the one knob it is bound to." },
  { id: "allowed_target", label: "Allowed target", why: "The target id must match a real managed container that is running or paused." },
  { id: "reversible", label: "Reversible", why: "The exact prior value must be readable, so the action can be undone to that value and not to a default." },
  { id: "bounds", label: "Bounds", why: "The new value must sit inside the configured floor and ceiling for that knob." },
  { id: "jump_size", label: "Jump size", why: "A single step may not move the knob further than the configured maximum." },
  { id: "foreground", label: "Foreground protection", why: "A foreground workload — a build or a test run — may never be deprioritised." },
  { id: "pause", label: "Pause policy", why: "Pausing is permitted only for the background writers it is explicitly allowed for." },
  { id: "cooldown", label: "Cooldown", why: "A target may not be changed again until its cooldown has elapsed." },
  { id: "in_flight", label: "In flight", why: "A target with an unresolved action may not receive another one." },
  { id: "degraded_source", label: "Degraded source", why: "An action whose diagnosis came from the degraded local model is refused outright." },
];

export default function SafetyActions() {
  const sel = useRunSelection();
  const actions = useHistoryActions({ limit: 100 });
  const stages = useMemo(() => latestStageStatus(sel.events, sel.cycle), [sel.events, sel.cycle]);

  const gate = stages.get("safety_gate");
  const controller = stages.get("controller");
  const ex = (gate?.payload ?? {}) as ExecutionPayload;

  // The Gate reports failures as "check_name: reason". Everything else passed, but only when a
  // verdict was actually reached — "no action" and "blocked" never reach the checks at all.
  const failures = useMemo(() => {
    const map = new Map<string, string>();
    for (const r of ex.reasons ?? []) {
      const i = r.indexOf(":");
      if (i > 0) map.set(r.slice(0, i).trim(), r.slice(i + 1).trim());
    }
    return map;
  }, [ex.reasons]);

  const evaluated = ex.status === "applied" || ex.status === "rejected";

  return (
    <div className="space-y-4">
      <PageHeader
        title="Safety & Actions"
        subtitle="The deterministic Safety Gate and the action ledger it guards. No agent can apply a change; an agent can only propose one, and a proposal that fails any single check is rejected."
        actions={<RunPicker sel={sel} />}
      />

      {sel.error ? <ErrorPanel error={sel.error} /> : null}

      <section className="grid gap-3 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
        <Panel
          title={<span className="flex items-center gap-1.5"><ShieldCheck className="size-3.5" /> Gate verdict</span>}
          hint="resourcetuner/safety/gate.py. All ten checks must pass; a check that raises an exception rejects rather than approves."
          right={gate ? <Badge tone={STAGE_STATUS_META[gate.status].tone}>{STAGE_STATUS_META[gate.status].label}</Badge> : null}
        >
          {!sel.run ? (
            <EmptyPanel title="No runs recorded yet"
                        hint={<>Start one from <Link className="text-accent hover:underline" to="/live-lab">Live Lab</Link>.</>} />
          ) : sel.isPending ? (
            <LoadingPanel rows={5} />
          ) : !gate ? (
            <EmptyPanel title="The Gate did not report in this cycle" />
          ) : (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={executionTone(ex.status)}>{ex.status ?? DASH}</Badge>
                {ex.tool && <span className="nums text-xs text-fg">{ex.tool}</span>}
                {ex.target_id && <span className="nums text-xs text-fg-muted">{ex.target_id}</span>}
              </div>
              <p className="text-xs leading-relaxed text-fg-muted">{gate.summary}</p>

              {!evaluated && (
                <p className="rounded-md border border-border bg-bg-raised px-2.5 py-2 text-[11px] text-fg-subtle">
                  {ex.status === "blocked"
                    ? "The cycle ran in degraded mode, so the action was blocked before it ever reached the Gate. The checks below were not evaluated."
                    : "No action was proposed this cycle, so there was nothing for the Gate to evaluate."}
                </p>
              )}

              <ul className="space-y-1">
                {CHECKS.map((c) => {
                  const failed = failures.get(c.id);
                  const state = !evaluated ? "not evaluated" : failed ? "FAIL" : "pass";
                  const tone = !evaluated ? "neutral" : failed ? "bad" : "ok";
                  return (
                    <li key={c.id} className="flex items-start gap-2 border-b border-border/50 pb-1">
                      <Badge tone={tone}>{state}</Badge>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="text-[11px] font-medium text-fg">{c.label}</span>
                          <Hint text={c.why}><span className="cursor-help text-[11px] text-fg-subtle">ⓘ</span></Hint>
                        </div>
                        {failed && <p className="mt-0.5 text-[11px] leading-snug text-bad">{failed}</p>}
                      </div>
                    </li>
                  );
                })}
              </ul>
              <PayloadView value={gate.payload} label="Gate result as recorded" />
            </div>
          )}
        </Panel>

        <Panel title="Controller" hint="The write is verified by reading the cgroup file back; a value that did not take effect is not reported as applied.">
          {!controller ? (
            <EmptyPanel title="Nothing recorded for this cycle" />
          ) : (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <Badge tone={STAGE_STATUS_META[controller.status].tone}>
                  {STAGE_STATUS_META[controller.status].label}
                </Badge>
                <span className="text-xs text-fg-muted">{controller.summary}</span>
              </div>
              <KeyValue rows={[
                { k: "Knob", v: ex.knob ?? DASH },
                { k: "Previous value", v: fmtValue(ex.old_value) },
                { k: "Requested value", v: fmtValue(ex.new_value) },
                { k: "Observed after write", v: fmtValue(ex.observed ?? ex.new_value),
                  hint: "Read back from the cgroup file after the write." },
                { k: "Action id", v: ex.action_id ?? DASH },
              ]} />
              <PayloadView value={controller.payload} label="Controller result as recorded" />
            </div>
          )}
        </Panel>
      </section>

      <section>
        <SectionTitle
          hint="data/actions.db — the write-ahead ledger. A row is written before the change is applied, so a crash leaves a record to roll back from."
          right={actions.data ? <span className="text-[11px] text-fg-subtle">{actions.data.total} recorded</span> : null}
        >
          Action ledger
        </SectionTitle>
        <Panel bodyClassName="p-0">
          {actions.isPending ? (
            <LoadingPanel rows={4} />
          ) : actions.error ? (
            <ErrorPanel error={actions.error} onRetry={() => void actions.refetch()} />
          ) : !actions.data?.actions.length ? (
            <EmptyPanel title="No actions have ever been applied"
                        hint="The ledger fills the first time the Gate approves a change." />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Status</Th><Th>Tool</Th><Th>Target</Th><Th>Knob</Th>
                  <Th className="text-right">From</Th><Th className="text-right">To</Th>
                  <Th>Created</Th><Th>Resolved</Th><Th>Detail</Th>
                </tr>
              </thead>
              <tbody>
                {actions.data.actions.map((a) => (
                  <tr key={a.action_id} className="border-t border-border/60">
                    <Td><Badge tone={actionStatusTone(a.status)}>{a.status}</Badge></Td>
                    <Td className="text-fg">{a.tool}</Td>
                    <Td className="nums text-fg-muted">{a.container_name || a.target_id}</Td>
                    <Td className="text-fg-subtle">{a.knob}</Td>
                    <Td className="nums text-right">{fmtValue(a.old_value)}</Td>
                    <Td className="nums text-right">{fmtValue(a.new_value)}</Td>
                    <Td className="text-fg-subtle">{dateTime(a.created_at)}</Td>
                    <Td className="text-fg-subtle">
                      {a.resolved_at ? relativeTime(a.resolved_at) : <span className="text-warn">unresolved</span>}
                    </Td>
                    <Td className="max-w-[18rem] truncate text-fg-subtle">{a.detail ?? DASH}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Panel>
      </section>

      <ReadOnlyNote>
        This view reads the ledger; it cannot write to it. Rollback is not a button here by
        design — an action is resolved by the Evaluation Engine, which rolls it back automatically
        on a DEGRADED, TIMEOUT or ERROR verdict, and by the run's cleanup, which rolls back
        anything still unresolved when a run ends or is stopped.
      </ReadOnlyNote>
    </div>
  );
}

function fmtValue(v: unknown): string {
  if (v === null) return "unlimited";
  if (v === undefined) return DASH;
  if (typeof v === "number") return String(v);
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}
