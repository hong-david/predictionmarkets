"""Layer 3: k-NN classifier over character-n-gram TF-IDF.

When Layers 1 and 2 both miss — Kalshi's metadata is empty *and* the
ticker doesn't match any prefix rule — this layer steps in. It embeds
the market's title into a sparse character-n-gram vector, computes cosine
similarity against the seed corpus (`seed_data.py`), and votes among the
top-k nearest neighbours.

Why character n-grams over word tokens:
  Kalshi market titles are short, templated, and full of named entities
  ("Knicks", "Djokovic", "FOMC") that word-tokenisers either split badly
  or miss. Character n-grams (3-5 chars) handle those uniformly without
  language-specific tokenisation.

Why TF-IDF and not sentence-transformers / BERT:
  No torch dependency. The whole classifier compiles from a single Python
  file with stdlib + numpy. Sentence-transformers gives ~5-10pp better
  accuracy on natural-language tasks but for short templated titles the
  margin is much smaller, and we want this layer to be replaceable, not
  the system's centerpiece. If it ever becomes a bottleneck we can swap
  the `_Embedder` implementation; the public API of this layer doesn't
  change.

Why k-NN and not centroid / nearest-class-mean:
  k-NN with k>=3 gives us a natural confidence signal: agreement among
  neighbours = high confidence, disagreement = low confidence. The
  orchestrator uses that signal to decide whether to escalate to Layer 4
  (LLM) or accept this verdict.

Output contract:
  Returns `Classification` with `layer="knn_embedding"`. Confidence is
  derived from similarity-weighted voting:
    - `"high"`   if the winning class holds >=70% of the weighted vote OR
                 the top neighbour has cosine similarity >= 0.95.
    - `"medium"` if the winning class holds >=40%.
    - `"low"`    otherwise (the orchestrator escalates to Layer 4).
  Returns `None` only when the seed corpus is empty (degenerate case).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

from app.services.classifier.seed_data import SEEDS, Seed
from app.services.classifier.types import Classification, Confidence


_NGRAM_MIN: int = 3
_NGRAM_MAX: int = 5
_DEFAULT_K: int = 5

# Pre-compiled normaliser: lowercase, collapse whitespace, strip non-alnum
# except spaces. Matches what we apply during seed indexing too.
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")


def _normalise(text: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace."""
    s = text.lower()
    s = _NON_ALNUM_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _char_ngrams(text: str, n_min: int = _NGRAM_MIN, n_max: int = _NGRAM_MAX) -> Iterable[str]:
    """Yield all char n-grams in `text` for n in [n_min, n_max].

    Padded with leading / trailing spaces so word boundaries become part of
    the feature ("knicks " vs " knicks"), which lets short queries match
    seeds even when the seed has the same word as a substring of a
    different word.
    """
    padded = f" {text} "
    for n in range(n_min, n_max + 1):
        if len(padded) < n:
            continue
        for i in range(len(padded) - n + 1):
            yield padded[i : i + n]


class _Embedder:
    """TF-IDF embedder with cached IDF over the seed corpus.

    Lazy-built on first use. Rebuilds are O(seeds * avg_title_len) which is
    cheap (<10ms for 50 seeds), so we don't bother memoising across
    processes. The first call to `embed_query` triggers the index build.
    """

    def __init__(self, seeds: list[Seed]):
        self._seeds = seeds
        self._df: Counter[str] = Counter()
        self._n_docs: int = 0
        self._seed_vecs: list[dict[str, float]] = []
        self._seed_norms: list[float] = []
        self._built = False

    def _build(self) -> None:
        # Document frequency across the corpus, then per-seed sparse
        # TF-IDF vector. Vectors are dicts (token -> weight) since most
        # seeds share <2% of features and a dense matrix would be wasteful.
        df: Counter[str] = Counter()
        per_seed_tf: list[Counter[str]] = []

        for seed in self._seeds:
            tf: Counter[str] = Counter(_char_ngrams(_normalise(seed.title)))
            per_seed_tf.append(tf)
            for tok in tf:
                df[tok] += 1

        n = len(self._seeds)
        for tf in per_seed_tf:
            vec: dict[str, float] = {}
            for tok, count in tf.items():
                # Smoothed IDF (the +1 prevents log(0) for tokens that
                # appear in every doc).
                idf = math.log((n + 1) / (df[tok] + 1)) + 1.0
                vec[tok] = count * idf
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self._seed_vecs.append(vec)
            self._seed_norms.append(norm)

        self._df = df
        self._n_docs = n
        self._built = True

    def embed_query(self, text: str) -> tuple[dict[str, float], float]:
        """Return (sparse_vec, norm) for `text`.

        Uses the corpus IDF cached from `_build`. Tokens not seen in the
        corpus get IDF = log((n+1)/1) + 1, i.e. they're treated as maximally
        rare, which is the conservative choice — they can drive a match if
        they appear, but only against seeds that contain them.
        """
        if not self._built:
            self._build()

        tf = Counter(_char_ngrams(_normalise(text)))
        vec: dict[str, float] = {}
        for tok, count in tf.items():
            idf = math.log((self._n_docs + 1) / (self._df.get(tok, 0) + 1)) + 1.0
            vec[tok] = count * idf
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return vec, norm

    def neighbours(self, query: str, k: int) -> list[tuple[float, Seed]]:
        """Return top-k seeds by cosine similarity to `query`, descending."""
        if not self._built:
            self._build()

        q_vec, q_norm = self.embed_query(query)
        scores: list[tuple[float, Seed]] = []
        for seed, s_vec, s_norm in zip(self._seeds, self._seed_vecs, self._seed_norms):
            # Sparse dot product over the smaller vector for speed.
            if len(q_vec) <= len(s_vec):
                small, large = q_vec, s_vec
            else:
                small, large = s_vec, q_vec
            dot = 0.0
            for tok, w in small.items():
                if tok in large:
                    dot += w * large[tok]
            sim = dot / (q_norm * s_norm)
            scores.append((sim, seed))

        scores.sort(key=lambda x: x[0], reverse=True)
        return scores[:k]


# Module-level singleton. Built once per process on first classification
# call. ~1ms per query against a 50-seed corpus.
_EMBEDDER: _Embedder | None = None


def _get_embedder() -> _Embedder:
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = _Embedder(SEEDS)
    return _EMBEDDER


def reset_embedder_for_tests() -> None:
    """Tests can swap SEEDS and want a clean module state. Public on purpose."""
    global _EMBEDDER
    _EMBEDDER = None


def classify_via_knn(
    title: str,
    *,
    k: int = _DEFAULT_K,
    min_top_similarity: float = 0.05,
) -> Classification | None:
    """Classify `title` by majority vote over the top-k neighbours.

    `min_top_similarity` is a sanity floor: if the most-similar seed scores
    below this, the title is genuinely far from anything we've labelled
    and we return a low-confidence verdict so the orchestrator can route
    it to Layer 4 (LLM) instead of accepting a guess.

    The vote is over `(category, subcategory)` jointly. Tags returned are
    the union of all winning-class neighbour tags, which is a reasonable
    superset for downstream consumers.
    """
    if not title or not title.strip():
        return None
    if not SEEDS:
        return None

    embedder = _get_embedder()
    scored = embedder.neighbours(title, k)
    if not scored:
        return None

    top_score = scored[0][0]
    if top_score < min_top_similarity:
        # Cold-start / out-of-distribution title. Hand back a low-confidence
        # fallback verdict using the single best neighbour so something is
        # always returned, but flag it clearly.
        best_seed = scored[0][1]
        return Classification(
            category=best_seed.category,
            subcategory=best_seed.subcategory,
            layer="knn_embedding",
            rule=f"knn:cold_start:top={top_score:.3f}",
            confidence="low",
            tags=list(best_seed.tags),
        )

    # Similarity-weighted voting. Plain count-voting is wrong here because
    # k=5 means a sim=1.00 exact match for macro/cpi gets out-voted by
    # three sim<0.10 crypto seeds whose only overlap is generic tokens
    # like "above " or "% ". The similarity-weighted version treats the
    # exact match as worth what it actually is.
    votes_weighted: dict[tuple[str, str], float] = {}
    for sim, seed in scored:
        key = (seed.category, seed.subcategory)
        votes_weighted[key] = votes_weighted.get(key, 0.0) + sim

    winner = max(votes_weighted, key=lambda c: votes_weighted[c])
    winner_weight = votes_weighted[winner]
    total_weight = sum(votes_weighted.values()) or 1.0
    winner_share = winner_weight / total_weight

    confidence: Confidence
    if winner_share >= 0.7 or top_score >= 0.95:
        # Either the winner dominates the weighted vote, or the top
        # neighbour is essentially identical to the query — treat as a
        # near-deterministic match.
        confidence = "high"
    elif winner_share >= 0.4:
        confidence = "medium"
    else:
        confidence = "low"

    # Union of tags from neighbours that voted for the winning class.
    tag_set: set[str] = set()
    for _, seed in scored:
        if (seed.category, seed.subcategory) == winner:
            tag_set.update(seed.tags)

    return Classification(
        category=winner[0],
        subcategory=winner[1],
        layer="knn_embedding",
        rule=(
            f"knn:k={k}:share={winner_share:.2f}:top={top_score:.3f}"
        ),
        confidence=confidence,
        tags=sorted(tag_set),
    )
