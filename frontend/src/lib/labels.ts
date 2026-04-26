/**
 * Human-readable copy for classifier / stats terminology. Raw API values
 * stay in English snake_case; display strings are for first-time readers.
 */

/** Insider-surveillance priority bucket (manipulability_prior). */
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
  return "Classifier *priority* (leak-sensitivity of this market type) — not the same as “alerts” in the right column, which count stored **evidence** rows. Use default sort to rank markets with alerts.";
}

/** Market category / axis key from charts (includes literal “unclassified”). */
export function categoryDisplay(key: string): string {
  if (key === "unclassified") return "Not categorized yet";
  return key.replace(/_/g, " ");
}

export function categoryHelp(): string {
  return "Topic bucket assigned automatically from the exchange’s tags and text (e.g. sports, economics). “Not categorized” means we have not stored a label yet.";
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
