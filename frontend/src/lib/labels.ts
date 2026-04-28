/**
 * Human-readable copy for classifier / stats terminology. Raw API values
 * stay in English snake_case; display strings are for first-time readers.
 */

/** Watch-priority bucket (`manipulability_prior`). */
const PRIOR_LABELS: Record<string, string> = {
  high: "High",
  medium_high: "Elevated",
  medium: "Medium",
  low: "Lower",
  very_low: "Minimal",
  unclassified: "Not set",
};

export function priorDisplay(p: string): string {
  return PRIOR_LABELS[p] ?? p.replace(/_/g, " ");
}

export function priorShort(p: string): string {
  return priorDisplay(p);
}

/** One-line explainer for tooltips / help text. */
export function priorHelp(): string {
  return "Watch priority is the classifier's estimate of how sensitive this market type is to news or manipulation. It is not a suspicious-activity verdict.";
}

/** Market category / axis key from charts (includes literal `unclassified`). */
export function categoryDisplay(key: string): string {
  if (key === "unclassified") return "Not categorized yet";
  return key.replace(/_/g, " ");
}

export function categoryHelp(): string {
  return "Topic bucket assigned automatically from the exchange tags and market text. Not categorized means we have not stored a label yet.";
}

export function layerDisplay(layer: string | null | undefined): string {
  if (!layer) return "";
  const m: Record<string, string> = {
    kalshi_tag: "From exchange tags",
    prefix_rule: "From ticker pattern",
    knn_embedding: "From similar markets",
    llm: "From language model (optional)",
  };
  return m[layer] ?? layer;
}
