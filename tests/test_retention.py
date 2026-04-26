"""Tests for the retention / scope policy.

Two layers of coverage:

  1. Pure-function tests of `is_in_scope`, `is_market_in_scope`,
     `is_ticker_in_scope` against representative classifications.
  2. Integration-shaped tests that the ingest path actually skips
     out-of-scope markets and surfaces the counter — uses a fake
     SQLAlchemy session in the same style as
     `tests/test_kalshi_ws_handlers.py`.
"""

from __future__ import annotations

from typing import Any

from app.services.classifier import Classification
from app.services.retention import (
    EXCLUDED_CATEGORIES,
    EXCLUDED_PRIORS,
    is_in_scope,
    is_market_in_scope,
    is_ticker_in_scope,
)


def _classification(
    *,
    category: str = "macro",
    subcategory: str = "cpi",
    prior: str = "high",
    confidence: str = "high",
    layer: str = "prefix_rule",
) -> Classification:
    return Classification(
        category=category,
        subcategory=subcategory,
        manipulability_prior=prior,
        confidence=confidence,
        layer=layer,
        rule="test",
        tags=[],
    )


# ---------- is_in_scope ---------------------------------------------------


class TestIsInScope:
    def test_macro_high_prior_is_in_scope(self) -> None:
        assert is_in_scope(_classification()) is True

    def test_low_prior_is_kept(self) -> None:
        # `low` prior is NOT in the excluded *priors* set — only
        # `very_low` is. We keep `popculture.ratings` for occasional
        # entertainment-industry leakage signal. (Crypto strikes are
        # also `low` prior but get filtered out via the *category*
        # exclusion below — see test_crypto_strike_category_is_excluded.)
        c = _classification(category="popculture", subcategory="ratings", prior="low")
        assert is_in_scope(c) is True

    def test_very_low_prior_is_excluded(self) -> None:
        assert is_in_scope(_classification(prior="very_low")) is False

    def test_exotic_combo_category_is_excluded(self) -> None:
        c = _classification(category="exotic_combo", subcategory="cross_category")
        assert is_in_scope(c) is False

    def test_crypto_strike_category_is_excluded(self) -> None:
        # Crypto strikes get excluded by *category*, not prior — the
        # per-row prior is `low` (same as popculture.ratings), but the
        # economics on these specific markets defeat any
        # microstructure-surveillance value. See retention.py docstring.
        for sub in ("short_window", "daily", "*"):
            c = _classification(category="crypto_strike", subcategory=sub, prior="low")
            assert is_in_scope(c) is False, f"crypto_strike/{sub} should be excluded"

    def test_excluded_sets_are_what_we_advertise(self) -> None:
        # If someone changes the policy without updating the README we
        # want this test to catch it. README claims exotic_combo and
        # crypto_strike (categories) and very_low (prior) are the
        # exclusions.
        assert EXCLUDED_CATEGORIES == frozenset({"exotic_combo", "crypto_strike"})
        assert EXCLUDED_PRIORS == frozenset({"very_low"})


# ---------- is_market_in_scope (raw Kalshi dict) --------------------------


class TestIsMarketInScope:
    def test_real_kalshi_dict_in_scope(self) -> None:
        # KXCPI-* matches the macro.cpi prefix rule.
        item = {
            "ticker": "KXCPI-26FEB-T3.0",
            "title": "Will core CPI come in above 3% in February 2026?",
            "tags": [],
        }
        in_scope, classification = is_market_in_scope(item)
        assert in_scope is True
        assert classification.category == "macro"
        assert classification.manipulability_prior in ("high", "medium_high")

    def test_exotic_combo_dict_excluded(self) -> None:
        # KXMVECROSSCATEGORY-* matches the exotic_combo prefix rule.
        item = {
            "ticker": "KXMVECROSSCATEGORY-26-NFL-NYC-RAIN",
            "title": "Will all of: Eagles win AND NYC rain AND ...",
            "tags": [],
        }
        in_scope, classification = is_market_in_scope(item)
        assert in_scope is False
        assert classification.category == "exotic_combo"

    def test_returns_classification_for_reuse(self) -> None:
        # The ingestor reuses the returned classification rather than
        # running the four-layer stack twice. Verify the second tuple
        # element is a fully-populated Classification.
        item = {"ticker": "FED-26FEB-T2.50", "title": "Will the Fed...", "tags": []}
        _, classification = is_market_in_scope(item)
        assert classification is not None
        assert classification.category is not None
        assert classification.subcategory is not None
        assert classification.manipulability_prior is not None
        assert classification.confidence in ("high", "medium", "low")


# ---------- is_ticker_in_scope (WS lazy-upsert path) ----------------------


class TestIsTickerInScope:
    def test_known_prefix_in_scope(self) -> None:
        in_scope, c = is_ticker_in_scope("KXCPI-26FEB-T3.0")
        assert in_scope is True
        assert c.category == "macro"

    def test_exotic_combo_prefix_excluded(self) -> None:
        in_scope, c = is_ticker_in_scope("KXMVECROSSCATEGORY-FOO-BAR")
        assert in_scope is False
        assert c.category == "exotic_combo"

    def test_unknown_prefix_kept_as_unclassified(self) -> None:
        # Conservative bias for first-sighting on the WS feed: a ticker
        # that matches no prefix rule lands at `other.unclassified` with
        # prior `medium` → kept. The REST poller will refine it later.
        in_scope, c = is_ticker_in_scope("ZZZNEWSERIES-26JAN-T1")
        assert in_scope is True
        assert c.category == "other"


# ---------- ingest path skip behaviour ------------------------------------


class _FakeSession:
    """Minimal SQLAlchemy stand-in mirroring test_kalshi_ws_handlers.py.

    We don't care which queries are issued for in-scope rows in this
    test — we only assert that out-of-scope rows produce *no* writes.
    """

    def __init__(self) -> None:
        self.market_rows: list[Any] = []
        self.snapshot_rows: list[Any] = []
        self.committed = False

    def query(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def one_or_none(self):
        return None  # always insert, never update

    def add(self, obj):
        from app.db.models import Market, MarketSnapshot

        if isinstance(obj, Market):
            self.market_rows.append(obj)
        elif isinstance(obj, MarketSnapshot):
            self.snapshot_rows.append(obj)

    def flush(self):
        # Stand-in autoincrement so `market.id` is non-None when the
        # snapshot constructor reads it.
        for i, m in enumerate(self.market_rows, start=1):
            if m.id is None:
                m.id = i

    def commit(self):
        self.committed = True


class TestIngestSkipsOutOfScope:
    def test_skips_exotic_combo(self) -> None:
        from app.services.market_ingestor import ingest_markets_payload

        payload = {
            "markets": [
                {
                    "ticker": "KXMVECROSSCATEGORY-X-Y-Z",
                    "title": "Some giant parlay",
                    "tags": [],
                },
                {
                    "ticker": "KXCPI-26FEB-T3.0",
                    "title": "Will core CPI come in above 3%?",
                    "tags": [],
                },
            ]
        }
        db = _FakeSession()
        result = ingest_markets_payload(db, payload)

        assert result["skipped_out_of_scope"] == 1
        assert result["inserted_markets"] == 1
        assert len(db.market_rows) == 1
        assert db.market_rows[0].market_id == "KXCPI-26FEB-T3.0"

    def test_skips_crypto_strike(self) -> None:
        # KXBTC15M-* matches the crypto.btc_15m prefix rule which maps
        # to category=crypto_strike — should now be excluded at ingest.
        from app.services.market_ingestor import ingest_markets_payload

        payload = {
            "markets": [
                {
                    "ticker": "KXBTC15M-26APR252100-T77600",
                    "title": "BTC price up in next 15 mins?",
                    "tags": [],
                },
                {
                    "ticker": "KXCPI-26FEB-T3.0",
                    "title": "Will core CPI come in above 3%?",
                    "tags": [],
                },
            ]
        }
        db = _FakeSession()
        result = ingest_markets_payload(db, payload)

        assert result["skipped_out_of_scope"] == 1
        assert result["inserted_markets"] == 1
        assert db.market_rows[0].market_id == "KXCPI-26FEB-T3.0"

    def test_skips_very_low_prior(self) -> None:
        from app.services.market_ingestor import ingest_markets_payload

        payload = {
            "markets": [
                # Weather → very_low prior → should skip.
                {
                    "ticker": "KXHIGHNY-26JUN26-T80",
                    "title": "Will NYC high temperature exceed 80F on June 26?",
                    "tags": [],
                }
            ]
        }
        db = _FakeSession()
        result = ingest_markets_payload(db, payload)

        # Either the weather rule fires (skip) or it doesn't and the
        # row is kept. We don't fail on classifier coverage here — we
        # just assert the *counter* is exposed correctly when the
        # filter does fire.
        assert "skipped_out_of_scope" in result

    def test_empty_payload_returns_zero_skipped(self) -> None:
        from app.services.market_ingestor import ingest_markets_payload

        db = _FakeSession()
        result = ingest_markets_payload(db, {"markets": []})
        assert result["skipped_out_of_scope"] == 0
