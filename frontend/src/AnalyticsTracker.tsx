import { useEffect } from "react";
import { useLocation } from "react-router-dom";

declare global {
  interface Window {
    goatcounter?: {
      count?: (vars?: Record<string, unknown>) => void;
      filter?: () => string | false;
    };
  }
}

function bucketPath(pathname: string): string {
  if (pathname === "/") return "/";
  if (pathname === "/markets") return "/markets";

  // Collapse all individual market detail pages into one bucket.
  if (pathname.startsWith("/markets/")) return "/markets/:market_id";

  // Keep admin visits separate if you ever want to see that bucket.
  if (pathname.startsWith("/admin/visits")) return "/admin/visits";

  // Optional: keep common pages separate if you add them later.
  if (pathname === "/about") return "/about";
  if (pathname === "/resume") return "/resume";
  if (pathname === "/projects") return "/projects";

  // Avoid creating a million one-off rows.
  return "/other";
}

function goatCount(vars: Record<string, unknown>) {
  let attempts = 0;

  const send = () => {
    attempts += 1;

    if (window.goatcounter?.count) {
      const filtered = window.goatcounter.filter?.();
      if (filtered) return;

      window.goatcounter.count(vars);
      return;
    }

    // count.js loads async, so retry briefly.
    if (attempts < 20) {
      window.setTimeout(send, 250);
    }
  };

  send();
}

export function trackGoatEvent(eventName: string, title?: string) {
  goatCount({
    path: eventName,
    title: title || eventName,
    event: true,
  });
}

export function AnalyticsTracker() {
  const location = useLocation();

  useEffect(() => {
    const path = bucketPath(location.pathname);

    goatCount({
      path,
      title: document.title || path,
    });
  }, [location.pathname]);

  return null;
}