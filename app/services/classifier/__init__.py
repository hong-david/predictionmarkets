"""Layered market classifier.

Public surface:

    from app.services.classifier import (
        classify,
        Classification,
        CLASSIFIER_VERSION,
    )

The orchestrator runs four layers in order and returns the first verdict it
trusts. Each layer is independently testable and replaceable; see the
module-level docstrings in `orchestrator.py` and the four `layer*` modules
for the per-layer contract.
"""

from app.services.classifier.orchestrator import classify
from app.services.classifier.types import (
    CLASSIFIER_VERSION,
    Classification,
    Confidence,
    ManipulabilityPrior,
)

__all__ = [
    "CLASSIFIER_VERSION",
    "Classification",
    "Confidence",
    "ManipulabilityPrior",
    "classify",
]
