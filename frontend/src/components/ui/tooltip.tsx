import * as React from "react";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { cn } from "@/lib/utils";

export const TooltipProvider = TooltipPrimitive.Provider;
export const Tooltip = TooltipPrimitive.Root;
export const TooltipTrigger = TooltipPrimitive.Trigger;

export const TooltipContent = React.forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(({ className, sideOffset = 6, ...props }, ref) => (
  <TooltipPrimitive.Portal>
    <TooltipPrimitive.Content ref={ref} sideOffset={sideOffset}
      className={cn("z-50 max-w-xs rounded-md border border-border bg-panel px-2.5 py-1.5 text-xs text-fg-muted shadow-xl", className)}
      {...props} />
  </TooltipPrimitive.Portal>
));
TooltipContent.displayName = "TooltipContent";

/** Short form for the common "label with an explanation" case. */
export function Hint({ children, text }: { children: React.ReactNode; text: React.ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild><span className="cursor-help border-b border-dotted border-border-strong">{children}</span></TooltipTrigger>
      <TooltipContent>{text}</TooltipContent>
    </Tooltip>
  );
}
