"""Layer 4: LLM zero-shot fallback.

The escape hatch for Layers 1-3. Reached only when:
  - Kalshi has no useful taxonomy for this market (Layer 1 miss), AND
  - No prefix rule fires (Layer 2 miss), AND
  - The k-NN layer's verdict was `confidence="low"` or there were no
    plausible neighbours (Layer 3 abstained).

What it does:
  Sends the market title (and optional subtitle) to a locally-running LLM
  with the candidate-category list, and parses a structured JSON response.
  No training, no fine-tuning, no labels. Off-the-shelf model used as a
  zero-shot text classifier.

Why this is wired through a Protocol:
  Two reasons. (1) The orchestrator should not couple to a specific LLM
  provider — switching from Ollama to a cloud API is one config change,
  not a refactor. (2) Tests need a deterministic stand-in; passing
  `NullLLMClassifier()` or a tiny fake from a test fixture keeps the
  orchestrator's behaviour testable without touching a network.

Default behaviour:
  Returns `NullLLMClassifier` from `default_llm_classifier()`. That
  classifier always returns `None`, which means in default deployment
  Layer 4 abstains and the orchestrator drops into the
  `fallback`/`other.unclassified` branch. Operators can flip this on by
  pointing `KalshiClassifierConfig.llm_classifier` at `OllamaLLMClassifier`.
  We don't enable Ollama by default because it requires a running daemon
  the user has to install and start themselves; silent failures from a
  missing daemon would be more surprising than the explicit opt-in.

Why Ollama and not OpenAI/Anthropic out of the box:
  Cost and trust boundaries. Ollama runs entirely on the host, has no API
  keys to provision, and does not exfiltrate market metadata to a third
  party. A cloud LLM is a one-file class away when needed, but the
  default should be the cheapest, most contained option.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

from app.services.classifier.types import Classification, Confidence


_LOG = logging.getLogger(__name__)


# Allowed category vocabulary handed to the LLM. Kept in sync with the
# priority map's top-level keys. The LLM's outputs are validated against
# this list and anything outside it gets coerced to `other` rather than
# trusted blindly.
ALLOWED_CATEGORIES: tuple[str, ...] = (
    "macro",
    "corporate",
    "judicial",
    "sports_outcome",
    "sports_derivative",
    "sports_prop",
    "crypto_strike",
    "weather",
    "election",
    "popculture",
    "exotic_combo",
    "other",
)


# Subcategory vocabulary handed to the LLM, organised by category. The
# orchestrator maps `(category, subcategory)` to a manipulability_prior;
# anything outside this vocabulary collapses to the per-category default.
ALLOWED_SUBCATEGORIES: dict[str, tuple[str, ...]] = {
    "macro": ("fed_decision", "cpi", "jobs", "gdp", "*"),
    "corporate": ("merger", "earnings", "fda", "*"),
    "judicial": ("scotus", "ruling", "*"),
    "sports_outcome": ("major_league_game", "fight_winner", "tennis_match", "soccer_match", "*"),
    "sports_derivative": ("spread", "total", "*"),
    "sports_prop": ("player_points", "first_event", "*"),
    "crypto_strike": ("short_window", "daily", "*"),
    "weather": ("temperature", "precipitation", "*"),
    "election": ("primary", "general", "*"),
    "popculture": ("awards", "ratings", "*"),
    "exotic_combo": ("*",),
    "other": ("unclassified",),
}


class LLMClassifier(Protocol):
    """The narrowest interface the orchestrator needs from any LLM provider.

    Implementations may be synchronous (Ollama via httpx) or already-async-
    wrapped (an OpenAI client). For now the orchestrator runs synchronously
    so this returns a plain `Classification | None`.
    """

    def classify(self, title: str, subtitle: str | None = None) -> Classification | None:
        ...


class NullLLMClassifier:
    """Default LLM classifier — always returns None.

    Letting the orchestrator route everything to the `fallback` branch
    when no LLM is configured. Cheap, deterministic, and safe for
    environments that don't want any external (or even local) inference
    dependency.
    """

    def classify(self, title: str, subtitle: str | None = None) -> Classification | None:
        return None


# Single source of truth for the prompt template. Kept here (and not
# inlined in OllamaLLMClassifier) so a unit test can import it and assert
# it never strays from `ALLOWED_CATEGORIES` / `ALLOWED_SUBCATEGORIES`.
_PROMPT_TEMPLATE = """You are classifying prediction-market questions for a financial-surveillance system.

Categories (pick exactly one):
{categories}

Subcategory must be one of the values listed for the chosen category, or "*":
{subcategories}

Tags (pick zero or more from): scheduled_announcement, single_actor_leverage, public_underlying, combat_sport.

Respond ONLY with compact JSON of the form:
{{"category": "...", "subcategory": "...", "tags": ["..."]}}

Market title: {title}
Market subtitle: {subtitle}
"""


def _build_prompt(title: str, subtitle: str | None) -> str:
    cats = ", ".join(ALLOWED_CATEGORIES)
    subs_lines = []
    for cat, subs in ALLOWED_SUBCATEGORIES.items():
        subs_lines.append(f"  - {cat}: {', '.join(subs)}")
    return _PROMPT_TEMPLATE.format(
        categories=cats,
        subcategories="\n".join(subs_lines),
        title=title,
        subtitle=subtitle or "",
    )


def _coerce_to_classification(raw: dict, source: str) -> Classification | None:
    """Validate an LLM JSON dict against the allowed vocabulary.

    LLMs occasionally hallucinate values outside the candidate list. Rather
    than trust them, we coerce out-of-vocabulary results to the safest
    in-vocabulary value (`other.unclassified`) and downgrade confidence.
    Returns None if the response is too malformed to recover anything from.
    """
    cat = raw.get("category")
    sub = raw.get("subcategory") or "*"
    tags = raw.get("tags") or []
    if not isinstance(cat, str):
        return None
    if not isinstance(tags, list):
        tags = []

    if cat not in ALLOWED_CATEGORIES:
        return Classification(
            category="other",
            subcategory="unclassified",
            layer="llm_zero_shot",
            rule=f"{source}:out_of_vocab:{cat}",
            confidence="low",
            tags=[],
        )

    confidence: Confidence
    if sub in ALLOWED_SUBCATEGORIES.get(cat, ()):
        confidence = "medium"
    else:
        sub = "*"
        confidence = "low"

    safe_tags = [str(t) for t in tags if isinstance(t, str)]
    return Classification(
        category=cat,
        subcategory=sub,
        layer="llm_zero_shot",
        rule=source,
        confidence=confidence,
        tags=safe_tags,
    )


class OllamaLLMClassifier:
    """LLM classifier backed by a locally-running Ollama daemon.

    Talks to Ollama's `/api/generate` HTTP endpoint with a short prompt and
    `format="json"` so we get parseable output without hand-rolling regexes.
    No SDK dependency: a single `httpx.post` is enough.

    Robustness contract:
      - If Ollama isn't running, returns None and logs once. Surveillance
        ingestion must never block on this layer.
      - If the model returns something un-parseable, returns None. The
        orchestrator's fallback path takes over.
      - If the model returns a valid-shaped JSON whose values are outside
        the allowed vocabulary, `_coerce_to_classification` downgrades to
        `other.unclassified` with confidence `low`.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "llama3.1:8b",
        timeout_s: float = 8.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def classify(self, title: str, subtitle: str | None = None) -> Classification | None:
        if not title or not title.strip():
            return None
        try:
            import httpx
        except ImportError:
            _LOG.warning("OllamaLLMClassifier requires httpx; returning None")
            return None

        prompt = _build_prompt(title, subtitle)
        try:
            response = httpx.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                },
                timeout=self.timeout_s,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            _LOG.warning("Ollama classify failed: %s", exc)
            return None

        raw_text = body.get("response", "")
        if not raw_text:
            return None
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            _LOG.warning("Ollama returned non-JSON: %s", raw_text[:200])
            return None
        if not isinstance(payload, dict):
            return None

        return _coerce_to_classification(payload, source=f"ollama:{self.model}")


# Hook used by the orchestrator. Tests can monkeypatch this to inject a
# fake. Production deployments swap it out by setting an env var or
# pointing it at an Ollama instance during app startup.
def default_llm_classifier() -> LLMClassifier:
    """Return the LLM classifier the orchestrator should use by default.

    Default is `NullLLMClassifier`: an explicit no-op, which means the
    fallback path triggers when Layers 1-3 all miss. Operators wanting the
    full 4-layer experience replace this with `OllamaLLMClassifier()` (or
    a cloud-API class) at process startup.
    """
    return NullLLMClassifier()
