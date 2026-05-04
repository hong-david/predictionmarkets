import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Newspaper, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "@/api/client";
import type { SearchMarketResult, SearchNewsResult } from "@/api/types";
import { fmtAgo, fmtInt } from "@/lib/utils";

export function GlobalSearch() {
  const navigate = useNavigate();
  const [value, setValue] = useState("");
  const [debounced, setDebounced] = useState("");
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(value.trim()), 500);
    return () => window.clearTimeout(t);
  }, [value]);

  const minSearchLength = 4;

  const search = useQuery({
    queryKey: ["globalSearch", debounced],
    queryFn: () => api.search(debounced, "all", 8),
    enabled: debounced.length >= minSearchLength,
    staleTime: 60_000,
  });

  const submit = () => {
    const q = value.trim();
    if (!q) return;
    navigate(`/markets?q=${encodeURIComponent(q)}`);
    setOpen(false);
  };

  const markets = search.data?.markets ?? [];
  const news = search.data?.news ?? [];
  const showPanel = open && debounced.length >= 2;

  return (
    <div className="relative hidden md:block w-[320px] xl:w-[430px]">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
        <input
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => window.setTimeout(() => setOpen(false), 140)}
          placeholder="Search markets or news..."
          className="w-full rounded-md border border-input bg-card pl-8 pr-3 py-1.5 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
        />
      </form>

      {showPanel ? (
        <div className="absolute left-0 right-0 top-[calc(100%+0.4rem)] z-50 overflow-hidden rounded-lg border border-border bg-card shadow-xl">
          {search.isPending ? (
            <div className="px-3 py-3 text-sm text-muted-foreground">
              Searching...
            </div>
          ) : search.isError ? (
            <div className="px-3 py-3 text-sm text-muted-foreground">
              Search unavailable.
            </div>
          ) : !markets.length && !news.length ? (
            <div className="px-3 py-3 text-sm text-muted-foreground">
              No matches.
            </div>
          ) : (
            <div className="max-h-[520px] overflow-auto">
              {markets.length ? (
                <section>
                  <SearchSectionLabel label="Markets" />
                  <ul className="divide-y divide-border">
                    {markets.slice(0, 5).map((market) => (
                      <MarketSearchRow
                        key={market.market_id}
                        market={market}
                        onClick={() => setOpen(false)}
                      />
                    ))}
                  </ul>
                </section>
              ) : null}

              {news.length ? (
                <section className={markets.length ? "border-t border-border" : ""}>
                  <SearchSectionLabel label="News" />
                  <ul className="divide-y divide-border">
                    {news.slice(0, 5).map((article, index) => (
                      <NewsSearchRow
                        key={`${article.article_id ?? "news"}-${index}`}
                        article={article}
                      />
                    ))}
                  </ul>
                </section>
              ) : null}
            </div>
          )}
          {search.data ? (
            <div className="border-t border-border px-3 py-2 text-[11px] text-muted-foreground flex items-center justify-between gap-3">
              <span>{search.data.provider}</span>
              <button
                type="button"
                className="text-primary hover:underline"
                onMouseDown={(e) => e.preventDefault()}
                onClick={submit}
              >
                Open in Markets
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function SearchSectionLabel({ label }: { label: string }) {
  return (
    <div className="px-3 py-1.5 text-[11px] uppercase tracking-wider text-muted-foreground bg-secondary/30">
      {label}
    </div>
  );
}

function MarketSearchRow({
  market,
  onClick,
}: {
  market: SearchMarketResult;
  onClick: () => void;
}) {
  return (
    <li>
      <Link
        to={`/markets/${encodeURIComponent(market.market_id)}`}
        onClick={onClick}
        className="block px-3 py-2.5 hover:bg-secondary/40 transition-colors"
      >
        <div className="text-sm font-medium leading-snug line-clamp-2">
          {market.title || market.market_id}
        </div>
        {market.subtitle ? (
          <div className="text-xs text-primary mt-0.5 line-clamp-1">
            {market.subtitle}
          </div>
        ) : null}
        <div className="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
          <code className="font-mono">{market.market_id}</code>
          {market.category ? <span>{market.category}</span> : null}
          <span className="num">{fmtInt(market.trade_count)} trades</span>
        </div>
      </Link>
    </li>
  );
}

function NewsSearchRow({ article }: { article: SearchNewsResult }) {
  return (
    <li className="px-3 py-2.5 hover:bg-secondary/40 transition-colors">
      <a
        href={article.url ?? "#"}
        target="_blank"
        rel="noreferrer"
        className="block group"
      >
        <div className="flex items-start justify-between gap-2">
          <div className="text-sm font-medium leading-snug line-clamp-2 group-hover:text-primary">
            {article.title}
          </div>
          {article.url ? (
            <ExternalLink className="h-3 w-3 mt-0.5 text-muted-foreground flex-shrink-0" />
          ) : (
            <Newspaper className="h-3 w-3 mt-0.5 text-muted-foreground flex-shrink-0" />
          )}
        </div>
        <div className="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
          {article.source ? <span>{article.source}</span> : null}
          <span>{fmtAgo(article.first_seen_at ?? article.published_at)}</span>
          {article.linked_market_count ? (
            <span>{fmtInt(article.linked_market_count)} linked markets</span>
          ) : null}
        </div>
      </a>
      {article.linked_markets.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {article.linked_markets.slice(0, 3).map((market) => (
            <Link
              key={market.market_id}
              to={`/markets/${encodeURIComponent(market.market_id)}`}
              className="rounded border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
            >
              {market.market_id}
            </Link>
          ))}
        </div>
      ) : null}
    </li>
  );
}
