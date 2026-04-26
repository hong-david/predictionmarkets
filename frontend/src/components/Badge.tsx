import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * Small coloured pill. Used for severity, manipulability prior,
 * confidence, and status. Variants are explicit rather than computed
 * from a string so a typo in props lights up the type checker, not a
 * silent grey badge in production.
 */
type Variant =
  | "neutral"
  | "primary"
  | "success"
  | "warning"
  | "danger"
  | "outline";

const variants: Record<Variant, string> = {
  neutral: "bg-secondary text-secondary-foreground border-border",
  primary: "bg-primary/15 text-primary border-primary/30",
  success: "bg-[hsl(var(--severity-low)/0.15)] text-[hsl(var(--severity-low))] border-[hsl(var(--severity-low)/0.35)]",
  warning: "bg-[hsl(var(--severity-medium)/0.15)] text-[hsl(var(--severity-medium))] border-[hsl(var(--severity-medium)/0.35)]",
  danger: "bg-[hsl(var(--severity-high)/0.15)] text-[hsl(var(--severity-high))] border-[hsl(var(--severity-high)/0.35)]",
  outline: "bg-transparent text-muted-foreground border-border",
};

export function Badge({
  variant = "neutral",
  className,
  children,
}: {
  variant?: Variant;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] uppercase tracking-wider font-semibold",
        variants[variant],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function severityVariant(s: string | null | undefined): Variant {
  if (s === "high") return "danger";
  if (s === "medium") return "warning";
  if (s === "low") return "success";
  return "outline";
}

export function priorVariant(p: string | null | undefined): Variant {
  if (p === "high") return "danger";
  if (p === "medium_high") return "warning";
  if (p === "medium") return "primary";
  if (p === "low" || p === "very_low") return "outline";
  return "outline";
}
