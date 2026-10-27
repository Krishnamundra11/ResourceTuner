/** Diagnosis & Explainability — why the system believed what it believed.
 *
 *  Three layers, kept visibly separate because the project's design rule depends on it:
 *
 *  1. the deterministic classifier's own numbers (no model involved);
 *  2. the ADK Supervisor's delegation — which specialists it decided were worth asking;
 *  3. the CrewAI crew's findings, each with the evidence it cited, the deterministic checker's
 *     verdict on those citations, and which provider actually answered.
 *
 *  A finding that failed its checks, or that only a degraded local model could answer, is shown
 *  with its diagnosis intact and marked as carrying no action — that is the rule the backend
 *  enforces, and this view reports it rather than restating it. */

import { useMemo } from "react";
import { Link } from "react-router-dom";
import { CircleAlert, ShieldOff } from "lucide-react";

import { PayloadView } from "@/components/PayloadView";
import { KeyValue, PageHeader, Panel, SectionTitle } from "@/components/primitives";
import { RunPicker, useRunSelection } from "@/components/RunPicker";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "@/components/StateViews";
import { Badge } from "@/components/ui/badge";
import { Table, Td, Th } from "@/components/ui/misc";
import { Hint } from "@/components/ui/tooltip";
import { latestStageStatus } from "@/hooks/useEventStream";
import { DASH, num, pct } from "@/lib/format";
import { STAGE_STATUS_META, workloadColor } from "@/lib/status";
import type {
  ClassifyPayload, DecisionPayload, Delegation, Finding, Provenance,
} from "@/lib/types";

const AGENT_LABEL: Record<string, string> = { cpu: "CPU Agent", ram: "RAM Agent", io: "I/O Agent" };

export default function Diagnosis() {
  const sel = useRunSelection();
  const stages = useMemo(() => latestStageStatus(sel.events, sel.cycle), [sel.events, sel.cycle]);

  const classify = stages.get("classify");
  const supervisor = stages.get("supervisor");
  const diagnostics = stages.get("diagnostics");
  const decision = stages.get("decision");

  const cls = (classify?.payload ?? {}) as ClassifyPayload;
  const delegation = ((supervisor?.payload ?? {}) as { delegation?: Delegation }).delegation ?? {};
  const degraded = ((supervisor?.payload ?? {}) as { degraded?: boolean }).degraded ?? false;
  const findings = (((diagnostics?.payload ?? {}) as { findings?: Finding[] }).findings ?? []);
  const dec = (decision?.payload ?? {}) as DecisionPayload;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Diagnosis & Explainability"
        subtitle="The reasoning layer, separated from the measurement layer. The classifier's numbers are computed; the agents' diagnoses are checked against those numbers before anything is allowed to become an action."
        actions={<RunPicker sel={sel} />}
      />

      {sel.error ? <ErrorPanel error={sel.error} /> : null}

      {!sel.run ? (
        <EmptyPanel title="No runs recorded yet"
                    hint={<>Start one from <Link className="text-accent hover:underline" to="/live-lab">Live Lab</Link>.</>} />
      ) : sel.isPending ? (
        <LoadingPanel rows={6} />
      ) : (
        <>
          {degraded && <DegradedBanner />}

          {/* 1 — deterministic */}
          <section className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
            <Panel title="Deterministic classification"
                   hint="resourcetuner/classifier/rules.py. Confidence is min(dominance, persistence); both are measured ratios over the sampling window, not a model's estimate.">
              {!classify ? (
                <EmptyPanel title="Not recorded for this cycle" />
              ) : (
                <div className="space-y-3">
                  <div className="flex items-center gap-2">
                    <span className="size-2.5 rounded-sm" style={{ background: workloadColor(cls.workload?.class) }} />
                    <span className="text-base font-semibold text-fg">
                      {(cls.workload?.class ?? "UNKNOWN").replace(/_/g, " ")}
                    </span>
                  </div>
                  <KeyValue rows={[
                    { k: "Confidence", v: cls.workload?.confidence == null ? DASH : pct(cls.workload.confidence * 100, 0) },
                    { k: "Dominance", v: cls.workload?.dominance == null ? DASH : pct(cls.workload.dominance * 100, 1) },
                    { k: "Persistence", v: cls.workload?.persistence == null ? DASH : pct(cls.workload.persistence * 100, 1) },
                    { k: "LLM fallback needed", v: cls.workload?.needs_llm_fallback ? "yes" : "no",
                      hint: "The mechanical fallback rule: low confidence, conflicting foreground signals, or no signature match." },
                  ]} />
                  {cls.workload?.fallback_reasons?.length ? (
                    <ul className="list-inside list-disc space-y-0.5 text-[11px] text-warn">
                      {cls.workload.fallback_reasons.map((r) => <li key={r}>{r}</li>)}
                    </ul>
                  ) : null}
                  {cls.host && (
                    <div>
                      <SectionTitle>Host at the moment of classification</SectionTitle>
                      <KeyValue columns={2} rows={Object.entries(cls.host).map(([k, v]) => ({
                        k: k.replace(/_/g, " "), v: v === null ? DASH : num(v, 2),
                      }))} />
                    </div>
                  )}
                </div>
              )}
            </Panel>

            <Panel title="Targets the agents were shown"
                   hint="The evidence bundle: only these targets, and only these fields, were given to the crew. An agent that cites a number outside this set fails its check."
                   bodyClassName="p-0">
              {!cls.targets?.length ? (
                <EmptyPanel title="No targets recorded" />
              ) : (
                <Table>
                  <thead>
                    <tr><Th>Target</Th><Th>Workload</Th><Th className="text-right">CPU</Th>
                        <Th className="text-right">cpu.weight</Th><Th>State</Th></tr>
                  </thead>
                  <tbody>
                    {cls.targets.map((t) => (
                      <tr key={t.target_id} className="border-t border-border/60">
                        <Td className="font-medium text-fg">{t.target_id}</Td>
                        <Td>
                          <span className="flex items-center gap-1.5">
                            <span className="size-2 rounded-sm" style={{ background: workloadColor(t.workload) }} />
                            {(t.workload ?? DASH).replace(/_/g, " ")}
                          </span>
                        </Td>
                        <Td className="nums text-right">{pct(t.cpu_percent, 1)}</Td>
                        <Td className="nums text-right">{t.cpu_weight ?? DASH}</Td>
                        <Td className="text-fg-subtle">{t.state ?? DASH}</Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              )}
            </Panel>
          </section>

          {/* 2 — supervisor */}
          <section>
            <SectionTitle hint="Google ADK. The Supervisor decides which specialists are worth asking, then calls the entire CrewAI crew as one step — it does not know a crew runs underneath.">
              ADK Supervisor — delegation
            </SectionTitle>
            <Panel>
              {!supervisor ? (
                <EmptyPanel title="Not recorded for this cycle" />
              ) : (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    {(delegation.resources ?? []).length === 0
                      ? <Badge tone="idle">No specialist consulted</Badge>
                      : delegation.resources!.map((r) => (
                          <Badge key={r} tone="accent">{AGENT_LABEL[r] ?? r}</Badge>
                        ))}
                    <Badge tone={STAGE_STATUS_META[supervisor.status].tone}>
                      {STAGE_STATUS_META[supervisor.status].label}
                    </Badge>
                  </div>
                  {delegation.reason && (
                    <p className="text-xs leading-relaxed text-fg-muted">{delegation.reason}</p>
                  )}
                  <ProvenanceLine provenance={
                    ((supervisor.payload ?? {}) as { provenance?: Provenance }).provenance ?? {}} />
                </div>
              )}
            </Panel>
          </section>

          {/* 3 — the crew */}
          <section>
            <SectionTitle hint="CrewAI, one Crew with three members. Each answer is structured JSON, and a deterministic checker verifies that every number cited actually belongs to the metric it is attached to.">
              CrewAI diagnostic crew — findings
            </SectionTitle>
            {findings.length === 0 ? (
              <Panel>
                <EmptyPanel title={diagnostics ? (diagnostics.summary || "No findings recorded") : "Not recorded for this cycle"}
                            hint={diagnostics?.status === "skipped"
                              ? "The Supervisor consulted no specialist for this cycle."
                              : undefined} />
              </Panel>
            ) : (
              <div className="grid gap-3 lg:grid-cols-2">
                {findings.map((f) => <FindingCard key={f.agent} finding={f} />)}
              </div>
            )}
          </section>

          {/* 4 — decision */}
          <section>
            <SectionTitle hint="Google ADK. The Decision Agent weighs the crew's candidate actions against the learned policy history and picks at most one — which the Safety Gate then still has to approve.">
              ADK Decision Agent
            </SectionTitle>
            <Panel>
              {!decision ? (
                <EmptyPanel title="Not recorded for this cycle" />
              ) : (
                <div className="space-y-3">
                  <div className="flex flex-wrap items-center gap-2">
                    {dec.chosen_action ? (
                      <>
                        <Badge tone="accent">{dec.chosen_action.tool}</Badge>
                        <span className="nums text-xs text-fg">{dec.chosen_action.target_id}</span>
                        <span className="nums text-xs text-fg-muted">
                          {dec.chosen_action.weight != null
                            ? `cpu.weight → ${dec.chosen_action.weight}`
                            : dec.chosen_action.cpus != null
                              ? `cpu limit → ${num(dec.chosen_action.cpus, 2)} cores`
                              : ""}
                        </span>
                      </>
                    ) : <Badge tone="idle">No action proposed</Badge>}
                  </div>
                  {dec.reasoning && <p className="text-xs leading-relaxed text-fg-muted">{dec.reasoning}</p>}
                  {dec.chosen_action?.rationale && (
                    <p className="text-[11px] leading-relaxed text-fg-subtle">{dec.chosen_action.rationale}</p>
                  )}
                  <PayloadView value={decision.payload} label="Decision payload as recorded" />
                </div>
              )}
            </Panel>
          </section>
        </>
      )}
    </div>
  );
}

function DegradedBanner() {
  return (
    <div className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn-dim/30 px-3 py-2 text-xs text-warn">
      <ShieldOff className="mt-0.5 size-4 shrink-0" />
      <div>
        <strong className="font-medium">Degraded mode.</strong> Every cloud provider in the chain
        failed and the local fallback model answered instead. Those answers are kept and shown here
        for explanation, but they carry no action: the backend blocks them before the Safety Gate,
        and the Gate would reject them again.
      </div>
    </div>
  );
}

function ProvenanceLine({ provenance }: { provenance: Provenance }) {
  if (!provenance.provider && !provenance.model) return null;
  return (
    <p className="flex flex-wrap items-center gap-x-2 text-[11px] text-fg-subtle">
      <span>answered by</span>
      <span className="text-fg-muted">{provenance.provider ?? DASH}</span>
      <span className="nums">{provenance.model ?? ""}</span>
      {provenance.degraded && <Badge tone="warn">degraded</Badge>}
    </p>
  );
}

function FindingCard({ finding }: { finding: Finding }) {
  const d = finding.diagnosis;
  // Mirrors Finding.actionable in agents/diagnostic_crew/crew.py — displayed, not re-decided.
  const actionable = !!d && finding.check_ok && !finding.provenance.degraded;
  const candidate = d?.candidate_action ?? null;

  return (
    <Panel
      title={
        <span className="flex items-center gap-2">
          {AGENT_LABEL[finding.agent] ?? finding.agent}
          {d ? (
            <Badge tone={d.contention ? "bad" : "ok"}>
              {d.contention ? "contention" : "no contention"}
            </Badge>
          ) : <Badge tone="unknown">no diagnosis</Badge>}
          {d?.scope && d.scope !== "none" && <Badge tone="neutral">{d.scope} scope</Badge>}
        </span>
      }
      right={
        <Badge tone={actionable ? "ok" : "idle"}>
          <Hint text={actionable
            ? "This finding may carry an action forward to the Decision Agent."
            : !d ? "No diagnosis was produced, so nothing can go forward."
              : !finding.check_ok ? "The deterministic checker rejected a cited number, so this finding carries no action."
                : "A degraded local model answered, so this finding carries no action."}>
            <span>{actionable ? "actionable" : "no action"}</span>
          </Hint>
        </Badge>
      }
    >
      {finding.error && (
        <p className="mb-2 flex items-start gap-1.5 text-[11px] text-bad">
          <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
          {finding.error}
        </p>
      )}

      {!d ? (
        <EmptyPanel title="This agent returned no usable diagnosis" />
      ) : (
        <div className="space-y-3">
          <p className="text-xs leading-relaxed text-fg">{d.summary}</p>

          {d.culprit_target_ids.length > 0 && (
            <p className="flex flex-wrap items-center gap-1.5 text-[11px] text-fg-subtle">
              competing targets:
              {d.culprit_target_ids.map((t) => (
                <span key={t} className="nums rounded border border-border px-1 text-fg-muted">{t}</span>
              ))}
            </p>
          )}

          <div>
            <SectionTitle hint="Each row is a number the agent copied from the evidence bundle. The checker verifies the value belongs to the metric it is attached to — a number that merely appears somewhere in the evidence is not enough.">
              Cited evidence
            </SectionTitle>
            <ul className="space-y-1.5">
              {d.evidence.map((e, i) => (
                <li key={`${e.metric}-${i}`} className="rounded border border-border bg-bg px-2 py-1.5">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="nums text-[11px] text-fg-muted">{e.metric}</span>
                    <span className="nums text-[11px] font-medium text-fg">{num(e.value, 2)}</span>
                  </div>
                  <p className="mt-0.5 text-[11px] leading-snug text-fg-subtle">{e.statement}</p>
                </li>
              ))}
            </ul>
          </div>

          {!finding.check_ok && finding.check_issues.length > 0 && (
            <div className="rounded-md border border-bad/40 bg-bad-dim/30 px-2.5 py-2">
              <p className="text-[11px] font-medium text-bad">The deterministic checker rejected this answer:</p>
              <ul className="mt-1 list-inside list-disc space-y-0.5 text-[11px] text-bad">
                {finding.check_issues.map((c) => <li key={c}>{c}</li>)}
              </ul>
            </div>
          )}

          {candidate ? (
            <div className="rounded-md border border-border bg-bg-raised px-2.5 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[11px] uppercase tracking-wide text-fg-subtle">proposed</span>
                <Badge tone={actionable ? "accent" : "idle"}>{candidate.tool}</Badge>
                <span className="nums text-[11px] text-fg">{candidate.target_id}</span>
                {candidate.weight != null && <span className="nums text-[11px] text-fg-muted">weight {candidate.weight}</span>}
                {candidate.cpus != null && <span className="nums text-[11px] text-fg-muted">{num(candidate.cpus, 2)} cores</span>}
              </div>
              {candidate.rationale && (
                <p className="mt-1 text-[11px] leading-snug text-fg-subtle">{candidate.rationale}</p>
              )}
            </div>
          ) : (
            <p className="text-[11px] text-fg-subtle">
              This agent proposed no action.
              {finding.agent === "ram" && " The RAM agent is diagnosis-only by design: with no swap on this host, no memory change is safely reversible."}
              {finding.agent === "io" && " The I/O agent may only ever propose pausing a background writer, because io.weight is disabled on this host."}
            </p>
          )}

          {d.historical_basis && (
            <p className="text-[11px] leading-relaxed text-fg-subtle">
              <span className="text-fg-muted">Historical basis: </span>{d.historical_basis}
            </p>
          )}
        </div>
      )}

      <div className="mt-3 space-y-2 border-t border-border pt-2">
        <ProvenanceLine provenance={finding.provenance} />
        <PayloadView value={finding} label="Finding as recorded" />
      </div>
    </Panel>
  );
}
