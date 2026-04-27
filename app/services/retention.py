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

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import blake2b
from typing import Literal, Protocol

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

StorageTier = Literal["ignored", "observe_only", "sampled", "hot", "triggered", "case"]


@dataclass(frozen=True)
class StorageDecision:
    """Auditable retention result for one market/event.

    `process_realtime` means the event should still feed stateful detectors.
    `store_raw_hot` is the current Postgres/ClickHouse raw-write gate.
    `store_case_evidence` means the surrounding window should be promoted to
    durable evidence storage instead of expiring with normal hot TTL.
    """

    tier: StorageTier
    score: int
    process_realtime: bool
    store_raw_hot: bool
    raw_ttl_hours: int | None
    store_features: bool
    store_case_evidence: bool
    sample_rate: float
    reasons: tuple[str, ...]

    @property
    def persist_raw_tape(self) -> bool:
        return self.store_raw_hot


class _MarketForTape(Protocol):
    """Duck-typed `Market` row field access for `should_persist_raw_tape`."""

    manipulability_prior: str | None
    close_time: datetime | None


@dataclass(frozen=True)
class RetentionSignals:
    """Extra streaming signals that can promote an otherwise quiet market."""

    recent_price_zscore: float | None = None
    trade_burst_score: float | None = None
    news_match_score: float | None = None
    pre_news_directional_move: bool = False
    l2_pull_score: float | None = None
    severe_anomaly: bool = False


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
    return storage_decision_for_event(
        market,
        volume_24h_fp=volume_24h_fp,
        open_interest_fp=open_interest_fp,
        has_materialized_anomaly=has_materialized_anomaly,
        now=now,
    ).persist_raw_tape


def retention_score(
    market: _MarketForTape,
    *,
    volume_24h_fp: Decimal | None,
    open_interest_fp: Decimal | None,
    has_materialized_anomaly: bool = False,
    signals: RetentionSignals | None = None,
    now: datetime | None = None,
) -> tuple[int, tuple[str, ...]]:
    """Score how much raw evidence this market/event deserves to retain."""
    tnow = now or datetime.now(timezone.utc)
    sig = signals or RetentionSignals()
    score = 0
    reasons: list[str] = []

    if market.manipulability_prior in RAW_TAPE_HIGH_PRIORS:
        score += 30
        reasons.append("high_prior")
    elif market.manipulability_prior == "medium":
        score += 10
        reasons.append("medium_prior")

    if _market_closes_within_days(market, tnow, days=RAW_TAPE_CLOSING_SOON_DAYS):
        score += 20
        reasons.append("closing_soon")
    if volume_24h_fp is not None and volume_24h_fp >= RAW_TAPE_MIN_VOLUME_24H:
        score += 15
        reasons.append("volume_24h_floor")
    if open_interest_fp is not None and open_interest_fp >= RAW_TAPE_MIN_OPEN_INTEREST:
        score += 10
        reasons.append("open_interest_floor")
    if has_materialized_anomaly:
        score += 35
        reasons.append("materialized_anomaly")
    if sig.severe_anomaly:
        score += 40
        reasons.append("severe_anomaly")
    if sig.recent_price_zscore is not None and sig.recent_price_zscore >= 3:
        score += 25
        reasons.append("price_zscore")
    if sig.trade_burst_score is not None and sig.trade_burst_score >= 3:
        score += 25
        reasons.append("trade_burst")
    if sig.news_match_score is not None and sig.news_match_score >= 0.75:
        score += 30
        reasons.append("news_match")
    if sig.pre_news_directional_move:
        score += 40
        reasons.append("pre_news_directional_move")
    if sig.l2_pull_score is not None and sig.l2_pull_score >= 3:
        score += 20
        reasons.append("l2_pull")

    return score, tuple(reasons)


def storage_decision_for_event(
    market: _MarketForTape,
    *,
    volume_24h_fp: Decimal | None,
    open_interest_fp: Decimal | None,
    has_materialized_anomaly: bool = False,
    signals: RetentionSignals | None = None,
    now: datetime | None = None,
) -> StorageDecision:
    """Tiered retention policy for raw Kalshi events and derived features."""
    score, reasons = retention_score(
        market,
        volume_24h_fp=volume_24h_fp,
        open_interest_fp=open_interest_fp,
        has_materialized_anomaly=has_materialized_anomaly,
        signals=signals,
        now=now,
    )

    if score >= 110:
        return StorageDecision(
            "case", score, True, True, None, True, True, 1.0, reasons
        )
    if score >= 80:
        return StorageDecision(
            "triggered", score, True, True, 720, True, True, 1.0, reasons
        )
    if score >= 50:
        return StorageDecision("hot", score, True, True, 168, True, False, 1.0, reasons)
    if score >= 20:
        return StorageDecision(
            "sampled", score, True, True, 24, True, False, 0.1, reasons
        )
    if score >= 0:
        return StorageDecision(
            "observe_only", score, True, False, 1, True, False, 0.0, reasons
        )
    return StorageDecision(
        "ignored", score, False, False, None, False, False, 0.0, reasons
    )


def should_sample_event(key: str, sample_rate: float) -> bool:
    """Deterministic sampler so reconnect replays make the same retain/drop choice."""
    if sample_rate >= 1:
        return True
    if sample_rate <= 0:
        return False
    digest = blake2b(key.encode("utf-8"), digest_size=8).digest()
    bucket = int.from_bytes(digest, "big") / float(2**64 - 1)
    return bucket < sample_rate


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
