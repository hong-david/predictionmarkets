import { useEffect } from "react";
import { useLocation } from "react-router-dom";

declare global {
  interface Window {
    goatcounter?: {
      count?: (vars?: Record<string, unknown>) => void;
    };
  }
}

function countPageView(path: string, title: string) {
  // count.js loads async, so the first route can fire before GoatCounter is ready.
  // Retry briefly so the initial pageview is not missed.
  let attempts = 0;

  const send = () => {
    attempts += 1;

    if (window.goatcounter?.count) {
      window.goatcounter.count({
        path,
        title,
      });
      return;
    }

    if (attempts < 20) {
      window.setTimeout(send, 250);
    }
  };

  send();
}

export function AnalyticsTracker() {
  const location = useLocation();

  useEffect(() => {
    countPageView(
      location.pathname + location.search,
      document.title || location.pathname
    );
  }, [location.pathname, location.search]);

  return null;
}