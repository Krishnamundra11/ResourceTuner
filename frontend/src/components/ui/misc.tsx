import * as React from "react";
import * as SeparatorPrimitive from "@radix-ui/react-separator";
import * as SliderPrimitive from "@radix-ui/react-slider";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import * as LabelPrimitive from "@radix-ui/react-label";
import * as ProgressPrimitive from "@radix-ui/react-progress";
import { cn } from "@/lib/utils";

export const Separator = ({ className, orientation = "horizontal", ...props }:
  React.ComponentPropsWithoutRef<typeof SeparatorPrimitive.Root>) => (
  <SeparatorPrimitive.Root orientation={orientation}
    className={cn("shrink-0 bg-border", orientation === "horizontal" ? "h-px w-full" : "h-full w-px", className)}
    {...props} />
);

export const Label = ({ className, ...props }: React.ComponentPropsWithoutRef<typeof LabelPrimitive.Root>) => (
  <LabelPrimitive.Root className={cn("text-xs font-medium text-fg-muted", className)} {...props} />
);

export const Slider = React.forwardRef<
  React.ElementRef<typeof SliderPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SliderPrimitive.Root>
>(({ className, id, "aria-label": ariaLabel, "aria-labelledby": ariaLabelledBy, ...props }, ref) => (
  // Radix puts role="slider" and keyboard focus on the Thumb, not the Root — so `id` (for a
  // paired <Label htmlFor>) and `aria-label`/`aria-labelledby` must land there too, or the
  // control has no accessible name at all despite looking labelled in the DOM.
  <SliderPrimitive.Root ref={ref}
    className={cn("relative flex w-full touch-none select-none items-center", className)} {...props}>
    <SliderPrimitive.Track className="relative h-1.5 w-full grow overflow-hidden rounded-full bg-bg-raised">
      <SliderPrimitive.Range className="absolute h-full bg-accent" />
    </SliderPrimitive.Track>
    <SliderPrimitive.Thumb id={id} aria-label={ariaLabel} aria-labelledby={ariaLabelledBy}
      className="block size-3.5 rounded-full border-2 border-accent bg-bg transition-transform hover:scale-110 disabled:pointer-events-none" />
  </SliderPrimitive.Root>
));
Slider.displayName = "Slider";

export const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root>
>(({ className, ...props }, ref) => (
  <SwitchPrimitive.Root ref={ref}
    className={cn("peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-border transition-colors",
      "data-[state=checked]:bg-accent-dim data-[state=unchecked]:bg-bg-raised disabled:opacity-50", className)}
    {...props}>
    <SwitchPrimitive.Thumb className="pointer-events-none block size-3.5 rounded-full bg-fg-muted shadow transition-transform data-[state=checked]:translate-x-4 data-[state=checked]:bg-accent-strong data-[state=unchecked]:translate-x-0.5" />
  </SwitchPrimitive.Root>
));
Switch.displayName = "Switch";

export const Progress = ({ className, value, ...props }:
  React.ComponentPropsWithoutRef<typeof ProgressPrimitive.Root>) => (
  <ProgressPrimitive.Root className={cn("relative h-1.5 w-full overflow-hidden rounded-full bg-bg-raised", className)} {...props}>
    <ProgressPrimitive.Indicator className="size-full flex-1 bg-accent transition-transform"
      style={{ transform: `translateX(-${100 - (value ?? 0)}%)` }} />
  </ProgressPrimitive.Root>
);

export const Skeleton = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("animate-pulse rounded-md bg-bg-raised", className)} {...props} />
);

/** A thin wrapper so every data table in the app shares spacing and header styling. */
export function Table({ className, ...props }: React.TableHTMLAttributes<HTMLTableElement>) {
  return <table className={cn("w-full border-collapse text-sm nums", className)} {...props} />;
}
export const Th = ({ className, ...props }: React.ThHTMLAttributes<HTMLTableCellElement>) => (
  <th className={cn("border-b border-border px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wide text-fg-subtle", className)} {...props} />
);
export const Td = ({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) => (
  <td className={cn("border-b border-border/60 px-3 py-2 align-middle text-fg-muted", className)} {...props} />
);
