"""Orchestrator — runs the four classifier layers in order.

Escalation policy (top to bottom):

  1. `classify_via_kalshi(market_dict)`        — Layer 1
  2. `classify_via_prefix_rules(ticker, title)` — Layer 2
  3. `classify_via_knn(title)`                  — Layer 3
  4. `llm.classify(title, subtitle)`            — Layer 4

The first layer that returns a `confidence != "low"` Classification wins.
A `confidence == "low"` Classification from any layer is *kept* (so the
orchestrator can return *something*) but does not stop escalation — we
keep trying further layers in case a later one returns medium/high.

If every layer returns either None or low confidence, the orchestrator
returns the best low-confidence classification found, or — if every layer
returned None — a synthesized `("other", "unclassified")` fallback so
downstream code never has to handle a None.

The priority map is applied as the LAST step on whatever classification we
return, so `Classification.manipulability_prior` is always populated.

The whole orchestrator is a pure function modulo the LLM dependency; no
DB, no I/O beyond the optional LLM HTTP call. Easy to test.
"""

from __future__ import annotations

from app.services.classifier.layer1_kalshi import classify_via_kalshi
from app.services.classifier.layer2_rules import classify_via_prefix_rules
from app.services.classifier.layer3_knn import classify_via_knn
from app.services.classifier.layer4_llm import (
    LLMClassifier,
    default_llm_classifier,
)
from app.services.classifier.priorities import prior_for
from app.services.classifier.types import Classification


def _fallback() -> Classification:
    """Last-resort classification when every layer abstains."""
    return Classification(
        category="other",
        subcategory="unclassified",
        layer="fallback",
        rule="all_layers_abstained",
        confidence="low",
        tags=[],
    )


def _better(a: Classification | None, b: Classification | None) -> Classification | None:
    """Return the higher-confidence of two classifications.

    `b` wins ties so the *later* layer's verdict is preferred when
    confidence is equal — Layer 3 / 4 are slower-but-richer signals than
    Layer 1's keyword sniff, so when both return "low" we'd rather keep
    the embedding-based answer.
    """
    if a is None:
        return b
    if b is None:
        return a
    rank = {"high": 2, "medium": 1, "low": 0}
    if rank[b.confidence] >= rank[a.confidence]:
        return b
    return a


def classify(
    market: dict | None = None,
    *,
    ticker: str | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    llm: LLMClassifier | None = None,
) -> Classification:
    """Classify a market, attaching `manipulability_prior`.

    Two call shapes:
      1. `classify(market=raw_kalshi_dict)`    — preferred when we have
         the raw upstream payload (REST poller, lazy hydrator). Layer 1
         can use Kalshi's metadata.
      2. `classify(ticker=..., title=...)`     — for stub Markets / lazy-
         upserted rows where we don't have the raw Kalshi dict around.
         Layer 1 is skipped. Layers 2-4 still run.

    `subtitle` is used by Layer 4 only (extra context for the LLM). Pass
    `llm=` to inject a deterministic test fake; the default uses
    `default_llm_classifier()`, which is `NullLLMClassifier` unless
    overridden.

    Always returns a `Classification` with `manipulability_prior` set.
    """
    if market is not None:
        ticker = ticker or market.get("ticker")
        title = title or market.get("title")
        subtitle = (
            subtitle
            or market.get("yes_sub_title")
            or market.get("no_sub_title")
            or market.get("subtitle")
        )

    best: Classification | None = None

    if market is not None:
        layer1 = classify_via_kalshi(market)
        if layer1 is not None and layer1.confidence != "low":
            return _attach_prior(layer1)
        best = _better(best, layer1)

    if ticker:
        layer2 = classify_via_prefix_rules(ticker, title)
        if layer2 is not None and layer2.confidence != "low":
            return _attach_prior(layer2)
        best = _better(best, layer2)

    if title:
        layer3 = classify_via_knn(title)
        if layer3 is not None and layer3.confidence != "low":
            return _attach_prior(layer3)
        best = _better(best, layer3)

    if title:
        llm = llm or default_llm_classifier()
        layer4 = llm.classify(title, subtitle)
        if layer4 is not None and layer4.confidence != "low":
            return _attach_prior(layer4)
        best = _better(best, layer4)

    return _attach_prior(best or _fallback())


def _attach_prior(c: Classification) -> Classification:
    """Stamp `manipulability_prior` onto a classification by lookup.

    Low-confidence safety clamp:
      A classification we don't trust shouldn't be allowed to promote a
      market into the high-priority watchlist. If an L3 / L4 verdict
      came back with `confidence='low'` and the prior map says "high" /
      "medium_high", we downgrade to "medium" until a more confident
      layer (or an analyst) corrects the category. Without this, e.g. a
      garbled k-NN guess of "corporate.merger" for an unrelated political
      market would page surveillance overnight.
    """
    if c.manipulability_prior is not None:
        return c
    base = prior_for(c.category, c.subcategory)
    if c.confidence == "low" and base in ("high", "medium_high"):
        base = "medium"
    return c.with_prior(base)
