/**
 * Maps engine reason strings to plain-English for the "Why" panel.
 * Unknown strings pass through; match longer keys first.
 */

const PAIRS: [RegExp, string][] = [
  [/^wide spread vs recent baseline/i, "Best bid/ask is wider than this market’s own recent history."],
  [/^wide spread \(static threshold\)/i, "Best bid/ask is wider than a fixed dollar band."],
  [/^zero liquidity/i, "Order book size reported as empty or zero (structural, not a trade)."],
  [/^empty visible yes book/i, "The visible “yes” side of the order book is empty."],
  [/^large ref-price move vs recent \(z\)/i, "Last/mid price moved more than this market’s recent price moves (rolling baseline)."],
  [/^sharp price move \(static threshold\)/i, "Ref price moved a lot in one snapshot (static band)."],
  [/^large volume delta vs recent \(z\)/i, "Cumulative exchange volume jumped more than this market’s recent volume steps."],
  [/^large volume jump \(static threshold\)/i, "A big step in reported cumulative contract volume (static threshold)."],
  [/^moderate volume jump \(static threshold\)/i, "A medium step in reported cumulative contract volume (static threshold)."],
  [/^limited history/i, "Not enough prior snapshots to score everything yet."],
  [/^very high order-book event rate/i, "A lot of order-book messages in a few minutes (churn / flicker)."],
  [/^sustained cancel.*pull side/i, "Cancels and/or pulling liquidity dominated recent book updates."],
];

export function humanizeAnomalyReason(raw: string): string {
  const t = raw.trim();
  for (const [re, out] of PAIRS) {
    if (re.test(t)) return out;
  }
  return t;
}
