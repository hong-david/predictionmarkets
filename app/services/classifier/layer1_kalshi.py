"""Layer 1: Kalshi taxonomy adapter.

The cheapest and highest-confidence layer. Kalshi already publishes
structured metadata on each market — `category`, `event_ticker`, `tags`,
and (sometimes) `series_ticker`. We map their taxonomy to ours.

Why this layer first:
  Kalshi has already paid the analyst cost for their own taxonomy. Reusing
  it costs us a 50-line lookup table plus a few sniff checks against
  free-form `tags`, instead of hand-writing prefix rules per ticker. When
  Kalshi adds a new market series, we get the category for free (assuming
  they tag it consistently — they usually do, but see the smoke-test note
  below).

Why we still need Layers 2-4:
  Kalshi's metadata is patchy in practice. About 30-40% of markets we've
  seen come back with no `category`, no `series_ticker`, and either an
  empty `tags` list or generic ones like ["Other"]. Lazy-upserted markets
  (`status='unknown'`) by definition have NO Kalshi metadata yet. So this
  layer often returns None, and the orchestrator falls through.

Output contract:
  Returns a `Classification` with `layer="kalshi_taxonomy"` and
  `confidence="high"` on a hit, or `None` if Kalshi's metadata isn't
  rich enough to classify. Never raises.
"""

from __future__ import annotations

from app.services.classifier.types import Classification


# Kalshi's `category` values mapped to our (category, subcategory). Kalshi's
# top-level vocabulary is small and stable; this is the cheapest adapter.
# When Kalshi inevitably adds a new top-level category, it shows up as a
# miss here and falls through to Layer 2's prefix rules.
KALSHI_CATEGORY_MAP: dict[str, tuple[str, str]] = {
    "Economics": ("macro", "*"),
    "Politics": ("election", "*"),
    "Climate and Weather": ("weather", "*"),
    "Weather": ("weather", "*"),
    "Sports": ("sports_outcome", "*"),
    "Crypto": ("crypto_strike", "*"),
    "Companies": ("corporate", "*"),
    "Financials": ("corporate", "*"),
    "World": ("other", "unclassified"),
    "Science and Technology": ("other", "unclassified"),
    "Health": ("other", "unclassified"),
    "Entertainment": ("popculture", "*"),
    "Culture": ("popculture", "*"),
}


# Free-form `tags` patterns. Multiple tags can fire; the first match wins
# in the order listed below. Order intentional — more specific tags first
# so e.g. "Bitcoin" wins over generic "Crypto", and "FOMC" wins over
# generic "Economics". Lower-cased on both sides for robustness.
TAG_RULES: list[tuple[str, tuple[str, str], list[str]]] = [
    # tag substring, (category, subcategory), tags
    ("fomc", ("macro", "fed_decision"), ["fomc", "scheduled_announcement"]),
    ("fed rate", ("macro", "fed_decision"), ["fomc", "scheduled_announcement"]),
    ("cpi", ("macro", "cpi"), ["cpi", "scheduled_announcement"]),
    ("inflation", ("macro", "cpi"), ["inflation", "scheduled_announcement"]),
    ("jobs report", ("macro", "jobs"), ["nfp", "scheduled_announcement"]),
    ("nonfarm", ("macro", "jobs"), ["nfp", "scheduled_announcement"]),
    ("gdp", ("macro", "gdp"), ["gdp", "scheduled_announcement"]),
    ("merger", ("corporate", "merger"), ["merger", "single_actor_leverage"]),
    ("earnings", ("corporate", "earnings"), ["earnings", "scheduled_announcement"]),
    ("fda", ("corporate", "fda"), ["fda", "scheduled_announcement"]),
    ("scotus", ("judicial", "scotus"), ["scotus", "scheduled_announcement"]),
    ("supreme court", ("judicial", "scotus"), ["scotus", "scheduled_announcement"]),
    ("ruling", ("judicial", "ruling"), ["judicial", "scheduled_announcement"]),
    ("bitcoin", ("crypto_strike", "*"), ["crypto", "public_underlying"]),
    ("ethereum", ("crypto_strike", "*"), ["crypto", "public_underlying"]),
    ("solana", ("crypto_strike", "*"), ["crypto", "public_underlying"]),
    ("ufc", ("sports_outcome", "fight_winner"), ["combat_sport", "single_actor_leverage"]),
    ("boxing", ("sports_outcome", "fight_winner"), ["combat_sport", "single_actor_leverage"]),
    ("mma", ("sports_outcome", "fight_winner"), ["combat_sport", "single_actor_leverage"]),
    ("nba", ("sports_outcome", "major_league_game"), ["nba"]),
    ("nfl", ("sports_outcome", "major_league_game"), ["nfl"]),
    ("mlb", ("sports_outcome", "major_league_game"), ["mlb"]),
    ("nhl", ("sports_outcome", "major_league_game"), ["nhl"]),
    ("tennis", ("sports_outcome", "tennis_match"), ["tennis"]),
    ("soccer", ("sports_outcome", "soccer_match"), ["soccer"]),
    ("temperature", ("weather", "temperature"), ["weather", "public_underlying"]),
    ("rain", ("weather", "precipitation"), ["weather", "public_underlying"]),
    ("snow", ("weather", "precipitation"), ["weather", "public_underlying"]),
    ("primary", ("election", "primary"), ["election", "primary"]),
    ("election", ("election", "general"), ["election"]),
    ("oscar", ("popculture", "awards"), ["awards"]),
    ("emmy", ("popculture", "awards"), ["awards"]),
    ("grammy", ("popculture", "awards"), ["awards"]),
]


def classify_via_kalshi(market: dict) -> Classification | None:
    """Classify a raw Kalshi market dict using its own metadata.

    `market` is the dict shape returned by Kalshi's `/markets` endpoint
    (or `/markets/{ticker}`). We never construct this from our DB — it
    has to be the raw upstream payload, because the DB schema strips
    Kalshi's `tags` / `series_ticker` (we don't persist them today).

    Returns None when Kalshi's metadata is too thin to classify. The
    orchestrator's escalation path takes over in that case.
    """
    if not isinstance(market, dict):
        return None

    tags_raw = market.get("tags") or []
    if isinstance(tags_raw, list):
        # Tag matching is case-insensitive substring against any tag
        # value, which is robust to Kalshi changing punctuation /
        # capitalisation across months. We deliberately do NOT also
        # match against the title here — title-as-text is Layer 3's
        # job, and mixing the two would let Layer 1 swallow
        # classifications that Layer 2's specific prefix rules would
        # have done better with (e.g. a "Bitcoin" hit in the title
        # bypassing the more-specific KXBTC15M short-window rule).
        haystack = " | ".join(str(t).lower() for t in tags_raw)
    else:
        haystack = ""

    for needle, (cat, sub), tag_list in TAG_RULES:
        if needle in haystack:
            return Classification(
                category=cat,
                subcategory=sub,
                layer="kalshi_taxonomy",
                rule=f"tag:{needle}",
                confidence="high",
                tags=list(tag_list),
            )

    raw_category = market.get("category")
    if isinstance(raw_category, str) and raw_category in KALSHI_CATEGORY_MAP:
        cat, sub = KALSHI_CATEGORY_MAP[raw_category]
        return Classification(
            category=cat,
            subcategory=sub,
            layer="kalshi_taxonomy",
            rule=f"category:{raw_category}",
            confidence="high",
            tags=[],
        )

    return None
