"""Registry of news sources and lightweight source-quality scoring.

The ingest pipeline stores schema-free article metadata. This registry keeps
source breadth, authority tiers, and diagnostics in code instead of scattering
feed URLs through the ingestor.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class NewsSource:
    key: str
    label: str
    url: str
    adapter: str = "rss"
    source_tier: str = "broad"
    authority_tier: str = "broad"
    topic_tags: tuple[str, ...] = ()
    enabled: bool = True
    requires_env: str | None = None
    notes: str | None = None

    @property
    def domain(self) -> str | None:
        parsed = urlparse(self.url)
        return parsed.netloc.lower() or None

    @property
    def available(self) -> bool:
        return self.enabled and (
            self.requires_env is None or bool(os.getenv(self.requires_env))
        )

    def as_diagnostic(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "url": self.url,
            "adapter": self.adapter,
            "source_tier": self.source_tier,
            "authority_tier": self.authority_tier,
            "topic_tags": list(self.topic_tags),
            "enabled": self.enabled,
            "available": self.available,
            "requires_env": self.requires_env,
            "notes": self.notes,
        }


DEFAULT_NEWS_SOURCES: tuple[NewsSource, ...] = (
    # Official / primary sources.
    NewsSource(
        "federal_register_recent",
        "Federal Register recent documents",
        "https://www.federalregister.gov/documents/search.rss?conditions%5Bpublication_date%5D%5Bis%5D=recent",
        source_tier="official",
        authority_tier="official",
        topic_tags=("regulation", "government"),
    ),
    NewsSource(
        "sec_edgar_current_8k",
        "SEC EDGAR current 8-K filings",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=100&output=atom",
        source_tier="official",
        authority_tier="official",
        topic_tags=("sec", "corporate", "filings"),
    ),
    NewsSource(
        "fda_medwatch",
        "FDA MedWatch safety alerts",
        "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch/rss.xml",
        source_tier="official",
        authority_tier="official",
        topic_tags=("fda", "health", "safety"),
    ),
    NewsSource(
        "congress_api_bills",
        "Congress.gov bills API",
        "https://api.congress.gov/v3/bill",
        adapter="congress_api",
        source_tier="official",
        authority_tier="official",
        topic_tags=("congress", "legislation", "government"),
        requires_env="CONGRESS_API_KEY",
        notes="Optional; enabled when CONGRESS_API_KEY is present.",
    ),
    NewsSource(
        "fed_press_all",
        "Federal Reserve press releases",
        "https://www.federalreserve.gov/feeds/press_all.xml",
        source_tier="official",
        authority_tier="official",
        topic_tags=("fed", "macro"),
    ),
    NewsSource(
        "sec_press",
        "SEC press releases",
        "https://www.sec.gov/news/pressreleases.rss",
        source_tier="official",
        authority_tier="official",
        topic_tags=("sec", "regulation"),
    ),
    NewsSource(
        "cftc_releases",
        "CFTC releases",
        "https://www.cftc.gov/RSS/RSSGP/rssgp.xml",
        source_tier="official",
        authority_tier="official",
        topic_tags=("cftc", "regulation"),
    ),
    NewsSource(
        "cftc_enforcement",
        "CFTC enforcement",
        "https://www.cftc.gov/RSS/RSSENF/rssenf.xml",
        source_tier="official",
        authority_tier="official",
        topic_tags=("cftc", "enforcement"),
    ),
    NewsSource(
        "ftc_press",
        "FTC press releases",
        "https://www.ftc.gov/feeds/press-release.xml",
        source_tier="official",
        authority_tier="official",
        topic_tags=("ftc", "antitrust"),
    ),
    NewsSource(
        "ftc_competition",
        "FTC competition releases",
        "https://www.ftc.gov/feeds/press-release-competition.xml",
        source_tier="official",
        authority_tier="official",
        topic_tags=("ftc", "competition"),
    ),
    NewsSource(
        "bls_empsit",
        "BLS employment situation",
        "https://www.bls.gov/feed/empsit.rss",
        source_tier="official",
        authority_tier="official",
        topic_tags=("macro", "employment"),
    ),
    NewsSource(
        "bls_cpi",
        "BLS CPI",
        "https://www.bls.gov/feed/cpi.rss",
        source_tier="official",
        authority_tier="official",
        topic_tags=("macro", "inflation"),
    ),
    NewsSource(
        "bls_latest",
        "BLS latest",
        "https://www.bls.gov/feed/bls_latest.rss",
        source_tier="official",
        authority_tier="official",
        topic_tags=("macro",),
    ),
    NewsSource(
        "eia_today",
        "EIA Today in Energy",
        "https://www.eia.gov/rss/todayinenergy.xml",
        source_tier="official",
        authority_tier="official",
        topic_tags=("energy", "macro"),
    ),
    NewsSource(
        "fda_press",
        "FDA press releases",
        "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
        source_tier="official",
        authority_tier="official",
        topic_tags=("fda", "health"),
    ),
    NewsSource(
        "noaa",
        "NOAA news",
        "https://www.noaa.gov/rss.xml",
        source_tier="official",
        authority_tier="official",
        topic_tags=("weather",),
    ),
    NewsSource(
        "nhc_atlantic",
        "National Hurricane Center Atlantic",
        "https://www.nhc.noaa.gov/index-at.xml",
        source_tier="official",
        authority_tier="official",
        topic_tags=("weather", "hurricane"),
    ),
    NewsSource(
        "nhc_pacific",
        "National Hurricane Center Pacific",
        "https://www.nhc.noaa.gov/index-ep.xml",
        source_tier="official",
        authority_tier="official",
        topic_tags=("weather", "hurricane"),
    ),
    # Specialist sources.
    NewsSource(
        "coindesk",
        "CoinDesk",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        source_tier="specialist",
        authority_tier="specialist",
        topic_tags=("crypto",),
    ),
    NewsSource(
        "cointelegraph",
        "Cointelegraph",
        "https://cointelegraph.com/rss",
        source_tier="specialist",
        authority_tier="specialist",
        topic_tags=("crypto",),
    ),
    NewsSource(
        "decrypt",
        "Decrypt",
        "https://decrypt.co/feed",
        source_tier="specialist",
        authority_tier="specialist",
        topic_tags=("crypto",),
    ),
    NewsSource(
        "yahoo_finance",
        "Yahoo Finance",
        "https://finance.yahoo.com/news/rssindex",
        source_tier="specialist",
        authority_tier="specialist",
        topic_tags=("markets", "business"),
    ),
    NewsSource(
        "marketwatch",
        "MarketWatch top stories",
        "https://www.marketwatch.com/rss/topstories",
        source_tier="specialist",
        authority_tier="specialist",
        topic_tags=("markets", "business"),
    ),
    NewsSource(
        "dow_jones_public",
        "Dow Jones public top stories",
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        source_tier="specialist",
        authority_tier="specialist",
        topic_tags=("markets", "business"),
    ),
    NewsSource(
        "cnbc_top",
        "CNBC top news",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        source_tier="specialist",
        authority_tier="specialist",
        topic_tags=("markets", "business"),
    ),
    NewsSource(
        "cnbc_world",
        "CNBC world news",
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",
        source_tier="specialist",
        authority_tier="specialist",
        topic_tags=("world", "business"),
    ),
    NewsSource(
        "cnbc_us",
        "CNBC US news",
        "https://www.cnbc.com/id/10000113/device/rss/rss.html",
        source_tier="specialist",
        authority_tier="specialist",
        topic_tags=("us", "business"),
    ),
    # Broad news.
    NewsSource("nytimes_home", "New York Times home", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    NewsSource("nytimes_world", "New York Times world", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", topic_tags=("world",)),
    NewsSource("nytimes_politics", "New York Times politics", "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml", topic_tags=("politics",)),
    NewsSource("nytimes_business", "New York Times business", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", topic_tags=("business",)),
    NewsSource("nytimes_tech", "New York Times technology", "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", topic_tags=("technology",)),
    NewsSource("guardian_world", "Guardian world", "https://www.theguardian.com/world/rss", topic_tags=("world",)),
    NewsSource("guardian_us", "Guardian US", "https://www.theguardian.com/us-news/rss", topic_tags=("us",)),
    NewsSource("guardian_business", "Guardian business", "https://www.theguardian.com/business/rss", topic_tags=("business",)),
    NewsSource("bbc_business", "BBC business", "https://feeds.bbci.co.uk/news/business/rss.xml", topic_tags=("business",)),
    NewsSource("bbc_world", "BBC world", "https://feeds.bbci.co.uk/news/world/rss.xml", topic_tags=("world",)),
    NewsSource("bbc_politics", "BBC politics", "https://feeds.bbci.co.uk/news/politics/rss.xml", topic_tags=("politics",)),
    NewsSource("npr_news", "NPR news", "https://www.npr.org/rss/rss.php?id=1001"),
    NewsSource("npr_business", "NPR business", "https://www.npr.org/rss/rss.php?id=1006", topic_tags=("business",)),
    NewsSource("politico", "Politico", "https://rss.politico.com/politics-news.xml", topic_tags=("politics",)),
    # Sports.
    NewsSource("espn_top", "ESPN top news", "https://www.espn.com/espn/rss/news", source_tier="sports", authority_tier="sports", topic_tags=("sports",)),
    NewsSource("espn_nfl", "ESPN NFL", "https://www.espn.com/espn/rss/nfl/news", source_tier="sports", authority_tier="sports", topic_tags=("sports", "nfl")),
    NewsSource("espn_nba", "ESPN NBA", "https://www.espn.com/espn/rss/nba/news", source_tier="sports", authority_tier="sports", topic_tags=("sports", "nba")),
    NewsSource("espn_mlb", "ESPN MLB", "https://www.espn.com/espn/rss/mlb/news", source_tier="sports", authority_tier="sports", topic_tags=("sports", "mlb")),
    NewsSource("espn_nhl", "ESPN NHL", "https://www.espn.com/espn/rss/nhl/news", source_tier="sports", authority_tier="sports", topic_tags=("sports", "nhl")),
    NewsSource("espn_soccer", "ESPN soccer", "https://www.espn.com/espn/rss/soccer/news", source_tier="sports", authority_tier="sports", topic_tags=("sports", "soccer")),
    NewsSource("guardian_sport", "Guardian sport", "https://www.theguardian.com/sport/rss", source_tier="sports", authority_tier="sports", topic_tags=("sports",)),
    NewsSource("cbs_sports", "CBS Sports headlines", "https://www.cbssports.com/rss/headlines/", source_tier="sports", authority_tier="sports", topic_tags=("sports",)),
    NewsSource("mlb_news", "MLB news", "https://www.mlb.com/feeds/news/rss.xml", source_tier="sports", authority_tier="sports", topic_tags=("sports", "mlb")),
)

_SOURCES_BY_URL = {s.url: s for s in DEFAULT_NEWS_SOURCES}
_SOURCES_BY_DOMAIN: dict[str, NewsSource] = {}
for _source in DEFAULT_NEWS_SOURCES:
    if _source.domain and _source.domain not in _SOURCES_BY_DOMAIN:
        _SOURCES_BY_DOMAIN[_source.domain] = _source


def default_rss_sources() -> tuple[NewsSource, ...]:
    return tuple(
        s
        for s in DEFAULT_NEWS_SOURCES
        if s.adapter == "rss" and s.available
    )


def default_rss_feed_urls() -> tuple[str, ...]:
    return tuple(source.url for source in default_rss_sources())


def source_for_url(url: str | None) -> NewsSource | None:
    if not url:
        return None
    if url in _SOURCES_BY_URL:
        return _SOURCES_BY_URL[url]
    domain = urlparse(url).netloc.lower()
    if not domain:
        return None
    return _SOURCES_BY_DOMAIN.get(domain)


def source_for_article(article: Any) -> NewsSource | None:
    canonical_url = getattr(article, "canonical_url", None)
    if canonical_url:
        source = source_for_url(canonical_url)
        if source is not None:
            return source
    domain = getattr(article, "domain", None)
    if domain:
        return _SOURCES_BY_DOMAIN.get(str(domain).lower())
    return None


def source_registry_diagnostics() -> dict[str, Any]:
    sources = [source.as_diagnostic() for source in DEFAULT_NEWS_SOURCES]
    enabled = [source for source in DEFAULT_NEWS_SOURCES if source.enabled]
    available = [source for source in DEFAULT_NEWS_SOURCES if source.available]
    return {
        "total": len(DEFAULT_NEWS_SOURCES),
        "enabled": len(enabled),
        "available": len(available),
        "rss_available": len(default_rss_sources()),
        "optional_unavailable": [
            source.as_diagnostic()
            for source in DEFAULT_NEWS_SOURCES
            if source.enabled and not source.available
        ],
        "sources": sources,
    }


def source_quality_component(article: Any) -> dict[str, Any]:
    source = source_for_article(article)
    tier = (
        getattr(article, "source_tier", None)
        or (source.source_tier if source is not None else None)
        or "unknown"
    )
    authority = source.authority_tier if source is not None else tier
    table = {
        "official": (1.08, 0.08, 0.20),
        "primary": (1.06, 0.06, 0.22),
        "specialist": (1.03, 0.03, 0.30),
        "sports": (1.02, 0.02, 0.32),
        "broad": (1.00, 0.00, 0.35),
        "gdelt": (1.00, 0.00, 0.35),
        "rss": (1.00, 0.00, 0.35),
    }
    multiplier, bonus, floor = table.get(str(authority), (1.0, 0.0, 0.35))
    return {
        "source_key": source.key if source is not None else None,
        "source_label": source.label if source is not None else None,
        "source_tier": tier,
        "authority_tier": authority,
        "multiplier": multiplier,
        "bonus": bonus,
        "minimum_score_for_bonus": floor,
    }


def apply_source_quality(score: float, article: Any) -> tuple[float, dict[str, Any]]:
    """Return source-adjusted relevance and an auditable component block."""

    component = source_quality_component(article)
    base = max(0.0, min(1.0, float(score or 0.0)))
    floor = float(component["minimum_score_for_bonus"])
    applied = base >= floor and (
        float(component["bonus"]) > 0.0 or float(component["multiplier"]) != 1.0
    )
    adjusted = base
    if applied:
        adjusted = min(
            1.0,
            base * float(component["multiplier"]) + float(component["bonus"]),
        )
    component["input_relevance_score"] = base
    component["adjusted_relevance_score"] = adjusted
    component["applied"] = applied
    return adjusted, component
