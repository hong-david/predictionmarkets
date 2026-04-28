"""Layer 2: ticker / title prefix rules.

Kalshi tickers follow a templated naming scheme (`KX<series>-<event>-<leg>`)
that's actually quite informative. This layer maps those prefixes / tokens
to our taxonomy. It catches everything Layer 1 missed because Kalshi's own
metadata was thin or absent (in particular: every lazy-upserted
`status='unknown'` market the WS feed creates).

Why prefix rules and not just "match the whole ticker":
  Tickers have variable suffix structure (date, team, strike) but a stable
  series prefix. A regex on the prefix is robust to Kalshi adding new
  events under the same series — e.g. every NBA game ticker starts with
  `KXNBAGAME-`, regardless of date or matchup.

Why first-match-wins, not "best match":
  Auditability. "This rule fired" is a better explanation than "rule 7
  scored 0.83 and rule 3 scored 0.81". When two rules conflict, the order
  in `RULES` is the conflict-resolution authority and is reviewable in
  one place.

Output contract:
  Returns `Classification` with `layer="prefix_rule"`, `confidence="high"`
  on a hit, or None on miss. Never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.classifier.types import Classification


@dataclass(frozen=True)
class Rule:
    """A single prefix / regex rule.

    Either `prefix` (cheap, common case) OR `pattern` (full regex) drives
    matching. `pattern` is checked against the concatenation
    `f"{ticker}|{title}"` so a rule can fire on either a ticker shape or
    a title phrase.
    """

    name: str
    category: str
    subcategory: str
    tags: tuple[str, ...]
    prefix: str | None = None
    pattern: re.Pattern[str] | None = None


def _re(p: str) -> re.Pattern[str]:
    return re.compile(p, re.IGNORECASE)


# Order matters: more specific first, generic fallbacks last. Bumping
# CLASSIFIER_VERSION when this list changes triggers a backfill reclassify.
RULES: list[Rule] = [
    # ---- Crypto strike markets (highest-volume category we see) ----------
    # KXBTC15M-...   (BTC 15-minute strike)
    # KXBTC...HM...  (BTC hourly)
    # KXBTCD...      (BTC daily)
    Rule(
        name="crypto.btc_15m",
        prefix="KXBTC15M",
        category="crypto_strike",
        subcategory="short_window",
        tags=("crypto", "btc", "short_window", "public_underlying"),
    ),
    Rule(
        name="crypto.btc_hourly",
        pattern=_re(r"^KXBTC(?:HM|H)\b"),
        category="crypto_strike",
        subcategory="short_window",
        tags=("crypto", "btc", "short_window", "public_underlying"),
    ),
    Rule(
        name="crypto.btc_daily",
        prefix="KXBTCD",
        category="crypto_strike",
        subcategory="daily",
        tags=("crypto", "btc", "public_underlying"),
    ),
    Rule(
        name="crypto.btc_generic",
        prefix="KXBTC",
        category="crypto_strike",
        subcategory="*",
        tags=("crypto", "btc", "public_underlying"),
    ),
    Rule(
        name="crypto.eth_short",
        pattern=_re(r"^KXETH(?:15M|HM|H)\b"),
        category="crypto_strike",
        subcategory="short_window",
        tags=("crypto", "eth", "short_window", "public_underlying"),
    ),
    Rule(
        name="crypto.eth_generic",
        prefix="KXETH",
        category="crypto_strike",
        subcategory="*",
        tags=("crypto", "eth", "public_underlying"),
    ),
    Rule(
        name="crypto.sol_generic",
        prefix="KXSOL",
        category="crypto_strike",
        subcategory="*",
        tags=("crypto", "sol", "public_underlying"),
    ),

    # ---- Macro / scheduled-announcement -----------------------------------
    Rule(
        name="macro.fomc",
        pattern=_re(r"^KX(?:FED|FOMC|RATEDECISION)"),
        category="macro",
        subcategory="fed_decision",
        tags=("fomc", "scheduled_announcement"),
    ),
    Rule(
        name="macro.cpi",
        pattern=_re(r"^KX(?:CPI|INFLATION)"),
        category="macro",
        subcategory="cpi",
        tags=("cpi", "scheduled_announcement"),
    ),
    Rule(
        name="macro.jobs",
        pattern=_re(r"^KX(?:JOBS|NFP|UNEMP|UNEMPLOYMENT|PAYROLL)"),
        category="macro",
        subcategory="jobs",
        tags=("nfp", "scheduled_announcement"),
    ),
    Rule(
        name="macro.gdp",
        prefix="KXGDP",
        category="macro",
        subcategory="gdp",
        tags=("gdp", "scheduled_announcement"),
    ),

    # ---- Corporate / event-driven -----------------------------------------
    Rule(
        name="corporate.earnings",
        pattern=_re(r"^KX(?:EARN|EARNINGS)"),
        category="corporate",
        subcategory="earnings",
        tags=("earnings", "scheduled_announcement"),
    ),
    Rule(
        name="corporate.merger",
        pattern=_re(r"^KX(?:MERGER|ACQUISITION|MA)(?:-|$)"),
        category="corporate",
        subcategory="merger",
        tags=("merger", "single_actor_leverage"),
    ),
    Rule(
        name="corporate.fda",
        prefix="KXFDA",
        category="corporate",
        subcategory="fda",
        tags=("fda", "scheduled_announcement"),
    ),

    # ---- Judicial ---------------------------------------------------------
    Rule(
        name="judicial.scotus",
        pattern=_re(r"^KX(?:SCOTUS|SUPREMECOURT)"),
        category="judicial",
        subcategory="scotus",
        tags=("scotus", "scheduled_announcement"),
    ),
    Rule(
        name="judicial.ruling",
        pattern=_re(r"^KX(?:RULING|VERDICT|TRIAL|CONFIRM)"),
        category="judicial",
        subcategory="ruling",
        tags=("judicial", "scheduled_announcement"),
    ),

    # ---- Sports — combat (single-actor leverage) --------------------------
    Rule(
        name="sports.ufc",
        prefix="KXUFC",
        category="sports_outcome",
        subcategory="fight_winner",
        tags=("ufc", "combat_sport", "single_actor_leverage"),
    ),
    Rule(
        name="sports.mma",
        pattern=_re(r"^KX(?:MMA|BELLATOR|PFL)"),
        category="sports_outcome",
        subcategory="fight_winner",
        tags=("mma", "combat_sport", "single_actor_leverage"),
    ),
    Rule(
        name="sports.boxing",
        prefix="KXBOX",
        category="sports_outcome",
        subcategory="fight_winner",
        tags=("boxing", "combat_sport", "single_actor_leverage"),
    ),

    # ---- Sports — major league game outcomes ------------------------------
    # Ticker prefix variants seen in the wild include KXNBAGAME, KXNBAWIN,
    # KXNBA<MATCHUP>; the regex catches all of them. The "spread" /
    # "total" / "pts" matchers run AFTER this generic one because we want
    # the more specific derivative classification to win.
    Rule(
        name="sports.nba_spread",
        pattern=_re(r"^KXNBA.*(?:SPREAD|HANDICAP|LINE)"),
        category="sports_derivative",
        subcategory="spread",
        tags=("nba", "spread"),
    ),
    Rule(
        name="sports.nba_total",
        pattern=_re(r"^KXNBA.*(?:TOTAL|OVERUNDER|OU)"),
        category="sports_derivative",
        subcategory="total",
        tags=("nba", "total"),
    ),
    Rule(
        name="sports.nba_player_prop",
        pattern=_re(r"^KXNBA.*(?:PTS|REB|AST|STL|BLK|3PT)"),
        category="sports_prop",
        subcategory="player_points",
        tags=("nba", "player_prop", "single_actor_leverage"),
    ),
    Rule(
        name="sports.nba_game",
        prefix="KXNBA",
        category="sports_outcome",
        subcategory="major_league_game",
        tags=("nba",),
    ),
    Rule(
        name="sports.nfl_spread",
        pattern=_re(r"^KXNFL.*(?:SPREAD|HANDICAP|LINE)"),
        category="sports_derivative",
        subcategory="spread",
        tags=("nfl", "spread"),
    ),
    Rule(
        name="sports.nfl_total",
        pattern=_re(r"^KXNFL.*(?:TOTAL|OVERUNDER|OU)"),
        category="sports_derivative",
        subcategory="total",
        tags=("nfl", "total"),
    ),
    Rule(
        name="sports.nfl_game",
        prefix="KXNFL",
        category="sports_outcome",
        subcategory="major_league_game",
        tags=("nfl",),
    ),
    Rule(
        name="sports.mlb_first_event",
        pattern=_re(r"^KXMLB.*(?:FIRSTHIT|FIRSTRUN|HOMER|HR)"),
        category="sports_prop",
        subcategory="first_event",
        tags=("mlb", "single_actor_leverage"),
    ),
    Rule(
        name="sports.mlb_game",
        prefix="KXMLB",
        category="sports_outcome",
        subcategory="major_league_game",
        tags=("mlb",),
    ),
    Rule(
        name="sports.nhl_game",
        prefix="KXNHL",
        category="sports_outcome",
        subcategory="major_league_game",
        tags=("nhl",),
    ),
    Rule(
        name="sports.tennis",
        pattern=_re(r"^KX(?:TENNIS|ATP|WTA)"),
        category="sports_outcome",
        subcategory="tennis_match",
        tags=("tennis",),
    ),
    Rule(
        name="sports.soccer",
        pattern=_re(r"^KX(?:SOCCER|EPL|MLS|LALIGA|UCL|UEFA|FIFA)"),
        category="sports_outcome",
        subcategory="soccer_match",
        tags=("soccer",),
    ),
    Rule(
        name="sports.golf",
        pattern=_re(r"^KX(?:GOLF|PGA|MASTERS|USOPEN|OPEN)"),
        category="sports_outcome",
        subcategory="*",
        tags=("golf",),
    ),

    # ---- Weather ----------------------------------------------------------
    Rule(
        name="weather.temp",
        pattern=_re(r"^KX(?:HIGHTEMP|LOWTEMP|TEMP)"),
        category="weather",
        subcategory="temperature",
        tags=("weather", "public_underlying"),
    ),
    Rule(
        name="weather.precip",
        pattern=_re(r"^KX(?:RAIN|SNOW|PRECIP)"),
        category="weather",
        subcategory="precipitation",
        tags=("weather", "public_underlying"),
    ),

    # ---- Elections --------------------------------------------------------
    Rule(
        name="election.primary",
        pattern=_re(r"^KX(?:PRIMARY|CAUCUS)"),
        category="election",
        subcategory="primary",
        tags=("election", "primary"),
    ),
    Rule(
        name="election.governor",
        pattern=_re(r"^KX(?:GOV|GOVERNOR|GOVRACE)"),
        category="election",
        subcategory="general",
        tags=("election",),
    ),
    Rule(
        name="election.senate",
        pattern=_re(r"^KX(?:SENATE|SEN)"),
        category="election",
        subcategory="general",
        tags=("election",),
    ),
    Rule(
        name="election.house",
        prefix="KXHOUSE",
        category="election",
        subcategory="general",
        tags=("election",),
    ),
    Rule(
        name="election.presidential",
        pattern=_re(r"^KX(?:PRES|POTUS|PRESIDENT)"),
        category="election",
        subcategory="general",
        tags=("election", "presidential"),
    ),

    # ---- Pop culture ------------------------------------------------------
    Rule(
        name="popculture.awards",
        pattern=_re(r"^KX(?:OSCAR|EMMY|GRAMMY|TONY|GOLDENGLOBE)"),
        category="popculture",
        subcategory="awards",
        tags=("awards",),
    ),
    Rule(
        name="popculture.box_office",
        pattern=_re(r"^KX(?:BOXOFFICE|MOVIE)"),
        category="popculture",
        subcategory="ratings",
        tags=("box_office",),
    ),

    # ---- Exotic combos ----------------------------------------------------
    # MVECROSSCATEGORY = "multi-variable equity cross category" — Kalshi's
    # combinatorial parlay markets. Manipulability prior is derived in v2;
    # for now they sit in the medium bucket.
    Rule(
        name="exotic.cross_category",
        pattern=_re(r"^KX(?:MVE|COMBO|PARLAY)"),
        category="exotic_combo",
        subcategory="*",
        tags=("combinatorial",),
    ),
]


def classify_via_prefix_rules(ticker: str, title: str | None = None) -> Classification | None:
    """Run the prefix / regex rule list against a single market.

    `ticker` is the Kalshi `market_id` (always upper-case, hyphen-delimited).
    `title` is optional; when present, `pattern` rules see the concatenation
    `f"{ticker}|{title}"` so a rule can fire on either.

    Returns `None` if no rule matched. Never raises.
    """
    if not ticker:
        return None
    haystack = f"{ticker}|{title or ''}"
    ticker_upper = ticker.upper()

    for rule in RULES:
        if rule.prefix is not None and ticker_upper.startswith(rule.prefix):
            return Classification(
                category=rule.category,
                subcategory=rule.subcategory,
                layer="prefix_rule",
                rule=rule.name,
                confidence="high",
                tags=list(rule.tags),
            )
        if rule.pattern is not None and rule.pattern.search(haystack):
            return Classification(
                category=rule.category,
                subcategory=rule.subcategory,
                layer="prefix_rule",
                rule=rule.name,
                confidence="high",
                tags=list(rule.tags),
            )

    return None
