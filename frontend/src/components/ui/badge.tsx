import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11px] font-medium leading-5 whitespace-nowrap",
  {
    variants: {
      tone: {
        neutral: "border-border bg-bg-raised text-fg-muted",
        accent: "border-accent/40 bg-accent-dim/40 text-accent-strong",
        ok: "border-ok/40 bg-ok-dim/40 text-ok",
        warn: "border-warn/40 bg-warn-dim/40 text-warn",
        bad: "border-bad/40 bg-bad-dim/40 text-bad",
        idle: "border-border bg-idle-dim/40 text-fg-subtle",
        unknown: "border-unknown/40 bg-unknown-dim/40 text-unknown",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export type BadgeTone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>;

export const Badge = ({ className, tone, ...props }: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>) => (
  <span className={cn(badgeVariants({ tone }), className)} {...props} />
);
export { badgeVariants };
