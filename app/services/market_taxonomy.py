"""Shared market taxonomy helpers.

This module does read/scoring-time normalization only. It does not mutate the
stored markets.category column. The goal is to make dashboard presentation,
sports discounts, and peer baselines agree on obvious sports-family markets
whose stored category may be stale or too broad.
"""

from __future__ import annotations

import re

SPORTS_CATEGORIES = {
    "sports_outcome",
    "sports_derivative",
    "sports_prop",
}

SPORTS_EVENT_PREFIXES = (
    # Tennis
    "KXATP",
    "KXWTA",
    "KXITF",
    # US major sports / common league aliases
    "KXMLB",
    "KXNBA",
    "KXNFL",
    "KXNHL",
    "KXNCAAF",
    "KXNCAAB",
    "KXMLS",
    # Soccer / football leagues frequently emitted as league-specific prefixes
    "KXEPL",
    "KXSAUDIPLGAME",
    "KXEGYPLGAME",
    "KXCHAMPLGAME",
    "KXEUROPALGAME",
    "KXLALIGA",
    "KXSERIEA",
    "KXBUNDES",
    # Fighting / golf / racing / cricket / baseball variants
    "KXUFC",
    "KXPGA",
    "KXDPWORLDTOUR",
    "KXF1RACE",
    "KXCRICKET",
    "KXNPB",
)

_SPORTS_TITLE_RE = re.compile(
    r"\b("
    r"vs\.?|winner\??|win the .*match|round of|qualification final|"
    r"grand prix winner|open winner|tournament winner|"
    r"fc\b|united\b|city\b|al[- ]|"
    r"nba|nfl|mlb|nhl|mls|ufc|f1|formula 1|tennis|golf|soccer|football|baseball|basketball|hockey"
    r")\b",
    re.IGNORECASE,
)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _lower(value: object) -> str:
    return _clean(value).lower()


def _upper(value: object) -> str:
    return _clean(value).upper()


def market_symbol(event_id: object = None, market_id: object = None) -> str:
    """Return the best market/event symbol available for prefix checks."""
    return _upper(event_id) or _upper(market_id)


def is_sports_market(
    *,
    category: object = None,
    event_id: object = None,
    market_id: object = None,
    title: object = None,
) -> bool:
    category_s = _lower(category)
    if category_s in SPORTS_CATEGORIES:
        return True

    symbol = market_symbol(event_id, market_id)
    if symbol.startswith(SPORTS_EVENT_PREFIXES):
        return True

    title_s = _clean(title)
    return bool(title_s and _SPORTS_TITLE_RE.search(title_s))


def normalized_category_for_market(
    *,
    category: object = None,
    event_id: object = None,
    market_id: object = None,
    title: object = None,
) -> str | None:
    """Return the category used by read/scoring paths.

    We preserve specific sports subcategories when already present. Obvious
    sports-like event prefixes with stale categories are normalized to
    sports_outcome because they are outcome-style markets unless a more
    specific classifier already labeled them.
    """
    category_s = _lower(category)
    if category_s in SPORTS_CATEGORIES:
        return category_s

    if is_sports_market(
        category=category,
        event_id=event_id,
        market_id=market_id,
        title=title,
    ):
        return "sports_outcome"

    return category_s or None


def category_family_for_market(
    *,
    category: object = None,
    event_id: object = None,
    market_id: object = None,
    title: object = None,
) -> str:
    normalized = normalized_category_for_market(
        category=category,
        event_id=event_id,
        market_id=market_id,
        title=title,
    )
    if normalized in SPORTS_CATEGORIES:
        return "sports"
    return normalized or "unclassified"
