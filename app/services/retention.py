"""Retention / scope policy for the surveillance universe.

This module answers the question: "is this market worth tracking at all?"
It's the only place the system decides what to drop on the floor — both
proactively (skip ingest) and retrospectively (prune existing rows).

Why centralise the policy:

  Without one place, scope decisions leak everywhere — REST poller has
  one set of filters, WS lazy-upsert has another, the prune script has a
  third, and they drift out of sync. Surveillance regulators reviewing
  the system would have to read three files to answer "what's in scope
  and why". One file, one set of predicates, used by every caller.

What's out of scope by default:

  - **Exotic combinatorial markets** (`category="exotic_combo"`). These
    are Kalshi's `KXMVECROSSCATEGORY-...` parlay markets — there are
    ~285k of them in production today. Their manipulability prior
    *should* be derived from the constituent legs, but that v2 logic
    isn't built yet. Until then, parking them in scope is just expensive
    storage with no surveillance signal. They're trivially re-includable
    when the per-leg derivation lands.
  - **Crypto strike markets** (`category="crypto_strike"`). 15-minute,
    hourly, and daily BTC / ETH / SOL price strikes. The original
    rationale kept these for trade-tape pattern detection (spoofing,
    layering, banging-the-close). On these specific markets the
    economics defeat the surveillance value: the highest-volume strike
    in production carries ~$1-2k of notional, so realised profit from
    successful microstructure manipulation is cents, and the underlying
    price is set by global crypto markets that dwarf Kalshi by ~10,000x
    — any in-Kalshi pressure gets arbed against spot/futures instantly.
    The regulatory-news angle (ETF approval, executive order, etc.)
    shows up on the *political* market that frames the decision, which
    we already classify as `macro` or `judicial` and keep at high prior;
    the crypto strike is at best a leveraged derivative bet that
    informed flow could *also* express, never the cleanest source.
  - **Very-low manipulability prior** (`weather`, etc.). Underlying is a
    public physical measurement (temperature, rainfall) that no insider
    can move; surveillance signal is essentially zero.

What's intentionally NOT excluded:

  - `low` prior generally (e.g. `popculture.ratings`). Public underlying
    like Nielsen ratings, but unlike crypto strikes these aren't
    automated MM venues — the per-row noise floor is much lower, and
    occasional entertainment-industry leakage is a credible if rare
    pattern. Excluding them would save ~162 rows; the cost-benefit
    doesn't justify the policy precedent.
  - Settled / closed markets. Resolution timestamps are surveillance
    ground truth (the leakage detector needs them); deleting them by age
    would destroy the only labelled history we'll ever have. A
    cold-storage tier is a future PR.

How operators can override:

  The policy is a frozenset module constant rather than a config because
  this is a *surveillance scope* decision, not an env var. Changing it
  is a code review with a commit message and a reviewer, which is
  exactly what regulators expect for "what does our system pay attention
  to" decisions. If it ever needs to be runtime-configurable, the public
  surface (`is_in_scope`, `is_market_in_scope_by_ticker`) doesn't change.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from app.services.classifier import Classification, classify

# Categories that are entirely dropped from the surveillance universe.
# Adding a category here must be paired with a scope-justification entry
# in the README design-choices section (see "Retention / scope policy").
EXCLUDED_CATEGORIES: frozenset[str] = frozenset({"exotic_combo", "crypto_strike"})

# Manipulability priors below which markets are dropped. `very_low`
# excludes weather (and any future category we map to it); other
# `low`-prior markets stay in scope because the trade tape can still
# carry spoofing / momentum-ignition signal that doesn't depend on the
# underlying being insider-leakable.
EXCLUDED_PRIORS: frozenset[str] = frozenset({"very_low"})

# --- Raw tape + order-book row persistence (second tier) -------------------
#
# In-scope markets (above) still get `markets` + quote snapshots and the
# anomaly materializer. The **trades** and **book_events** tables are the
# main disk amplifiers. We only persist those rows when at least one
# “surveillance-relevant” signal is true: high / medium_high prior, near
# resolution, 24h volume or OI over a floor, or a materialized `anomalies`
# row. Everything else is still visible in the UI via REST snapshots, but
# the per-print tape and L2 history are not retained — matching a tiered
# retention model without an offline cold store in v1.
#
# Constants are code (not env) for the same reason as EXCLUDED_CATEGORIES.
RAW_TAPE_HIGH_PRIORS: frozenset[str] = frozenset({"high", "medium_high"})
RAW_TAPE_MIN_VOLUME_24H = Decimal("250")  # contracts; from ticker 24h field
RAW_TAPE_MIN_OPEN_INTEREST = Decimal("200")
RAW_TAPE_CLOSING_SOON_DAYS = 14


class _MarketForTape(Protocol):
    """Duck-typed `Market` row field access for `should_persist_raw_tape`."""

    manipulability_prior: str | None
    close_time: datetime | None


def _market_closes_within_days(
    market: _MarketForTape,
    now: datetime,
    *,
    days: int,
) -> bool:
    """True if the market’s close is in (now, now+days] (surveillance: soon to resolve)."""
    ct = market.close_time
    if ct is None:
        return False
    if ct.tzinfo is None:
        ct = ct.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    deadline = now + timedelta(days=days)
    return now < ct <= deadline


def should_persist_raw_tape(
    market: _MarketForTape,
    *,
    volume_24h_fp: Decimal | None,
    open_interest_fp: Decimal | None,
    has_materialized_anomaly: bool = False,
    now: datetime | None = None,
) -> bool:
    """Decide whether to write **trades** and **book_events** rows for this market.

    Quote snapshots and anomaly materialization are unchanged — this only
    gates the append-only tape and L2 tables.

    Callers that already know a materialized `anomalies` row exists should
    pass ``has_materialized_anomaly=True`` to avoid a DB round-trip; otherwise
    the WS path queries once when the cheap predicates fail.
    """
    if has_materialized_anomaly:
        return True
    tnow = now or datetime.now(timezone.utc)
    if market.manipulability_prior in RAW_TAPE_HIGH_PRIORS:
        return True
    if _market_closes_within_days(market, tnow, days=RAW_TAPE_CLOSING_SOON_DAYS):
        return True
    if volume_24h_fp is not None and volume_24h_fp >= RAW_TAPE_MIN_VOLUME_24H:
        return True
    if open_interest_fp is not None and open_interest_fp >= RAW_TAPE_MIN_OPEN_INTEREST:
        return True
    return False


def is_in_scope(c: Classification) -> bool:
    """Return True if this classification belongs in the surveillance universe.

    Pure function over a `Classification`. The orchestrator's confidence
    clamp has already run by the time we see this, so a low-confidence
    verdict that *would* have had a high prior already shows up as
    `medium` here — which means we keep it. That's deliberate: dropping
    rows we're not confident about would silently bias the corpus toward
    the things our prefix rules already understand.
    """
    if c.category in EXCLUDED_CATEGORIES:
        return False
    if c.manipulability_prior in EXCLUDED_PRIORS:
        return False
    return True


def is_market_in_scope(market_dict: dict) -> tuple[bool, Classification]:
    """Classify a raw Kalshi market dict and apply the scope filter.

    Used by the REST ingest path (`ingest_markets_payload`) so we never
    upsert an out-of-scope market to begin with. Returns the
    `Classification` alongside the boolean so callers can stamp the
    classifier columns on the row in a single pass without classifying
    twice.
    """
    c = classify(market=market_dict)
    return is_in_scope(c), c


def is_ticker_in_scope(ticker: str) -> tuple[bool, Classification]:
    """Decide scope for a bare ticker (no title, no Kalshi metadata).

    Used by the WS lazy-upsert path (`_get_or_create_market`), where the
    `ticker` channel emits messages for markets we haven't seen via
    REST yet. We only have the ticker string at that point, so the
    classifier runs on Layer 2 (prefix rules) alone — Layer 1 needs
    Kalshi tags, Layer 3 / 4 need a title.

    The conservative bias here is "in scope unless we're sure it isn't".
    A ticker that doesn't match any rule lands at `other.unclassified`
    with prior `medium`, which keeps it. That's correct — the WS feed
    is exactly where new market series first appear, and we'd rather
    upsert a not-yet-recognised market than silently drop signal we
    haven't taught the classifier about.
    """
    c = classify(ticker=ticker)
    return is_in_scope(c), c
