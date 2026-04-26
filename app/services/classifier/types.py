"""Shared types for the layered classifier.

These are pure value objects — no Kalshi specifics, no DB coupling — so
every layer can produce / consume the same shape. The orchestrator combines
a layer's verdict with the priority map to fill in `manipulability_prior`
when a layer doesn't set one itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# Bumping this number forces the backfill script to reclassify every row
# whose stored `classifier_version` is < this value. Increment when:
#   - A new layer is introduced.
#   - A rule's category / subcategory / tags change in a way that should
#     overwrite previously-stored verdicts.
# Do NOT bump for cosmetic refactors (rename, comment) — that would force a
# full reclassify across hundreds of thousands of rows for no behaviour
# change.
CLASSIFIER_VERSION: int = 1


# Manipulability prior. The mapping from category/subcategory to one of
# these values is the one true human-judgment surface in the system; see
# priorities.py.
ManipulabilityPrior = Literal["very_low", "low", "medium", "medium_high", "high"]

# Confidence in the classification itself, NOT in the prior. Drives whether
# the orchestrator stops at this layer or escalates to the next one, and
# whether the dashboard surfaces a "needs review" badge.
#   - "high":   deterministic match (Kalshi taxonomy hit, prefix rule hit).
#   - "medium": agreement among neighbours in the embedding layer, or LLM
#               with high model-reported confidence.
#   - "low":    fallback / no signal / disagreement; analyst review.
Confidence = Literal["high", "medium", "low"]

# Which layer produced the verdict. Recorded on the Market row so an
# auditor can trace any classification back to its origin.
Layer = Literal[
    "kalshi_taxonomy",
    "prefix_rule",
    "knn_embedding",
    "llm_zero_shot",
    "fallback",
]


@dataclass(frozen=True)
class Classification:
    """The output of one classifier layer.

    `manipulability_prior` is left None by Layers 1-3 and the LLM layer; the
    orchestrator fills it in by looking up `(category, subcategory)` in the
    priority map. Keeping classification and priority assignment separate is
    the central design idea — see app/services/classifier/priorities.py.
    """

    category: str
    subcategory: str
    layer: Layer
    rule: str
    confidence: Confidence
    tags: list[str] = field(default_factory=list)
    manipulability_prior: ManipulabilityPrior | None = None

    def with_prior(self, prior: ManipulabilityPrior) -> "Classification":
        """Return a copy of this classification with `manipulability_prior` set."""
        return Classification(
            category=self.category,
            subcategory=self.subcategory,
            layer=self.layer,
            rule=self.rule,
            confidence=self.confidence,
            tags=list(self.tags),
            manipulability_prior=prior,
        )
