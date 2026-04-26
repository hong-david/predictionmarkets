# Prediction Market Surveillance

A small market-surveillance backend for prediction markets (currently Kalshi). It ingests live market data over WebSocket and REST, persists it in Postgres, and runs detectors that flag potentially suspicious market activity. The project is being built deliberately — each addition is justified by the surveillance question it unlocks rather than by chasing scale for its own sake.

---

## 1. Overview

### What we are trying to achieve

Build a system that can identify **suspicious trading patterns** on a public prediction market with the constraints of public-only data (no counterparty / account identifiers). Specifically, the project targets a small, named taxonomy of patterns rather than one vague "anomaly score":

| # | Pattern | Detectable from public data? |
|---|---|---|
| 1 | Informed / leakage trading (price moves before public news, in the eventual winning direction) | Yes — needs trade tape + market resolution outcomes |
| 2 | Marking the close / settlement (small trades nudging price near resolution in thin liquidity) | Yes |
| 3 | Spoofing / layering (large orders added then quickly canceled) | Yes — needs order-book deltas |
| 4 | Wash trading (self-trades inflating volume) | Only suggestive signatures; full detection requires account IDs we do not have |
| 5 | Momentum ignition (deliberate burst of aggressive trades to start a move) | Yes |
| 6 | Cross-market manipulation (trades on market A move correlated market B) | Yes — needs related-market mapping |
| 7 | Quote stuffing (abnormal rate of order updates / cancellations) | Yes — needs order-book deltas |

### How we are approaching it

1. **Each pattern is a falsifiable hypothesis.** Rules are stated as testable predicates ("within N seconds of resolution, |Δprice| ≥ k σ of this market's recent return distribution and sign matches outcome"), not arbitrary thresholds.
2. **Per-market baselines, not global thresholds.** A 0.05 move is huge in a deep market and noise in a thin one — signals are scored against rolling per-market distributions (z-scores / percentile ranks) rather than fixed numbers.
3. **Resolution as ground truth.** Resolved markets give a free retrospective label for the leakage detector specifically. That is the one detector class where precision/recall can be measured cleanly.
4. **Replayable detectors.** Raw events (snapshots, trades, eventually order-book deltas) are persisted append-only so any detector can be re-run deterministically over history when its rule changes.
5. **Statistical sanity.** Permutation / bootstrap baselines and negative-control markets are used to confirm that detectors are finding real structure rather than noise.

This README is updated whenever the code changes.

---

## 2. System Architecture

### Component diagram

```mermaid
flowchart LR
    subgraph EXTERNAL[External]
        KREST[Kalshi REST API]
        KWS[Kalshi WebSocket\nticker + trade + orderbook_delta]
    end

    subgraph INGEST[Ingest]
        POLLER[scripts/poll_markets.py\n+ market_ingestor.py]
        WSCONS[scripts/run_ws_ticker_consumer.py\n-> kalshi_ws.consume_market_data_forever]
    end

    subgraph DB[Postgres]
        TMARKETS[(markets)]
        TSNAPS[(market_snapshots)]
        TTRADES[(trades)]
        TBOOK[(book_events)]
        TANOM[(anomalies)]
    end

    subgraph PROC[Processing]
        CLASSIFIER[classifier\nKalshi taxonomy -> prefix rules\n-> kNN -> LLM]
        ENGINE[anomaly_engine\nper-snapshot scoring]
        MAT[anomaly_materializer\nwrites/updates Anomaly rows]
    end

    subgraph SERVE[Serving]
        API[FastAPI app/main.py\n/health /markets /markets/.../features /anomalies]
    end

    KREST -->|GET /markets| POLLER
    KWS -->|ticker msg| WSCONS
    KWS -->|trade msg| WSCONS
    KWS -->|orderbook_snapshot / orderbook_delta| WSCONS

    POLLER -->|paginated cursor sweep\nupsert + snapshot| TMARKETS
    POLLER --> TSNAPS
    POLLER -->|raw market dict| CLASSIFIER
    CLASSIFIER -->|category, subcategory,\nmanipulability_prior, tags| TMARKETS
    WSCONS -->|lazy-upsert unknown ticker\nstatus='unknown' sentinel| TMARKETS
    WSCONS -->|new snapshot per ticker| TSNAPS
    WSCONS -->|new trade per trade msg\nINSERT ON CONFLICT DO NOTHING| TTRADES
    WSCONS -->|book event rows per snapshot/delta\nINSERT ON CONFLICT DO NOTHING| TBOOK

    WSCONS -->|trigger after snapshot| MAT
    MAT --> ENGINE
    ENGINE --> MAT
    MAT --> TANOM

    API --> TMARKETS
    API --> TSNAPS
    API --> TANOM

    classDef ext fill:#fef3c7,stroke:#d97706,color:#000
    classDef ing fill:#dbeafe,stroke:#2563eb,color:#000
    classDef db fill:#dcfce7,stroke:#16a34a,color:#000
    classDef proc fill:#ede9fe,stroke:#7c3aed,color:#000
    classDef api fill:#fee2e2,stroke:#dc2626,color:#000
    class KREST,KWS ext
    class POLLER,WSCONS ing
    class TMARKETS,TSNAPS,TTRADES,TBOOK,TANOM db
    class CLASSIFIER,ENGINE,MAT proc
    class API api
```

### How the pieces interact

- **REST poller** (`scripts/poll_markets.py` → `app/services/market_ingestor.py`) re-sweeps the open Kalshi universe on a 5-minute interval via `KalshiRestClient.iter_markets`, ingests in 500-row batches, and runs the anomaly materializer at the end of each cycle. The "sweep, don't single-page" shape is shared with the bootstrap script — see "Periodic universe sweep, not single-page polling" in design choices for why.
- **REST bootstrap** (`scripts/bootstrap_markets.py` → `KalshiRestClient.iter_markets`) does the same paginated cursor sweep but as a one-shot, no anomaly materialization, exits when the cursor is exhausted. Same chunked ingest helper (`market_ingestor.chunked`) so the two stay in lockstep.
- **Lazy-upsert hydrator** (`scripts/hydrate_unknown_markets.py` → `KalshiRestClient.get_market`) drains the WS-driven `status='unknown'` backlog by hitting Kalshi's per-ticker `/markets/{ticker}` endpoint. Necessary because the bulk poller is filtered by `status=open`, but Kalshi's actively-trading markets are typically `'active'` or `'finalized'` — exactly the ones that show up at the top of the dashboard. `--top-by-trades` orders the queue by descending trade count so the most-visible markets get human titles first. See "Targeted hydration of the lazy-upsert backlog" in design choices.
- **WebSocket consumer** (`scripts/run_ws_ticker_consumer.py` → `app/services/kalshi_ws.consume_market_data_forever`) holds a single authenticated WS connection to Kalshi and sends two `subscribe` commands on it: one for `ticker` + `trade` (no market filter), and one for `orderbook_delta` (with explicit `market_tickers`, since that channel requires market specification). On each `ticker`, it appends a `market_snapshots` row and re-runs the anomaly engine for that market. On each `trade`, it appends a `trades` row. On each `orderbook_snapshot`, it bulk-inserts one `book_events` row per price level. On each `orderbook_delta`, it appends a single `book_events` row. A fresh `session_id` UUID is generated per connection and stamped on every book event so Kalshi's per-subscription `seq` is interpretable across reconnects.
- **Market classifier** (`app/services/classifier/`) is a four-layer pipeline that decides what *kind* of market each row is and how much surveillance attention it deserves. It runs synchronously inside `ingest_markets_payload` (and as a one-shot via `scripts/classify_markets.py`), populates `markets.{category, subcategory, manipulability_prior, classifier_tags, classifier_layer, classifier_rule, classifier_confidence, classifier_version}`, and is short-circuited when the row is already at the current `CLASSIFIER_VERSION`. Layers 1 (Kalshi taxonomy) → 2 (~40 ticker prefix / regex rules) → 3 (k-NN over a small TF-IDF char-n-gram seed corpus) → 4 (optional LLM zero-shot fallback, default `NullLLMClassifier`). The first layer that returns a non-`low` confidence wins. A separate priority map in `priorities.py` maps `(category, subcategory) → manipulability_prior` and is the only human-judgment surface in the system. A safety clamp downgrades `high` / `medium_high` priors to `medium` whenever the classification's confidence is `low`, so a confused k-NN guess can't promote an unrelated market onto the high-prior watchlist. See "Layered classifier with explicit confidence" in design choices.
- **Anomaly engine** (`app/services/anomaly_engine.py`) is currently a per-snapshot rule scorer (wide spread, zero liquidity, empty book, sharp price move, volume jump). It returns a `score`, `severity`, list of `reasons`, and a JSON `signals` blob.
- **Materializer** (`app/services/anomaly_materializer.py`) calls the engine for a given market over its recent snapshots and either inserts a new `anomalies` row or updates the existing one keyed on `(market_pk, latest_snapshot_id)`.
- **Dashboard** (`app/api/routes/dashboard.py` → `/`) is a single self-contained HTML page that polls `/stats`, `/dashboard/top-markets`, and `/anomalies/stored` every 5 seconds and re-renders. Vanilla JS / no build step / no framework — see the design-choices section.
- **FastAPI** (`app/main.py` plus `app/api/routes/*`) exposes read APIs over the stored data: `health`, `markets` (list/get + snapshot history), `markets/{id}/features` (computed view of latest state), and `anomalies`.
- **Auth** (`app/services/kalshi_auth.py`) signs Kalshi requests with RSA-PSS over `{timestamp}{method}{path}` and emits the three `KALSHI-ACCESS-*` headers required by both REST and WS.

### Storage model

- **`markets`** — one row per market, keyed externally by `market_id` (Kalshi ticker). Owns `event_id`, status, open/close times, and the classifier output (`category`, `subcategory`, `manipulability_prior`, `classifier_tags`, `classifier_layer`, `classifier_rule`, `classifier_confidence`, `classifier_version`).
- **`market_snapshots`** — append-only time series of L1 quote + aggregates for a market (`yes/no bid/ask`, `last_price`, `volume_fp`, `volume_24h_fp`, `open_interest_fp`, `liquidity_dollars`).
- **`trades`** — append-only public-trade tape (`trade_id` unique, `taker_side`, `count_fp`, `yes_price_dollars`, `no_price_dollars`, event-time `ts`, ingest-time `received_at`).
- **`book_events`** — append-only order-book event log. Each row is one price level: snapshot rows (`is_snapshot=true`) carry the absolute level size in `size_fp`; delta rows carry a signed `delta_fp` (positive adds contracts, negative removes, zero removes the level). Stamped with `session_id` (per WS connection) and Kalshi's per-subscription `seq`. Idempotency is enforced by a unique constraint on `(session_id, seq, side, price_dollars)`.
- **`anomalies`** — materialized output of the anomaly engine per `(market, latest_snapshot_id)` with `score`, `severity`, `reasons`, JSON `signals`.

### Tooling

- **Postgres** via SQLAlchemy 2 (`psycopg` driver) with **Alembic** migrations.
- **FastAPI / Uvicorn** for the API.
- **websockets** + **cryptography** for Kalshi WS auth and feed.
- **pytest**, **ruff**, **mypy** for tests and linting.
- **Redis** is configured in `app/core/config.py` (`redis_host`, `redis_port`) and a URL helper exists, but it is **not yet wired into any code path**. It is reserved for the queue / worker layer planned later.

---

## 3. Design decisions and changelog

This section tracks the architectural decisions actually present in the code, plus a chronological log of meaningful changes. It is updated whenever the code is updated.

### Current design choices

- **Public data only, named-pattern surveillance.** No account-level data is available from Kalshi's public feed. The detector taxonomy in §1 was chosen so each pattern is either fully detectable from public data or explicitly scoped out (wash trading).
- **Append-only event tables.** `market_snapshots` and `trades` are append-only so detectors can be replayed deterministically against historical data when rules change.
- **Event time vs ingest time are stored separately.** `trades.ts` is the Kalshi `ts_ms` (the moment the trade executed); `trades.received_at` is when the row was inserted. Surveillance queries are event-time queries; `received_at` exists for clock-skew / pipeline-latency monitoring.
- **`Numeric`, not `float`, for prices and volumes.** Money- and contract-quantity-like fields are stored at exchange precision (`Numeric(12, 4)` for dollar prices, `Numeric(18, 2)` for `*_fp` quantities) to avoid binary-floating-point error.
- **Idempotent ingest via `INSERT … ON CONFLICT DO NOTHING`.** Reconnects can replay messages. Trade ingest dedupes on `trade_id`; book-event ingest dedupes on `(session_id, seq, side, price_dollars)`. Either way, duplicates are a single cheap statement, not an exception path.
- **One WebSocket connection, multiple channels, multiple `subscribe` commands.** `consume_market_data_forever` opens one authenticated WS and sends two `subscribe` commands on it — one for `ticker` + `trade` (no market filter), one for `orderbook_delta` with explicit `market_tickers`. Two commands rather than one because the channels need different `params` shapes; one connection rather than two because we want a single auth handshake, single heartbeat, and a single dispatcher in the message loop.
- **Per-connection `session_id` for the book stream.** Kalshi's `seq` is per-subscription and resets on reconnect, so it is not a globally stable identifier. We generate a UUID per WS connection and stamp it on every `book_events` row. Idempotency, gap detection, and replay all use `(session_id, seq)`; a new `is_snapshot=true` row inside a new `session_id` is the natural marker of a session boundary.
- **Selective `orderbook_delta` subscription, ranked by lifetime volume.** Unlike `ticker` and `trade`, the book channel requires explicit market tickers. The consumer takes either a configured `kalshi_book_market_tickers` list, or — when none is configured — picks the top `kalshi_book_market_limit` (default 50) markets by `volume_fp` (lifetime cumulative volume) from each market's most recent snapshot, with a `Market.updated_at desc` cold-start fallback when no snapshot has positive volume yet. Lifetime, not 24h, because the Kalshi WS ticker payload does **not** include `volume_24h_fp`, so a 24h ranker would silently exclude every market we've seen via WS — exactly the actively-trading set we care about. The set is resolved once per connection; updating it mid-session via `update_subscription` is deferred.
- **Lazy upsert of unknown tickers from the WS feed.** The `ticker` and `trade` channels are unfiltered, so the WS feed will emit messages for markets that are not yet in `markets` (Kalshi creates new BTC / sports markets continuously, and a one-shot REST bootstrap can be biased by Kalshi's alphabetical pagination). Rather than dropping those messages, `_get_or_create_market` does an idempotent `INSERT ... ON CONFLICT DO NOTHING` keyed on `markets.market_id` and re-selects, so every unknown ticker is auto-promoted to a stub `Market` row carrying `status='unknown'` as a sentinel. The REST poller is the single writer that hydrates the rest of the metadata (title, event_ticker, open/close times) on a later sweep. Orderbook handlers stay strict — by construction, anything we receive on `orderbook_delta` was a market we explicitly asked for, so an unknown ticker there would be a real bug rather than a discovery.
- **One row per price level on snapshots.** An `orderbook_snapshot` is expanded into N `book_events` rows in a single bulk `INSERT … ON CONFLICT DO NOTHING`. Uniform schema with delta rows means replays just walk events in order rather than branching on row shape.
- **Per-table replay-ordering indexes.** Each event table has a per-market access path tuned to its data, rather than one uniform composite. The dominant detector query is *"give me events for market M in order between t1 and t2"*; the right key for that query depends on whether the table's event-time column is reliable.
  - `trades` indexes `(market_pk, ts)`. `ts` (Kalshi's `ts_ms`) is non-null on every row and is what every detector joins on.
  - `book_events` indexes `(market_pk, id)` instead. Snapshot rows do not carry `ts_ms`, so `ts` is nullable; the autoincrement `id` is monotonic per insert and never null, which is what replays actually need.
  - `market_snapshots` currently has separate single-column `market_pk` and `ts` indexes (pre-existing); tightening to a composite `(market_pk, ts)` is an obvious follow-up but has not been done yet.
- **Unique external IDs as constraints.** `markets.market_id` and `trades.trade_id` are both indexed `UNIQUE`. Dedup is enforced by the database, not application logic.
- **Snapshot-based anomaly engine, scoped to evolve.** `anomaly_engine.analyze_market` currently uses static thresholds (spread > 0.10, |Δprice| ≥ 0.15, Δvolume ≥ 25). This is acknowledged as a placeholder; the planned next step is per-market rolling baselines (z-scores / percentile ranks) and pattern-specific detectors instead of a single summed score. Trade and `book_events` data are persisted but not yet read by the engine.
- **No in-memory order book yet.** `book_events` is persisted append-only so a live L2 book can be reconstructed when needed. Building and maintaining one in-process (the natural home for spoofing / quote-stuffing detectors) is deliberately deferred until the detector PR — keeping ingest and live-state concerns separate.
- **Per-message DB lookup for `market_pk`, with lazy upsert on miss.** Each handler resolves `market_ticker → market_pk` via a fresh DB query rather than caching the mapping. On a miss for `ticker` / `trade`, the handler falls through to the lazy-upsert path described above instead of dropping the message. This is intentionally naive in v1; if profiling under real book volume shows it as the bottleneck, an in-process LRU populated lazily (and invalidated when the REST poller updates a market) is the obvious next step.
- **Redis is declared but not used.** It is reserved for the planned queue / worker layer that will sit between WS ingest and the anomaly path. Wiring it in too early would be premature.
- **Backoff / reconnect on WS failures.** `consume_market_data_forever` reconnects with exponential backoff capped at 30s, so a transient Kalshi or network blip doesn't kill the consumer.
- **Periodic universe sweep, not single-page polling.** Both `scripts/bootstrap_markets.py` (one-shot) and `scripts/poll_markets.py` (interval-driven) walk Kalshi's `cursor`-based pagination via `iter_markets(status="open")` and ingest in fixed-size batches. The pre-fix poller called `get_markets(limit=25)` once per cycle, which meant the alphabetical front-load (~50k dead `KXMVECROSSCATEGORY...` markets) was the only thing it ever touched, and the lazy-upsert backlog from the WS feed was never hydrated. Sweeping the full open set is the only design that keeps the WS feed and REST metadata in sync without a per-ticker hydration endpoint. Defaults: `interval=300s`, `batch=500`. Both scripts share a `chunked()` helper in `app/services/market_ingestor.py` so the iteration shape stays in one place.
- **Targeted hydration of the lazy-upsert backlog (per-ticker REST).** The bulk `/markets` sweep is filtered by `status=open`, but Kalshi's high-trade-count markets right now (live NBA / MLB / UFC games, BTC 15-minute strikes that just expired) are `'active'` or `'finalized'`, not `'open'`. So the bulk sweep would never hydrate them and the dashboard would show them as `KXNBAGAME-...` ticker strings forever. `scripts/hydrate_unknown_markets.py` solves this by hitting Kalshi's per-ticker `/markets/{ticker}` endpoint for each row with `status='unknown'`. With `--top-by-trades` it orders the queue by descending `count(*)` from `trades`, so dashboard-visible markets get hydrated first. ~20 req/s with the default sleep is comfortably under any sane rate limit and drains 50 markets in ~25 seconds. This script is a backlog drainer, not a long-running daemon — it exits when the queue is empty.
- **Single-page server-rendered dashboard at `/`.** A self-contained HTML page (no Jinja templates, no Node, no JS framework) served by `app/api/routes/dashboard.py`. It polls three JSON endpoints — `/stats`, `/dashboard/top-markets`, `/anomalies/stored` — every 5s and re-renders. The goal is *demoability*, not a production UI: it exists so the project can be opened in a browser and visually communicate ingest progress + anomaly output, rather than asking a viewer to read raw JSON. A real dashboard (auth, charts, websocket push) is intentionally deferred until the surveillance engine is more than a static-threshold scorer.
- **Retention / scope policy in one place.** Whether a market deserves to be in the surveillance universe at all is a single decision encoded in `app/services/retention.py`: `EXCLUDED_CATEGORIES = {"exotic_combo", "crypto_strike"}` and `EXCLUDED_PRIORS = {"very_low"}`. Both the REST ingestor (`ingest_markets_payload`) and the WS lazy-upsert path (`_get_or_create_market`) call into the same predicate, so out-of-scope markets are dropped at the door — the classifier runs once per ingest, and the same `Classification` powers both the scope decision and the row stamping. The policy is a frozenset module constant rather than env config because "what does our system pay attention to" is the kind of decision a regulator wants to see in code review, not in a `.env`. Retroactive cleanup (`scripts/prune_markets.py`) reads the same predicate and uses the DB-level `ON DELETE CASCADE` (added in migration `f1a2c3d4b5e6`) so a single `DELETE FROM markets WHERE ...` cleans up every dependent row in `market_snapshots` / `trades` / `book_events` / `anomalies` — no slow ORM iteration, no orphaned children. Today's exclusions: Kalshi's `KXMVECROSSCATEGORY-*` parlay catalog (~285k rows of derived combinations whose manipulability-prior really should come from the constituent legs, which is a v2 problem) and `weather` (public physical underlying, no insider-leakable signal). Specifically *not* excluded: `low` priors like crypto strikes, because the trade tape itself can still surface spoofing / momentum-ignition signals that don't depend on the underlying being insider-leakable.
- **Layered classifier with explicit confidence.** Markets are classified into a category / subcategory / manipulability-prior tuple by a four-layer pipeline (`app/services/classifier/`): Kalshi taxonomy adapter → ticker / regex prefix rules → k-NN over a small char-n-gram TF-IDF seed corpus → optional LLM zero-shot fallback. The first layer that returns `confidence != "low"` wins; lower-confidence verdicts are kept only as fallback when every later layer also abstains. The priority map (`priorities.py`) is the *only* place a human value judgment lives in the codebase — the classifiers themselves are mechanical. Trade-offs the design pins down: (1) **Off-the-shelf, not a trained model.** No labels, no class-imbalance fixes, no retraining cadence, and the explanation `"matched_rule=sports.ufc"` is regulator-defensible in a way that a model output isn't. (2) **No torch / sentence-transformers dep.** The k-NN layer is pure-stdlib char-n-gram TF-IDF — slightly less accurate than a sentence transformer but ~5MB instead of ~700MB, and the public API of the layer is the same so we can swap implementations if accuracy ever becomes the bottleneck. (3) **Layer 4 (LLM) defaults to a no-op.** Setting `default_llm_classifier()` to return `NullLLMClassifier()` means the orchestrator has a 4-layer architecture but the default deployment runs only 3 — opting in to Ollama / a cloud LLM is one config change. (4) **Low-confidence safety clamp.** When the orchestrator's final verdict is `confidence="low"`, `manipulability_prior` is clamped down from `high`/`medium_high` to `medium` so a misclassified market can't escalate onto the high-prior watchlist. (5) **Versioning.** A `CLASSIFIER_VERSION` int travels with every classification; a backfill script reclassifies on bump. Cosmetic refactors don't touch it; rule / seed / map changes do.

### Recent changes

Most recent first.

#### 2026-04-25 — Retention follow-up: exclude crypto strikes; backfill stub backlog

After the initial retention pass landed, the live DB still had ~32k markets — high enough that we re-examined which buckets were genuinely surveillance-relevant. Two distinct sources of noise turned up.

**1. Crypto strikes are excluded by category, not by prior.** The original policy kept `crypto_strike` (`KXBTC15M-*`, `KXBTCD-*`, `KXETHD-*`, etc.) at `manipulability_prior='low'` on the theory that the trade tape itself could surface microstructure manipulation (spoofing, banging-the-close) even when the underlying is unmanipulable. That theory is sound in general but breaks on these specific markets:

  - The highest-volume strike in production carried ~$1-2k of notional. Realised profit from successful microstructure manipulation is *cents*; nobody operates a manipulation apparatus for cents.
  - The underlying price is set by global crypto markets that dwarf Kalshi by ~10,000x. Any in-Kalshi pressure gets arbed against spot/futures within milliseconds, so "banging the close" can't move resolution.
  - The regulatory-news angle (ETF approval, executive order) shows up on the *political* market that frames the decision — already classified as `macro` or `judicial` and kept at high prior. The crypto strike is a leveraged derivative bet on the same view, never the cleanest source of leakage.

  Concretely: added `crypto_strike` to `EXCLUDED_CATEGORIES`. We did *not* add `low` prior to `EXCLUDED_PRIORS` — `popculture.ratings` (the other resident of `low`) isn't an automated MM venue and has a much lower per-row noise floor; cost-benefit doesn't justify dragging it into the same bucket. The split (categories vs. priors) is the right granularity for this kind of policy decision: "which kinds of markets" and "which kinds of leakage potential" are independent axes.

**2. Stub markets escape both filters until the classifier runs on them.** Both retention filters check `markets.category` and `markets.manipulability_prior`. The WS lazy-upsert path stamps these on insert when the ticker matches a Layer 2 prefix rule, but ~13k existing rows had been inserted *before* the scope filter went live and still had `category=NULL`. They were inert backlog: status `'unknown'`, title equal to market_id, no metadata. The fix is operational, not structural: re-run `scripts/classify_markets.py`, which uses ticker / title / subtitle (Layers 2-4) to fill the verdict in 2.3s for 13k rows. Once classified, the prune script picks them up — 12,392 of them turned out to be `KXMVE...` exotic-combo parlays we already exclude, plus 265 crypto strikes (caught by the new exclusion above) and 206 weather rows (caught by `very_low`).

**Result:** DB went from 32,113 → **21,061 markets** in two prune passes (3,739 + 12,863), a 34% reduction on top of the earlier 92% drop. Trade and book_event tables are essentially intact (134k trades, 800k book events) — the deleted markets carried negligible activity, which is exactly the policy's premise.

Files touched:
  - `app/services/retention.py` — added `crypto_strike` to `EXCLUDED_CATEGORIES`; expanded the docstring with the crypto-specific economic argument and the explicit reason `low` prior is *not* added.
  - `tests/test_retention.py` — pinned the new policy contents; added explicit test `test_crypto_strike_category_is_excluded` covering all three subcategories; added `test_skips_crypto_strike` for the ingest path; updated `test_low_prior_is_kept` to use `popculture.ratings` (the remaining `low`-prior representative) so the intent is unambiguous.
  - `README.md` — design-choice entry updated to reflect the two-element `EXCLUDED_CATEGORIES`; this changelog entry.

#### 2026-04-25 — Retention / scope policy: drop noise at ingest, prune retroactively

The classifier (previous changelog entry) gave us per-row visibility into what each market is. This change uses that visibility to take action on markets we don't want to track at all. The motivating numbers: of 304,992 classified markets in the production DB, **285,444** were either Kalshi `KXMVECROSSCATEGORY-*` parlay-combination rows or `weather` rows whose manipulability prior is `very_low`. That's 93% of the table contributing zero surveillance signal — they bloat indexes, slow the dashboard, and wreck any "high-prior watchlist" cardinality metric we'd ever want to report.

The headline design idea is **one predicate, three callsites**: the same `is_in_scope(Classification) -> bool` function is used by (1) the REST ingestor, so out-of-scope markets never reach the DB; (2) the WS lazy-upsert path, so first-sighting tickers like `KXMVECROSSCATEGORY-...` never get stub Markets; (3) the prune script, so the existing backlog can be deleted with the same rule that prevents new ones. Without that consolidation, three separate filters drift apart silently and the answer to "what's in scope and why" becomes unauditable.

- **Policy module (`app/services/retention.py`).** `EXCLUDED_CATEGORIES = {"exotic_combo"}` and `EXCLUDED_PRIORS = {"very_low"}`, with three helpers: `is_in_scope(Classification)` for code paths that already have a classification; `is_market_in_scope(raw_kalshi_dict)` for the REST path (also returns the classification so the caller doesn't run the four-layer stack twice); `is_ticker_in_scope(ticker)` for the WS path where only the ticker string is available (Layer 2 prefix rules only). The conservative bias on tickers is "in scope unless a rule explicitly says otherwise" — a never-before-seen ticker that matches no prefix rule lands at `other.unclassified` with prior `medium` and is kept, which is correct because the WS feed is exactly where new market series first appear. The REST sweep refines the verdict on its next pass.
- **Migration (`alembic/versions/f1a2c3d4b5e6_…py`).** Adds `ON DELETE CASCADE` to all four FKs pointing at `markets.id` (`market_snapshots`, `trades`, `book_events`, `anomalies`). Previously cascade was declared only at the SQLAlchemy ORM relationship level, which is silent at the SQL layer — a raw `DELETE FROM markets WHERE ...` failed on FK violations, forcing per-row ORM iteration that would have taken ~hours on a 285k delete. With DB-level cascade it took 29s.
- **REST ingest hook.** `ingest_markets_payload` now classifies *first*, checks scope, and `continue`s on out-of-scope rows before any DB I/O. The classification it computes for the scope check is reused for the row stamping when the market is in scope, so the four-layer stack runs exactly once per ingested market regardless of outcome. New return-dict counter: `skipped_out_of_scope`. Existing in-DB rows that *were* in scope and are no longer (e.g. a category that's been removed from the policy) are left alone — removal is the prune script's job, not the ingestor's, so a misconfig of the policy can't accidentally destroy data.
- **WS lazy-upsert hook.** `_get_or_create_market` runs the ticker-only classifier and returns `None` on out-of-scope tickers, which the existing callers already treat as "skip this message". When the upsert *does* happen, the stub market gets stamped with the ticker-only classification immediately rather than waiting for the next REST sweep — so even WS-first markets show up in the dashboard with a category and a prior right away.
- **Prune script (`scripts/prune_markets.py`).** Default `--dry-run` prints a categorised breakdown (by category, by manipulability_prior) of what would be deleted; `--execute` actually deletes; `--category` / `--prior` flags narrow to a single bucket for partial cleanups. Uses `db.query(Market).filter(...).delete(synchronize_session=False)` to skip the ORM identity-map maintenance that would otherwise dominate runtime on a 285k delete. With the new DB-level cascade, child rows go with the parent in a single transaction.
- **Cleanup run on production DB.** The first invocation matched 285,444 rows (284,882 `exotic_combo` + 562 `weather`); deleted in 28.9s. A reclassify of WS-stub backlog (`scripts/classify_markets.py`) then surfaced a second 17,363-row batch (mostly WS-lazy-upserted `KXMVECROSSCATEGORY-*` from before the WS hook landed); pruned in 1.5s. Final state: **21,531 markets** (93% reduction), **782 high-prior + high-confidence** rows in the trustable watchlist, **4,595** in the broader (high or medium_high prior + high confidence) watchlist. Dependent table integrity preserved by the cascade — no orphaned snapshots / trades / anomalies.
- **Tests (`tests/test_retention.py`, 14 tests).** `is_in_scope` cases for each prior / excluded-category combination; `is_market_in_scope` against representative real Kalshi dict shapes (CPI in scope, exotic combo excluded); `is_ticker_in_scope` for known prefixes / exotic prefixes / unknown prefixes; ingest-shape tests using the same `_FakeSession` pattern as `test_kalshi_ws_handlers.py` to verify out-of-scope markets produce zero `add()` calls and the `skipped_out_of_scope` counter is surfaced. Plus a guard test that pins the policy-set contents to what the README claims, so a silent policy change can't drift past code review.
- **Lint + types.** `ruff check` clean across all 6 changed files; `mypy` clean (the existing 7 issues in `anomaly_engine.py` are unchanged and tracked separately).

#### 2026-04-25 — Layered market classifier (Kalshi taxonomy → prefix rules → k-NN → LLM)

Surveillance is fundamentally a triage problem: not every market deserves the same level of attention. Until now we had no way to answer "is this market in the high-priority watchlist?" — every market was treated identically by the anomaly engine. This change introduces a layered classifier that produces, per market, a `(category, subcategory, manipulability_prior)` tuple plus an audit trail of which layer / rule decided.

The headline design idea is to **separate classification from priority assignment**. Classification (which is mechanical text-mapping) is automated through four layers; priority assignment (which is irreducibly a human value judgment about what insider trading looks like in each kind of market) lives in a tiny ~30-row table that gets quarterly review, not per-market analyst edits. This makes the human-maintenance surface dramatically smaller than a "rule per ticker" approach without giving up auditability — every classification has a traceable `classifier_layer` + `classifier_rule` recorded on the row.

- **Schema (`alembic/versions/d5e9f120ab37_…py`).** Eight new columns on `markets`: `category`, `subcategory`, `manipulability_prior`, `classifier_tags` (JSON), `classifier_layer`, `classifier_rule`, `classifier_confidence`, `classifier_version`. Indexes on `category`, `subcategory`, `manipulability_prior`, `classifier_confidence`, `classifier_version` so dashboard / surveillance queries don't seq-scan.
- **`app/services/classifier/` package.**
  - **Layer 1 — Kalshi taxonomy adapter (`layer1_kalshi.py`).** Reads Kalshi's own `tags` array on the raw market dict and maps it to our taxonomy. ~30 tag rules covering FOMC / CPI / NFP / SCOTUS / UFC / NBA / Bitcoin / weather / awards / etc. Falls back to Kalshi's top-level `category` on tag miss. Deliberately ignores the *title* — that's Layer 3's job. Returns `confidence="high"` on hit, None on miss.
  - **Layer 2 — ticker prefix / regex rules (`layer2_rules.py`).** A first-match-wins list of ~40 rules covering every ticker prefix in production today: `KXBTC15M` / `KXBTCD` / `KXFOMC` / `KXCPI` / `KXNFP` / `KXSCOTUS` / `KXEARN` / `KXMERGER` / `KXUFC` / `KXBOX` / `KXNBA[GAME|SPREAD|TOTAL|PTS]` / `KXNFL[…]` / `KXMLB[…]` / `KXNHL` / `KXTENNIS` / `KXSOCCER` / `KXGOLF` / `KXHIGHTEMP` / `KXRAIN` / `KXPRES` / `KXPRIMARY` / `KXOSCAR` / `KXMVECROSSCATEGORY` plus generics. More-specific rules (NBA spread / total / player props) are listed before the generic NBA-game rule so they win conflict resolution. Returns `confidence="high"` on hit.
  - **Layer 3 — k-NN over char-n-gram TF-IDF (`layer3_knn.py` + `seed_data.py`).** When Layers 1+2 both miss, the title is embedded into a sparse char-n-gram (3-5 char) TF-IDF vector, and similarity-weighted voting over the top-k=5 seeds picks the winning `(category, subcategory)`. Pure stdlib — no torch, no sklearn, ~250 lines. Confidence is `"high"` if the winner takes ≥70% of the weighted vote OR the top neighbour is essentially identical (sim ≥ 0.95), `"medium"` if ≥40%, `"low"` otherwise. The seed corpus has ~50 hand-classified examples covering every category. Plain count-voting was tried first and rejected: a sim=1.00 exact-match seed got out-voted by three sim<0.10 unrelated seeds whose only overlap was the word "above".
  - **Layer 4 — LLM zero-shot (`layer4_llm.py`).** A `Protocol` with three implementations: `NullLLMClassifier` (default — always abstains), `OllamaLLMClassifier` (talks to a local Ollama daemon via `/api/generate` with `format=json`), and a stub seam where a cloud-LLM client would slot in. Validates LLM output against the allowed category / subcategory vocabulary; out-of-vocab values are coerced to `other.unclassified` with `confidence="low"` rather than trusted blindly. Network failures degrade gracefully to None — the surveillance pipeline never blocks on this layer. Default deployment runs the first 3 layers and lets Layer 4 abstain; flipping `default_llm_classifier()` enables Ollama.
  - **Priority map (`priorities.py`).** A static dict of `(category, subcategory) → manipulability_prior`. ~30 lines. The *one* human-judgment surface in the system — every other file is mechanical. Lookup precedence: exact match → per-category wildcard `(cat, "*")` → global `(other, unclassified)` so every classification gets a prior.
  - **Orchestrator (`orchestrator.py`).** Runs the four layers in order; first non-`low`-confidence verdict wins. Keeps the best low-confidence answer if every layer ends up low. Applies the priority map as a final step, with a **low-confidence safety clamp** that downgrades `high` / `medium_high` priors to `medium` whenever the verdict's confidence is `low` — without it, a confused k-NN guess of "corporate.merger" for a Brazilian soccer match would have promoted that match into the FOMC-grade watchlist.
- **Ingest hook.** `market_ingestor.ingest_markets_payload` now stamps every upserted market with the classifier's output (skipping rows already at the current `CLASSIFIER_VERSION`). The REST poller runs the full 4-layer pipeline against the raw Kalshi dict (so Layer 1 has access to `tags`); the WS lazy-upsert path doesn't classify (no Kalshi metadata, the next REST sweep will catch up).
- **Backfill script (`scripts/classify_markets.py`).** Reclassifies stale rows on demand. `--top-by-trades` orders the queue by descending trade count so dashboard-visible markets are processed first; mirrors `hydrate_unknown_markets.py`. `--all` forces every row, `--max` caps the run, `--batch` controls commit frequency.
- **Smoke test** (top-500 markets by trade count, real DB):
  ```
  classifier: done 500 rows in 0.4s (1386.5/s)
  classifier: by layer = {prefix_rule: 341, knn_embedding: 159}
  classifier: by confidence = {high: 343, low: 110, medium: 47}
  ```
  ~1400 rows/sec on a single Python process. ~78% high+medium confidence. The 110 low-confidence rows are the analyst-review queue; they're capped to `medium` prior by the safety clamp until either the LLM layer is enabled or a human upgrades them. The high-prior watchlist after the clamp is exclusively UFC fight winners (every row `confidence=high`), which is exactly what surveillance should be focused on for combat sports.
- **Tests (`tests/test_classifier.py`, 58 tests).** Per-layer tests for Layer 1 (tag matching, category fallback, tag-precedence-over-category), Layer 2 (parametrised over 22 representative tickers, plus rule-priority-map consistency), Layer 3 (obvious matches, cold-start handling, similarity-weighted voting), Layer 4 (vocabulary validation, malformed-input handling, network-failure graceful degradation), and the orchestrator (escalation order, prior-map attachment, low-confidence clamp, injected-LLM precedence).
- **Lint + types.** `ruff check` and `mypy` clean across all 12 changed source files; the existing 7 mypy issues in `anomaly_engine.py` are unchanged and tracked separately.

#### 2026-04-25 — Dashboard shows human titles; new lazy-upsert hydrator

After the dashboard went live we noticed every "Top markets by trade count" row was a raw ticker string like `KXNBAGAME-26APR25NYKATL-NYK` rather than something readable. The cause was downstream of the dashboard: the rows were lazy-upsert stubs (`status='unknown'`, `title=market_id`) and the bulk REST poller — filtered by `status=open` — was structurally incapable of ever hydrating them, because Kalshi's actively-trading markets are stamped `status='active'` or `'finalized'` (15-minute BTC strikes, live games), not `'open'`. The bulk sweep is the wrong tool for that backlog.

- **New `KalshiRestClient.get_market(ticker)`** wraps Kalshi's per-ticker `/markets/{ticker}` endpoint. Returns the `market` dict on 200, `None` on 404, raises on other 4xx/5xx (the script treats those as transient errors and counts them).
- **New `scripts/hydrate_unknown_markets.py`** is a one-shot backlog drainer. Selects all `Market` rows with `status='unknown'`, optionally re-orders them by descending `count(trades)` via `--top-by-trades` (so the dashboard's visible rows get hydrated first), then hits `get_market` per ticker with a configurable `--sleep` between calls, reusing `ingest_markets_payload` so the upsert path is identical to the bulk poller. Empirically: ~20 req/s, 50 markets hydrated in 25s, no errors against live Kalshi.
- **Dashboard JS now renders titles, not just tickers.** A new `renderMarketCell()` helper checks `title === market_id` (the lazy-upsert sentinel) and shows either the human title with the ticker as a smaller, muted, monospace subtitle, or — when metadata isn't yet hydrated — an italic "metadata pending" line above the ticker. CSS adds `.title` and `.title.pending` classes so the two states are visually distinct without screaming. Same helper is used by both panels (Top markets, Persisted anomalies); the anomalies endpoint already returned `title`, so no API change there.
- **Tests:** `test_kalshi_rest.py` adds three tests for `get_market` covering 200 / 404 / 5xx via a stubbed `httpx.Client`.
- **Lints + types:** `ruff check`, `mypy` on the changed files, and the existing `pytest` suite are all green.

#### 2026-04-25 — Poller does a paginated universe sweep, not a 25-row peek

`scripts/poll_markets.py` had the same single-page bug `bootstrap_markets.py` had pre-fix: every 30 seconds it called `KalshiRestClient.get_markets(limit=25)` and called it a day. Result: the poller spent its life rewriting the same 25 alphabetically-first dead exotic markets and never hydrated the ~7,700 lazy-upserted `status='unknown'` stubs the WS consumer had created. Without that hydration, the dashboard's "Markets" stat shows a backlog that just grows.

- Rewrote `scripts/poll_markets.py` to walk `iter_markets(status="open")` and ingest in 500-row batches, sleeping 5 minutes between cycles. Each cycle ends with the existing `materialize_anomalies` call so anomaly state stays current.
- Added `--once`, `--max`, `--batch`, `--interval`, `--anomaly-market-limit`, `--anomaly-lookback`, and `--status` flags so the same script can be a long-running daemon, a cron job, or a smoke test (`--once --max 600`).
- Extracted the `_chunked()` helper that was hiding inside `bootstrap_markets.py` into `app.services.market_ingestor.chunked`. Both scripts now import it. There's now exactly one batch-then-ingest pattern in the codebase rather than two near-identical copies.
- Smoke test: `--once --max 600 --batch 200` against live Kalshi finished in 3.2s, ingested 600 markets across 3 batches, materialized 100 fresh anomalies. `make test` (23 passed, 10 skipped — integration suite, expected) and `ruff check` are both clean. `mypy` clean on the changed files; the 7 pre-existing errors in `anomaly_engine.py` are unchanged and tracked separately.
- Companion README cleanup: the "REST poller" and "REST bootstrap" bullets in *How the pieces interact* now describe what these scripts actually do, and a new design-choice entry — "Periodic universe sweep, not single-page polling" — names the trade-off out loud.

#### 2026-04-25 — Browser dashboard at `/`

Until now, "view in browser" meant either Swagger (`/docs`) or hitting JSON endpoints directly — fine for an API smoke test, useless as a demo. Added a self-contained dashboard page that turns the existing JSON surface into something visual.

- New `app/api/routes/dashboard.py` exposes three things: a `/stats` endpoint with row counts across `markets`, `market_snapshots`, `trades`, `book_events`, `anomalies` (plus the `markets_status_unknown` count, which is interesting on its own — it's the lazy-upsert backlog the REST poller hasn't hydrated yet); a `/dashboard/top-markets` endpoint that joins `markets` with a `count(*)` of `trades` so the dashboard can rank by activity rather than insert recency; and a `/` route that returns a single HTML page.
- The HTML page is intentionally inline in the Python module — no Jinja, no template directory, no Node build, no framework. It's vanilla JS using `fetch()` to hit the three endpoints on a 5-second interval and re-render. Severity badges colour-code high/medium/low. Tickers are clickable links into the per-market JSON endpoints, so drill-down still works.
- This is a v1 demo dashboard, not a production UI. Auth, websocket push, and charting are deferred; calling `count(*)` on every poll is fine because the tables are small and the page polls every 5s, not every request. The README "design choices" section has a corresponding entry that names this trade-off out loud.

#### 2026-04-25 — Live smoke test → REST pagination, lazy upsert, volume-ranked book subscription

A first end-to-end run of `scripts/run_ws_ticker_consumer.py` against live Kalshi surfaced three real bugs that mocked unit tests had cleanly hidden. Each is now fixed and tested.

**1. Single-page REST client → `iter_markets` cursor sweep.**
`KalshiRestClient.get_markets` only ever fetched one page (no `cursor` support, no `status` filter), so the bootstrap had landed ~400 exotic combinatorial markets and zero of the actively-trading mainstream ones. Live trade-tape messages matched **0%** of our `markets` table.

- `KalshiRestClient` now accepts `cursor` and `status`, and exposes `iter_markets(status="open", limit=1000, max_markets=None)` which walks the cursor until it's empty.
- `scripts/bootstrap_markets.py` was rewritten to consume that generator and `argparse`-driven flags (`--status`, `--max`, `--batch`). Default behaviour is "sweep all open markets in 500-row batches with running progress lines."
- `tests/test_kalshi_rest.py` adds 9 unit tests covering cursor follow-through, the empty-page and empty-cursor termination cases, the `max_markets` cap, and that `status` / `limit` are forwarded on every page call.

**2. Stale market universe → lazy upsert in WS handlers.**
Even after fixing pagination, Kalshi's `/markets` endpoint front-loads tens of thousands of dead `KXMVECROSSCATEGORY` / `KXMVESPORTSMULTIGAMEEXTENDED` combinatorial markets, so a bounded one-shot bootstrap cannot guarantee that the actively-trading mainstream markets are present at consumer startup. Dropping every "unknown ticker" message is the wrong default for surveillance.

- New `_get_or_create_market(db, market_ticker)` in `app/services/kalshi_ws.py` does an idempotent `INSERT ... ON CONFLICT DO NOTHING` keyed on `markets.market_id` and re-selects, returning a stub Market row carrying `status='unknown'` as a sentinel for "seen on the wire, REST metadata not yet hydrated."
- `handle_ticker_message` and `handle_trade_message` now route through this helper instead of skipping unknown tickers.
- `handle_orderbook_*` handlers stay strict: by construction the orderbook subscription is for an explicit list of tickers that already exist in `markets`, so an unknown ticker there is a real bug rather than a discovery.
- The unit test for the old "skip" behaviour was rewritten as `test_handle_trade_message_lazily_upserts_unknown_market`, which asserts the upsert call shape via a `pg_insert` recorder.
- Two integration tests (`test_lazy_upsert_creates_stub_market`, `test_lazy_upsert_is_idempotent_under_repeated_calls`) cover the round-trip in real Postgres, including that two trades on a brand-new ticker produce exactly one Market row.

**3. `Market.updated_at desc` was the wrong ranker → volume-ranked book subscription.**
Even with a healthy universe, `resolve_book_market_tickers` was picking the 50 most-recently-inserted markets, which on a fresh bootstrap meant 50 dead exotic markets and zero `orderbook_snapshot` events for a full minute. The surveillance-correct ranker is by *trading activity*, not by *insert recency*.

- `resolve_book_market_tickers` now ranks by `volume_fp` (lifetime cumulative volume) from each market's most recent snapshot, picked via Postgres `DISTINCT ON (market_pk) ... ORDER BY market_pk, ts DESC` for one (latest) snapshot per market in a single index pass. Markets with `status IN ('active','open','unknown')` are eligible — `'unknown'` is included so lazy-upserted markets are first-class subscription candidates.
- `volume_fp` rather than `volume_24h_fp` because the Kalshi WS ticker payload does not include `volume_24h_fp`, so each WS-driven snapshot would overwrite it with NULL and a 24h ranker would silently drop every market we've seen via WS.
- A `Market.updated_at desc` fallback covers the cold-start case where no snapshot has positive volume yet.
- Two integration tests cover both branches: `test_resolve_book_market_tickers_ranks_by_volume` populates three markets with descending lifetime volume and asserts the ordering, and `test_resolve_book_market_tickers_falls_back_when_no_snapshots` clears `market_snapshots` first and asserts the fallback path is hit.
- The corresponding unit test for the DB path was removed, since faking SQLAlchemy core query / `select` shapes is fragile and the integration test exercises the real query plan.

**Smoke-test deltas after the fixes (60-second window against live Kalshi):**

| Metric | Before any fix | After lazy upsert | After lazy upsert + volume-ranked subscription |
|---|---|---|---|
| `trades` ingested | 0 | 1,118 | 1,201 |
| `book_events` ingested | 0 | 0 | **6,859** (6,037 snapshot rows + 822 deltas) |
| `markets` auto-discovered (`status='unknown'`) | 0 | 4,548 | 4,548+ |

Top book-receiving markets after the third fix were exactly the ones surveillance should care about: current MLB / NBA / NHL games, governor races, and Fed-decision markets — not exotic combinatorial bets.

#### 2026-04-25 — Test suite + Postgres integration tests

- New `tests/` package with `conftest.py` that pins env vars to a test DB (`surveillance_test`) before any `app.*` import, so tests cannot accidentally touch a developer's real database.
- `tests/test_kalshi_ws_handlers.py` covers the WS handlers' branches that don't need a DB: malformed-payload guards on `handle_trade_message` and `handle_orderbook_delta_message`, the snapshot row-expansion shape for `handle_orderbook_snapshot_message`, and `resolve_book_market_tickers` precedence (config beats DB; DB query is only run when config is empty). 15 unit tests, no DB required.
- `tests/test_migrations_integration.py` runs against a real Postgres (gated on `RUN_INTEGRATION=1`). It drops the public schema, runs `alembic upgrade head`, asserts that all five tables exist with their unique constraints and composite indexes, and then exercises both `Trade`'s DB-level dedup (raises `IntegrityError`) and `BookEvent`'s `INSERT … ON CONFLICT DO NOTHING` (second identical insert is a true no-op). 6 integration tests.
- Fixed one mypy attr-defined error introduced earlier: `db.execute(stmt).rowcount` in `handle_trade_message` is annotated with `# type: ignore[attr-defined]` since SQLAlchemy 2's static return type is `Result[Any]` while the runtime object is a `CursorResult`. The 7 other mypy errors are pre-existing in `app/services/anomaly_engine.py` (the standard `Numeric` → `Decimal` typing gap) and are not session-introduced.
- Removed the obsolete `version: "3.9"` field from `docker-compose.yml`.

#### 2026-04-25 — Add `orderbook_delta` ingestion

- Added `BookEvent` model in `app/db/models.py`. One row per price level: `is_snapshot=true` rows carry absolute level size in `size_fp`; delta rows carry signed `delta_fp`. Stamped with per-connection `session_id` (UUID) and Kalshi's per-subscription `seq`. Unique constraint on `(session_id, seq, side, price_dollars)` enforces idempotency.
- New Alembic migration `c4d8f2e0a91b_create_book_events_table.py` (`down_revision = b3a7e4c19f02`).
- New `handle_orderbook_snapshot_message` (bulk insert, one row per level) and `handle_orderbook_delta_message` (single-row insert) in `app/services/kalshi_ws.py`. Both use `INSERT … ON CONFLICT DO NOTHING`.
- `consume_market_data_forever` now generates a `session_id` UUID per connection and sends a second `subscribe` command for `orderbook_delta` with explicit `market_tickers`. Resolved via new `resolve_book_market_tickers` helper from either a configured allowlist or the DB.
- Added `kalshi_book_market_tickers: list[str]` and `kalshi_book_market_limit: int = 50` to `app/core/config.py`.
- Book events do **not** trigger any detector yet; persistence-only step.

#### 2026-04-25 — Add public trade tape ingestion

- Added `Trade` model in `app/db/models.py`: `trade_id` unique, event-time `ts` indexed, composite `(market_pk, ts)` index for replay queries, `received_at` server-default for ingest time.
- New Alembic migration `b3a7e4c19f02_create_trades_table.py` creates the `trades` table and its indexes; `down_revision = da2173380c55` (current head).
- New `handle_trade_message` in `app/services/kalshi_ws.py` uses Postgres `INSERT … ON CONFLICT (trade_id) DO NOTHING` so reconnect-replays are idempotent and cheap.
- Renamed `consume_ticker_forever` → `consume_market_data_forever` and changed the subscribe payload from `["ticker"]` to `["ticker", "trade"]`. The message loop dispatches on `msg_type`.
- Updated `scripts/run_ws_ticker_consumer.py` to call the renamed entry point.
- Trades do **not** trigger the snapshot-based anomaly engine; coupling was deferred until the engine moves to trade-driven, baseline-aware detectors.

#### Pre-existing baseline (before this README)

- FastAPI app with routers `health`, `markets`, `markets/{id}/features`, `anomalies`.
- SQLAlchemy 2 declarative models for `Market`, `MarketSnapshot`, `Anomaly` with Alembic migrations.
- Kalshi REST client and WebSocket consumer (ticker channel only) with RSA-PSS request signing.
- Snapshot-based anomaly engine and materializer that writes to the `anomalies` table on each new snapshot.
- Bootstrap / poll / materialize / WS-consumer scripts under `scripts/`.

---

## Running locally

Prerequisites: Python 3.11+, a Postgres instance, and Kalshi API credentials (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`).

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; use .venv/bin/pip on *nix

# Configure .env (postgres_*, redis_*, kalshi_* — see app/core/config.py)
.venv/Scripts/python -m alembic upgrade head

# One-shot universe bootstrap (paginated cursor sweep over open markets)
PYTHONPATH=. .venv/Scripts/python -m scripts.bootstrap_markets --status open

# Serve the API
.venv/Scripts/python -m uvicorn app.main:app --reload

# Run the WS consumer (ticker + trade + orderbook_delta)
PYTHONPATH=. .venv/Scripts/python scripts/run_ws_ticker_consumer.py

# Periodic REST poll
PYTHONPATH=. .venv/Scripts/python scripts/poll_markets.py

# Drain the lazy-upsert backlog (markets seen on the WS feed but never
# returned by the bulk `status=open` sweep — typically active/finalized
# games and BTC 15-min strikes that the dashboard surfaces).
PYTHONPATH=. .venv/Scripts/python -m scripts.hydrate_unknown_markets --top-by-trades

# Backfill the layered classifier on every market not yet at the current
# CLASSIFIER_VERSION. Run after migrating the schema or bumping the
# version in app/services/classifier/types.py.
PYTHONPATH=. .venv/Scripts/python scripts/classify_markets.py --top-by-trades

# Drop out-of-scope markets and their child rows (snapshots, trades,
# book_events, anomalies). Default is dry-run; --execute commits.
# Policy lives in app/services/retention.py.
PYTHONPATH=. .venv/Scripts/python scripts/prune_markets.py            # dry-run
PYTHONPATH=. .venv/Scripts/python scripts/prune_markets.py --execute  # actually delete

# Then open http://127.0.0.1:8000/ for the live dashboard
# (or http://127.0.0.1:8000/docs for interactive Swagger)

# Materialize anomalies over recent snapshots
PYTHONPATH=. .venv/Scripts/python scripts/materialize_anomalies.py
```

### Tests

```bash
# Unit tests (no DB required)
.venv/Scripts/python -m pytest tests/

# Integration tests against a real Postgres
docker compose up -d postgres
docker compose exec -T postgres psql -U postgres \
    -c "CREATE DATABASE surveillance_test"
RUN_INTEGRATION=1 .venv/Scripts/python -m pytest tests/test_migrations_integration.py -v

# Static checks
.venv/Scripts/python -m ruff check app/ tests/
.venv/Scripts/python -m mypy app/
```
