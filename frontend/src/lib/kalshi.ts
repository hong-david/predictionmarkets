export function kalshiMarketUrl(marketId: string | null | undefined): string | null {
  const ticker = (marketId ?? "").trim();
  if (!ticker) return null;
  return `https://kalshi.com/markets/${encodeURIComponent(ticker.toLowerCase())}`;
}
