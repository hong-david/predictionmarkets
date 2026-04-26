import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/utils";

export function Card({
  className,
  ...props
}: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-card text-card-foreground shadow-sm",
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({
  title,
  subtitle,
  right,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 px-4 py-3 border-b border-border">
      <div>
        <h2 className="text-[11px] uppercase tracking-wider text-muted-foreground font-semibold">
          {title}
        </h2>
        {subtitle ? (
          <div className="text-xs text-muted-foreground mt-0.5">{subtitle}</div>
        ) : null}
      </div>
      {right ? <div className="text-xs text-muted-foreground">{right}</div> : null}
    </div>
  );
}

export function CardBody({
  className,
  ...props
}: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4", className)} {...props} />;
}
