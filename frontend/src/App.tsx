import { useQuery } from "@tanstack/react-query";
import { Activity } from "lucide-react";
import { Link, NavLink, Route, Routes } from "react-router-dom";

import EventGroupPage from "./routes/EventGroup";
import MarketDetailPage from "./routes/MarketDetail";
import MarketsBrowserPage from "./routes/MarketsBrowser";
import OverviewPage from "./routes/Overview";
import { GlobalSearch } from "./components/GlobalSearch";
import { api } from "./api/client";
import { cn } from "./lib/utils";

function NavTab({ to, label, end }: { to: string; label: string; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cn(
          "px-3 py-1.5 rounded-md text-sm font-medium transition-colors",
          isActive
            ? "bg-card text-foreground shadow-sm"
            : "text-muted-foreground hover:text-foreground hover:bg-card/60",
        )
      }
    >
      {label}
    </NavLink>
  );
}

export default function App() {
  const archiveStatus = useQuery({
    queryKey: ["archive-status"],
    queryFn: () => api.archiveStatus(),
    staleTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
    retry: 1,
  });
  const archiveMode = archiveStatus.data?.archive_mode ?? false;
  const cutoff = archiveStatus.data?.data_cutoff_at;

  return (
    <div className="min-h-screen flex flex-col">
      <div className="sticky top-0 z-20">
        {archiveMode ? (
          <div className="border-b border-amber-500/30 bg-amber-500/15 px-4 py-2 text-center text-xs text-amber-100">
            <span className="font-semibold">Maintenance archive:</span>{" "}
            data collection is paused. Charts, trades, news, and findings reflect
            data collected through {formatArchiveCutoff(cutoff)}.
          </div>
        ) : null}
        <header className="border-b border-border bg-background/85 backdrop-blur">
          <div className="container flex items-center justify-between gap-6 py-3">
            <Link
              to="/"
              className="flex items-center gap-3 min-w-0 rounded-md -m-1 p-1 hover:bg-card/50 transition-colors"
            >
              <div className="grid place-items-center h-8 w-8 rounded-md bg-primary/15 text-primary flex-shrink-0">
                <Activity className="h-4 w-4" />
              </div>
              <div className="leading-tight min-w-0">
                <div className="text-sm font-semibold tracking-tight">
                  Prediction Market Surveillance
                </div>
                <div className="text-[11px] text-muted-foreground">
                  {archiveMode
                    ? "Historical archive of public Kalshi market surveillance"
                    : "Monitoring public Kalshi data for unusual market activity"}
                </div>
              </div>
            </Link>
            <div className="flex items-center gap-3">
              <GlobalSearch />
              <nav className="flex items-center gap-1 rounded-lg bg-secondary/50 p-1">
                <NavTab to="/" label="Overview" end />
                <NavTab to="/markets" label="Markets" />
              </nav>
            </div>
          </div>
        </header>
      </div>
      <main className="container flex-1 py-6 animate-fade-in">
        <Routes>
          <Route path="/" element={<OverviewPage archiveMode={archiveMode} />} />
          <Route path="/markets" element={<MarketsBrowserPage />} />
          <Route path="/events/:eventId" element={<EventGroupPage />} />
          <Route path="/markets/:marketId" element={<MarketDetailPage />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
      <footer className="border-t border-border py-3">
        <div className="container text-[11px] text-muted-foreground">
          <a href="/docs" className="hover:text-foreground">
            JSON API
          </a>
          {" · "}
          <a
            href="https://github.com/hong-david/predictionmarkets"
            className="hover:text-foreground"
          >
            github
          </a>
        </div>
      </footer>
    </div>
  );
}

function formatArchiveCutoff(value: string | null | undefined): string {
  if (!value) return "the last successful ingestion";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "long",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(parsed) + " UTC";
}

function NotFound() {
  return (
    <div className="grid place-items-center h-[40vh]">
      <div className="text-center">
        <div className="text-2xl font-semibold">404</div>
        <div className="text-sm text-muted-foreground">No route here.</div>
      </div>
    </div>
  );
}
