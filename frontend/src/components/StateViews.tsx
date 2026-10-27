/** Loading / error / empty / stale / unavailable states. Every data surface in the app uses
 *  these, so an unknown value never silently renders as zero. */

import { AlertTriangle, Inbox, Loader2, WifiOff } from "lucide-react";
import { ApiError } from "@/lib/api";
import { Button } from "./ui/button";
import { Skeleton } from "./ui/misc";
import { cn } from "@/lib/utils";

export function LoadingPanel({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("space-y-2 p-4", className)} role="status" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-4" style={{ width: `${90 - i * 12}%` }} />
      ))}
    </div>
  );
}

export function ErrorPanel({ error, onRetry, className }: { error: unknown; onRetry?: () => void; className?: string }) {
  const api = error instanceof ApiError ? error : null;
  const offline = api?.status === 0;
  return (
    <div className={cn("flex flex-col items-start gap-2 p-4", className)} role="alert">
      <div className="flex items-center gap-2 text-sm text-bad">
        {offline ? <WifiOff className="size-4" /> : <AlertTriangle className="size-4" />}
        <span className="font-medium">{offline ? "Backend unreachable" : "Could not load this data"}</span>
      </div>
      <p className="text-xs text-fg-subtle">
        {error instanceof Error ? error.message : String(error)}
        {api && api.status > 0 ? ` (HTTP ${api.status})` : ""}
      </p>
      {offline && (
        <p className="text-xs text-fg-subtle">
          Start it with <code className="rounded bg-bg-raised px-1 font-mono">./venv/bin/python -m resourcetuner.dashboard</code>
        </p>
      )}
      {onRetry && <Button size="sm" variant="secondary" onClick={onRetry}>Retry</Button>}
    </div>
  );
}

export function EmptyPanel({ title, hint, icon, className }: {
  title: string; hint?: React.ReactNode; icon?: React.ReactNode; className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-2 px-4 py-10 text-center", className)}>
      <div className="text-fg-subtle">{icon ?? <Inbox className="size-6" />}</div>
      <p className="text-sm font-medium text-fg-muted">{title}</p>
      {hint && <p className="max-w-md text-xs text-fg-subtle">{hint}</p>}
    </div>
  );
}

export function WarmingUp({ what = "Telemetry" }: { what?: string }) {
  return (
    <div className="flex items-center gap-2 px-4 py-6 text-sm text-fg-muted">
      <Loader2 className="size-4 animate-spin" />
      {what} is warming up — the sampler needs a few seconds to fill its first window.
    </div>
  );
}

/** Shown when polling still works but the newest sample is older than it should be. */
export function StaleBanner({ ageSeconds }: { ageSeconds: number }) {
  return (
    <div className="flex items-center gap-2 rounded-md border border-warn/40 bg-warn-dim/30 px-3 py-1.5 text-xs text-warn">
      <AlertTriangle className="size-3.5" />
      Showing data from {ageSeconds}s ago — the live stream has not updated recently.
    </div>
  );
}

/** For a metric the backend reports as null: visibly different from a real zero. */
export function Unavailable({ why }: { why?: string }) {
  return <span className="text-fg-subtle" title={why ?? "not available on this host"}>—</span>;
}
