/** Raw stage payload, shown verbatim.
 *
 *  The backend scrubs secrets before an event is stored (see `scrub()` in events/store.py), so
 *  what arrives here is already redacted; this component adds no interpretation of its own. */

import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

export function PayloadView({ value, label = "Raw payload", defaultOpen = false, className }: {
  value: unknown; label?: string; defaultOpen?: boolean; className?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const empty = value === null || value === undefined
    || (typeof value === "object" && Object.keys(value as object).length === 0);

  if (empty) return <p className="text-[11px] text-fg-subtle">No payload was recorded for this stage.</p>;

  return (
    <div className={cn("rounded-md border border-border bg-bg", className)}>
      <button type="button" onClick={() => setOpen((o) => !o)}
              className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left text-[11px] text-fg-muted hover:text-fg"
              aria-expanded={open}>
        <ChevronRight className={cn("size-3 transition-transform", open && "rotate-90")} />
        {label}
      </button>
      {open && (
        <pre className="max-h-80 overflow-auto border-t border-border px-2.5 py-2 text-[10.5px] leading-relaxed text-fg-muted">
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </div>
  );
}
