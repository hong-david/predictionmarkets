import { Link } from "react-router-dom";

import { cn } from "@/lib/utils";
import type { MarketRow } from "@/api/types";
import { priorShort } from "@/lib/labels";

import { Badge, priorVariant } from "./Badge";

/**
 * One link target for the whole row: title, subtitle, ticker, and badges.
 * (Nested <a> tags are avoided — a single `Link` wraps the readable text.)
 */
export function MarketCell({
  market,
  link = true,
  showCategory = true,
  showPrior = false,
  className,
}: {
  market: Pick<
    MarketRow,
    | "market_id"
    | "title"
    | "subtitle"
    | "category"
    | "manipulability_prior"
  >;
  link?: boolean;
  showCategory?: boolean;
  showPrior?: boolean;
  className?: string;
}) {
  const hasTitle = market.title && market.title !== market.market_id;
  const titleEl = hasTitle ? (
    <div className="text-sm leading-tight font-medium">{market.title}</div>
  ) : (
    <div className="text-sm leading-tight italic text-muted-foreground">
      Title not loaded from exchange yet
    </div>
  );

  const inner = (
    <>
      {titleEl}
      {market.subtitle ? (
        <div className="text-xs text-primary mt-0.5 leading-tight">
          {market.subtitle}
        </div>
      ) : null}
      <div className="mt-1 flex items-center gap-1.5 flex-wrap">
        <span className="font-mono text-[11px] text-muted-foreground">
          {market.market_id}
        </span>
        {showCategory && market.category ? (
          <Badge variant="outline" className="font-mono normal-case tracking-normal">
            {market.category}
          </Badge>
        ) : null}
        {showPrior && market.manipulability_prior ? (
          <Badge variant={priorVariant(market.manipulability_prior)}>
            {priorShort(market.manipulability_prior)}
          </Badge>
        ) : null}
      </div>
    </>
  );

  return (
    <div className={cn("min-w-0", className)}>
      {link ? (
        <Link
          to={`/markets/${encodeURIComponent(market.market_id)}`}
          className="block rounded-md -m-0.5 p-0.5 hover:bg-secondary/40 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {inner}
        </Link>
      ) : (
        inner
      )}
    </div>
  );
}
