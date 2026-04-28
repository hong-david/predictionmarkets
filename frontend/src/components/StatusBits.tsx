import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** Skeleton block for loading states. Tailwind animates the pulse. */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "animate-pulse rounded-md bg-muted/50",
        className,
      )}
    />
  );
}

/** Inline empty / loading / error placeholder. */
export function EmptyState({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "py-10 text-center text-sm italic text-muted-foreground",
        className,
      )}
    >
      {children}
    </div>
  );
}

type StatusTone = "live" | "healthy" | "stale" | "empty" | "error";

/** Live-pulse dot. Green = healthy, yellow = stale/empty, red = error. */
export function StatusDot({ tone = "live" }: { tone?: StatusTone }) {
  const cls =
    tone === "error"
      ? "bg-[hsl(var(--severity-high))]"
      : tone === "stale"
        ? "bg-[hsl(var(--severity-medium))]"
        : tone === "empty"
          ? "bg-muted-foreground/50"
        : "bg-[hsl(var(--severity-low))]";
  return (
    <span
      aria-hidden
      className={cn("inline-block h-1.5 w-1.5 rounded-full", cls)}
    />
  );
}
