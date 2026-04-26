"""Server-side dashboard.

This is intentionally a single self-contained HTML page served by FastAPI:
no Jinja templates, no Node build step, no JS framework. The page polls the
existing JSON endpoints (`/stats`, `/anomalies/stored`, the dashboard's own
`/dashboard/top-markets`) on a 5-second interval and re-renders.

The goal here is *demoability*, not a production dashboard. It exists so the
project can be opened in a browser and visually communicate what the
ingestion pipeline is actually doing — top traded markets, recent anomalies
with severity colour-coding, raw event-table counts — rather than asking a
viewer to read raw JSON. A real dashboard would be a separate frontend with
auth, websockets, and charting, none of which are interesting to build until
the surveillance engine itself is more than a static-threshold scorer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.db.models import Anomaly, BookEvent, Market, MarketSnapshot, Trade

router = APIRouter(tags=["dashboard"])


@router.get("/stats")
def get_stats(db: Session = Depends(get_db)) -> dict:
    """Coarse counts across the four ingest tables.

    Used by the dashboard top-of-page. `count(*)` over the whole table is
    fine here because (a) these tables are small at v1 scale and (b) the
    dashboard polls every 5s, not every request.
    """
    market_count = db.query(func.count(Market.id)).scalar() or 0
    unknown_market_count = (
        db.query(func.count(Market.id)).filter(Market.status == "unknown").scalar() or 0
    )
    snapshot_count = db.query(func.count(MarketSnapshot.id)).scalar() or 0
    trade_count = db.query(func.count(Trade.id)).scalar() or 0
    book_event_count = db.query(func.count(BookEvent.id)).scalar() or 0
    anomaly_count = db.query(func.count(Anomaly.id)).scalar() or 0
    high_severity_count = (
        db.query(func.count(Anomaly.id)).filter(Anomaly.severity == "high").scalar() or 0
    )

    return {
        "markets": market_count,
        "markets_status_unknown": unknown_market_count,
        "snapshots": snapshot_count,
        "trades": trade_count,
        "book_events": book_event_count,
        "anomalies": anomaly_count,
        "anomalies_high_severity": high_severity_count,
    }


@router.get("/dashboard/top-markets")
def get_top_markets(
    limit: int = Query(default=15, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict:
    """Top markets by trade count, joined with their latest-snapshot mid/spread.

    This is the kind of "what's actually happening" view that the existing
    `/markets` endpoint can't answer because it just paginates by `id desc`.
    Surfacing it as a dedicated dashboard endpoint keeps the public API
    schema clean while still letting the dashboard render meaningfully.
    """
    rows = (
        db.query(
            Market.market_id,
            Market.title,
            Market.subtitle,
            Market.status,
            func.count(Trade.id).label("trade_count"),
        )
        .join(Trade, Trade.market_pk == Market.id)
        .group_by(Market.id)
        .order_by(func.count(Trade.id).desc())
        .limit(limit)
        .all()
    )

    # `title` on Kalshi is the *event description* ("Game 4: New York at
    # Atlanta Winner?"); the disambiguating leg ("Atlanta" vs "New York",
    # or "Over 214.5 points scored", or "$77,600 or above" for a BTC
    # strike) lives in `subtitle`. Two markets under the same event share
    # a title, so we *must* return both fields or the dashboard renders
    # apparent dupes.
    return {
        "count": len(rows),
        "markets": [
            {
                "market_id": r.market_id,
                "title": r.title,
                "subtitle": r.subtitle,
                "status": r.status,
                "trade_count": int(r.trade_count),
            }
            for r in rows
        ],
    }


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Prediction Market Surveillance</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <style>
    :root {
      --bg: #0b0d10;
      --panel: #14181d;
      --panel-2: #1b2027;
      --text: #e6e8eb;
      --muted: #8a93a0;
      --accent: #4ea1ff;
      --good: #2ecc71;
      --warn: #f1c40f;
      --bad: #e74c3c;
      --border: #232932;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 20px 24px 0 24px;
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 16px;
      flex-wrap: wrap;
    }
    header h1 { margin: 0; font-size: 20px; font-weight: 600; }
    header .sub { color: var(--muted); font-size: 13px; }
    header .right { color: var(--muted); font-size: 12px; }
    main { padding: 16px 24px 32px 24px; }
    .row {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }
    .stat {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 14px 16px;
    }
    .stat .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
    .stat .value { font-size: 24px; font-weight: 600; margin-top: 4px; }
    .stat .sub { color: var(--muted); font-size: 11px; margin-top: 2px; }
    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
    }
    .panel h2 {
      margin: 0;
      padding: 12px 16px;
      font-size: 13px;
      font-weight: 600;
      letter-spacing: .03em;
      text-transform: uppercase;
      color: var(--muted);
      background: var(--panel-2);
      border-bottom: 1px solid var(--border);
    }
    table { width: 100%; border-collapse: collapse; }
    th, td {
      padding: 9px 12px;
      text-align: left;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    th { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; font-weight: 500; }
    td { font-size: 13px; }
    td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
    tr:last-child td { border-bottom: 0; }
    .title { font-size: 13px; line-height: 1.35; margin-bottom: 3px; }
    .title.pending { color: var(--muted); font-style: italic; }
    .subtitle { font-size: 12px; color: var(--accent); margin-bottom: 3px; }
    .ticker { font-family: "SF Mono", Consolas, monospace; font-size: 11px; color: var(--muted); }
    .ticker:hover { color: var(--accent); }
    .badge {
      display: inline-block;
      font-size: 10px;
      letter-spacing: .04em;
      text-transform: uppercase;
      padding: 2px 7px;
      border-radius: 3px;
      font-weight: 600;
    }
    .badge.high   { background: rgba(231, 76, 60, .18); color: #ff7a6c; }
    .badge.medium { background: rgba(241, 196, 15, .15); color: #ffd34a; }
    .badge.low    { background: rgba(46, 204, 113, .15); color: #4be08a; }
    .badge.status { background: rgba(78, 161, 255, .15); color: var(--accent); font-weight: 500; }
    .reasons { color: var(--muted); font-size: 12px; }
    .reasons code { color: var(--text); background: var(--panel-2); padding: 1px 5px; border-radius: 3px; font-size: 11px; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .empty { padding: 16px; color: var(--muted); font-style: italic; }
    .pulse { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--good); margin-right: 6px; }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Prediction Market Surveillance</h1>
      <div class="sub">Live ingest from Kalshi · per-snapshot rule-scored anomalies</div>
    </div>
    <div class="right"><span class="pulse"></span><span id="last-refresh">connecting…</span></div>
  </header>
  <main>
    <section class="row" id="stats"></section>
    <section class="grid">
      <div class="panel">
        <h2>Top markets by trade count</h2>
        <div id="top-markets"></div>
      </div>
      <div class="panel">
        <h2>Persisted anomalies (highest score)</h2>
        <div id="anomalies"></div>
      </div>
    </section>
  </main>
  <script>
    const fmtInt = (n) => (n ?? 0).toLocaleString();
    const fmtFloat = (n, d = 3) => (n == null ? "—" : Number(n).toFixed(d));
    const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

    async function getJSON(path) {
      const r = await fetch(path, { cache: "no-store" });
      if (!r.ok) throw new Error(`${path} -> ${r.status}`);
      return r.json();
    }

    function renderStats(s) {
      const el = document.getElementById("stats");
      el.innerHTML = `
        <div class="stat">
          <div class="label">Markets</div>
          <div class="value">${fmtInt(s.markets)}</div>
          <div class="sub">${fmtInt(s.markets_status_unknown)} auto-discovered (status=unknown)</div>
        </div>
        <div class="stat">
          <div class="label">Snapshots</div>
          <div class="value">${fmtInt(s.snapshots)}</div>
          <div class="sub">REST poll + WS ticker</div>
        </div>
        <div class="stat">
          <div class="label">Trades</div>
          <div class="value">${fmtInt(s.trades)}</div>
          <div class="sub">WS trade channel</div>
        </div>
        <div class="stat">
          <div class="label">Book events</div>
          <div class="value">${fmtInt(s.book_events)}</div>
          <div class="sub">orderbook_snapshot + delta</div>
        </div>
        <div class="stat">
          <div class="label">Anomalies</div>
          <div class="value">${fmtInt(s.anomalies)}</div>
          <div class="sub">${fmtInt(s.anomalies_high_severity)} high-severity</div>
        </div>
      `;
    }

    function renderMarketCell(m, href) {
      // Lazy-upserted markets carry title === market_id as a sentinel for
      // "metadata not yet hydrated by the REST poller". Show that explicitly
      // rather than duplicating the ticker on two lines.
      const hasTitle = m.title && m.title !== m.market_id;
      const titleHtml = hasTitle
        ? `<div class="title">${esc(m.title)}</div>`
        : `<div class="title pending">metadata pending</div>`;
      // `subtitle` is the leg-level differentiator (e.g. "Atlanta" vs
      // "New York" for a game-winner market, "$77,600 or above" for a BTC
      // strike). Without rendering it, every leg of the same event looks
      // identical in the dashboard.
      const subtitleHtml = m.subtitle
        ? `<div class="subtitle">${esc(m.subtitle)}</div>`
        : "";
      return `${titleHtml}${subtitleHtml}<a class="ticker" href="${href}">${esc(m.market_id)}</a>`;
    }

    function renderTopMarkets(payload) {
      const el = document.getElementById("top-markets");
      const rows = payload.markets || [];
      if (!rows.length) {
        el.innerHTML = '<div class="empty">No trades ingested yet. Start the WS consumer.</div>';
        return;
      }
      const body = rows.map((m) => {
        const href = `/markets/${encodeURIComponent(m.market_id)}`;
        return `
        <tr>
          <td>${renderMarketCell(m, href)}</td>
          <td><span class="badge status">${esc(m.status || "?")}</span></td>
          <td class="num">${fmtInt(m.trade_count)}</td>
        </tr>
        `;
      }).join("");
      el.innerHTML = `
        <table>
          <thead><tr><th>Market</th><th>Status</th><th class="num">Trades</th></tr></thead>
          <tbody>${body}</tbody>
        </table>
      `;
    }

    function renderAnomalies(payload) {
      const el = document.getElementById("anomalies");
      const rows = payload.anomalies || [];
      if (!rows.length) {
        el.innerHTML = '<div class="empty">No anomalies stored yet.</div>';
        return;
      }
      const body = rows.map((a) => {
        const href = `/markets/${encodeURIComponent(a.market_id)}/anomaly`;
        return `
        <tr>
          <td><span class="badge ${esc(a.severity)}">${esc(a.severity)}</span></td>
          <td class="num">${fmtFloat(a.score, 1)}</td>
          <td>
            ${renderMarketCell({ market_id: a.market_id, title: a.title, subtitle: a.subtitle }, href)}
            <div class="reasons" style="margin-top:6px">${(a.reasons || []).map((r) => `<code>${esc(r)}</code>`).join(" ")}</div>
          </td>
        </tr>
        `;
      }).join("");
      el.innerHTML = `
        <table>
          <thead><tr><th>Severity</th><th class="num">Score</th><th>Market / signals</th></tr></thead>
          <tbody>${body}</tbody>
        </table>
      `;
    }

    async function refresh() {
      try {
        const [stats, top, anomalies] = await Promise.all([
          getJSON("/stats"),
          getJSON("/dashboard/top-markets?limit=15"),
          getJSON("/anomalies/stored?limit=15"),
        ]);
        renderStats(stats);
        renderTopMarkets(top);
        renderAnomalies(anomalies);
        document.getElementById("last-refresh").textContent =
          `updated ${new Date().toLocaleTimeString()}`;
      } catch (e) {
        document.getElementById("last-refresh").textContent = `error: ${e.message}`;
      }
    }

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
def dashboard_index() -> HTMLResponse:
    return HTMLResponse(content=_DASHBOARD_HTML)
