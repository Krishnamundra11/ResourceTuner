/** Small building blocks shared by the views: page headers, stat tiles, labelled values and
 *  key/value grids. Kept here so a number looks the same wherever it appears. */

import type React from "react";
import { Card } from "./ui/card";
import { Hint } from "./ui/tooltip";
import { cn } from "@/lib/utils";
import { DASH } from "@/lib/format";

export function PageHeader({ title, subtitle, actions }: {
  title: string; subtitle?: React.ReactNode; actions?: React.ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-lg font-semibold tracking-tight text-fg">{title}</h1>
        {subtitle && <p className="mt-0.5 max-w-3xl text-xs leading-relaxed text-fg-subtle">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  );
}

export function SectionTitle({ children, hint, right }: {
  children: React.ReactNode; hint?: React.ReactNode; right?: React.ReactNode;
}) {
  return (
    <div className="mb-2 flex items-center justify-between gap-2">
      <h2 className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-fg-subtle">
        {children}
        {hint && <Hint text={hint}><span className="cursor-help text-fg-subtle">ⓘ</span></Hint>}
      </h2>
      {right}
    </div>
  );
}

/** A single headline number. The unit rides with the value; the label is text-token ink. */
export function StatTile({ label, value, unit, hint, tone, foot }: {
  label: string;
  value: string;
  unit?: string;
  hint?: React.ReactNode;
  tone?: "ok" | "warn" | "bad" | "accent" | "muted";
  foot?: React.ReactNode;
}) {
  const toneClass = {
    ok: "text-ok", warn: "text-warn", bad: "text-bad", accent: "text-accent", muted: "text-fg-muted",
  }[tone ?? "muted"];
  return (
    <Card className="px-3 py-2.5">
      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-fg-subtle">
        {label}
        {hint && <Hint text={hint}><span className="cursor-help">ⓘ</span></Hint>}
      </div>
      <div className={cn("nums mt-1 text-xl font-semibold leading-none", value === DASH ? "text-fg-subtle" : toneClass)}>
        {value}
        {unit && value !== DASH && <span className="ml-0.5 text-xs font-normal text-fg-subtle">{unit}</span>}
      </div>
      {foot && <div className="mt-1 text-[11px] text-fg-subtle">{foot}</div>}
    </Card>
  );
}

export function KeyValue({ rows, columns = 1 }: {
  rows: { k: string; v: React.ReactNode; hint?: React.ReactNode }[]; columns?: 1 | 2;
}) {
  return (
    <dl className={cn("grid gap-x-6 gap-y-1.5 text-xs", columns === 2 ? "sm:grid-cols-2" : "")}>
      {rows.map(({ k, v, hint }) => (
        <div key={k} className="flex items-baseline justify-between gap-3 border-b border-border/50 pb-1">
          <dt className="flex shrink-0 items-center gap-1 text-fg-subtle">
            {k}
            {hint && <Hint text={hint}><span className="cursor-help">ⓘ</span></Hint>}
          </dt>
          <dd className="nums min-w-0 truncate text-right text-fg">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A labelled panel with a title row — the default container for a chart or a table. */
export function Panel({ title, hint, right, children, className, bodyClassName }: {
  title?: React.ReactNode; hint?: React.ReactNode; right?: React.ReactNode;
  children: React.ReactNode; className?: string; bodyClassName?: string;
}) {
  return (
    <Card className={cn("flex min-w-0 flex-col", className)}>
      {title && (
        <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
          <h3 className="flex items-center gap-1.5 text-xs font-medium text-fg-muted">
            {title}
            {hint && <Hint text={hint}><span className="cursor-help text-fg-subtle">ⓘ</span></Hint>}
          </h3>
          {right}
        </div>
      )}
      <div className={cn("min-w-0 flex-1 p-3", bodyClassName)}>{children}</div>
    </Card>
  );
}

/** Read-only marker. Used wherever a view can only ever show records, never create them. */
export function ReadOnlyNote({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-md border border-border bg-bg-raised px-3 py-2 text-[11px] leading-relaxed text-fg-subtle">
      {children}
    </p>
  );
}
