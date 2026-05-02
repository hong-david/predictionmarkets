export function kalshiMarketUrl(
  marketId: string | null | undefined,
  eventId?: string | null,
): string | null {
  const ticker = (eventId || marketId || "").trim();
  if (!ticker) return null;

  return `https://kalshi.com/search?q=${encodeURIComponent(ticker.toLowerCase())}`;
}