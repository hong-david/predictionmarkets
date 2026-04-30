"""Manipulability prior map — Layer 4.

This is the *one* place in the system where a human value judgment lives.
Every other layer is mechanical text-mapping; this file decides how much
surveillance attention each kind of market deserves.

Design contract:

  - Keys are `(category, subcategory)` tuples produced by Layers 1-3.
  - Values are one of `ManipulabilityPrior` (`very_low` ... `high`).
  - This map is intentionally tiny. If you find yourself wanting to add a
    rule that says "this *one* market is high prior" — that belongs in a
    classifier rule, not here. This file should grow only when a new
    *category* of market is introduced.

Why a static dict and not YAML / DB:

  Cheaper auditability. Every prior change is a line in `git blame` with a
  reviewer, a date, and a commit message. A YAML-config-in-DB design would
  give us something more "live" but at the cost of regulator-defensible
  versioning. We can swap it later without touching call sites; the public
  surface here is just `prior_for(category, subcategory)`.

How to read the priors:

  - "high":         insider trading is plausible, the information set is
                    narrow or single-actor, and the market is liquid enough
                    that informed flow could move price. The active
                    surveillance set.
  - "medium_high":  liquid, but the underlying is not easily insider-leaked
                    (sports outcomes — public events with private prep).
  - "medium":       broad participation reduces single-actor leverage
                    (general-election outcomes, large-cap macro).
  - "low":          public, frequently-updated underlying (BTC strikes,
                    weather) where insider-style information advantage is
                    largely impossible.
  - "very_low":     priced off a public number that is itself impossible
                    to manipulate at the individual level (temperature,
                    rainfall).

Bump CLASSIFIER_VERSION in types.py if a value here changes in a way that
should overwrite already-stored verdicts.
"""

from __future__ import annotations

from app.services.classifier.types import ManipulabilityPrior

# (category, subcategory) -> prior. Subcategory "*" is the per-category
# default applied when a more specific (category, subcategory) is missing.
PRIOR_MAP: dict[tuple[str, str], ManipulabilityPrior] = {
    # ----- Macro / scheduled-announcement markets -------------------------
    # Government data releases are surveillance-relevant, but broad scheduled
    # macro buckets create thousands of active contracts. Keep most of them
    # just below "high" so that bucket stays small enough to review. Fed
    # decision and press-conference markets are narrow enough to sit in high.
    ("macro", "fed_decision"): "high",
    ("macro", "cpi"): "medium_high",
    ("macro", "jobs"): "medium_high",
    ("macro", "gdp"): "medium_high",
    ("macro", "*"): "medium_high",

    # ----- Corporate / event-driven --------------------------------------
    # Mergers, earnings, FDA approvals, bankruptcy filings. Mergers and FDA
    # stay high because the information set is narrower and the binary event
    # edge is cleaner; broad scheduled earnings remains just below high.
    ("corporate", "merger"): "high",
    ("corporate", "earnings"): "medium_high",
    ("corporate", "fda"): "high",
    ("corporate", "*"): "medium_high",

    # ----- Judicial -------------------------------------------------------
    # SCOTUS / federal-court rulings have the same insider-leakage shape:
    # a tiny set of clerks, a binary outcome, a known announcement window.
    ("judicial", "scotus"): "high",
    ("judicial", "ruling"): "high",
    ("judicial", "*"): "high",

    # ----- Sports outcomes -----------------------------------------------
    # Mainstream game winners are public, closely watched, many-actor events:
    # a few thousand dollars of informed flow should not make Lakers/Rockets
    # look like a macro leak. Combat sports stay higher because one fighter
    # can more directly determine their own outcome.
    ("sports_outcome", "fight_winner"): "high",
    ("sports_outcome", "tennis_match"): "medium",
    ("sports_outcome", "soccer_match"): "medium",
    ("sports_outcome", "major_league_game"): "medium",
    ("sports_outcome", "*"): "medium",

    # ----- Sports derivatives (spreads, totals) --------------------------
    # Spreads and totals can be moved by intentional poor play without
    # changing the game outcome ("shaving points"). Still, major-league lines
    # are heavily watched and betting-liquidity-rich, so keep them at medium
    # unless other evidence appears.
    ("sports_derivative", "spread"): "medium",
    ("sports_derivative", "total"): "medium",
    ("sports_derivative", "*"): "medium",

    # ----- Sports props (single-player outcomes) -------------------------
    # First-basket / first-goal / over-under-on-player-X. More direct than a
    # team winner, but still not as narrow as a boardroom, court, or agency
    # decision. Treat as medium-high, then let news/injury/tape evidence lift it.
    ("sports_prop", "player_points"): "medium_high",
    ("sports_prop", "first_event"): "medium_high",
    ("sports_prop", "*"): "medium",

    # ----- Crypto strikes -------------------------------------------------
    # 15-minute / hourly / daily BTC / ETH strikes. The underlying is a
    # public price stream from large global exchanges that no individual
    # can move at the timescales these markets resolve on. Liquidity is
    # high but insider-style information advantage is essentially zero.
    ("crypto_strike", "short_window"): "low",
    ("crypto_strike", "daily"): "low",
    ("crypto_strike", "*"): "low",

    # ----- Weather --------------------------------------------------------
    # Temperature / rainfall / snowfall at a measurement station. The
    # underlying physically cannot be manipulated; even forecast-based
    # information advantage is small and shrinks with liquidity.
    ("weather", "temperature"): "very_low",
    ("weather", "precipitation"): "very_low",
    ("weather", "*"): "very_low",

    # ----- Elections ------------------------------------------------------
    # Generals are diluted across millions of voters. Primaries are
    # narrower and have more single-actor leverage (campaign drop-out,
    # endorsement). Pre-Iowa primary spikes have historically been the
    # most surveillance-relevant election pattern.
    ("election", "primary"): "medium_high",
    ("election", "general"): "medium",
    ("election", "*"): "medium",

    # ----- Pop culture / awards ------------------------------------------
    # Voting body is small (~9k for the Oscars). Ballot leakage is a real
    # but historically rare pattern.
    ("popculture", "awards"): "medium",
    ("popculture", "ratings"): "low",
    ("popculture", "*"): "medium",

    # ----- Exotic / cross-category combos --------------------------------
    # "Will X happen AND Y happen" markets. v1 prior is medium until we
    # build the constituent-derivation logic that combines the per-leg
    # priors of the underlying markets. Tagged so the analyst review queue
    # picks them up.
    ("exotic_combo", "*"): "medium",

    # ----- Fallback ------------------------------------------------------
    # Anything Layers 1-3 couldn't classify lands here. Medium prior so it
    # gets reviewed but doesn't crowd out the high-prior watchlist.
    ("other", "unclassified"): "medium",
    ("other", "*"): "medium",
}


def prior_for(category: str, subcategory: str) -> ManipulabilityPrior:
    """Return the manipulability prior for a `(category, subcategory)` pair.

    Lookup precedence:
      1. Exact `(category, subcategory)` match.
      2. Per-category default `(category, "*")`.
      3. Global default `("other", "unclassified")`.

    The third level guarantees we never return None — every classified
    market gets a prior, even if its category is unrecognised.
    """
    if (category, subcategory) in PRIOR_MAP:
        return PRIOR_MAP[(category, subcategory)]
    if (category, "*") in PRIOR_MAP:
        return PRIOR_MAP[(category, "*")]
    return PRIOR_MAP[("other", "unclassified")]
