import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Component, type ErrorInfo, type ReactNode, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import "./index.css";

class RootErrorBoundary extends Component<
  { children: ReactNode },
  { err: Error | null }
> {
  state: { err: Error | null } = { err: null };

  static getDerivedStateFromError(err: Error) {
    return { err };
  }

  componentDidCatch(err: Error, info: ErrorInfo) {
    console.error("React error:", err, info.componentStack);
  }

  render() {
    if (this.state.err) {
      return (
        <div
          style={{
            minHeight: "100vh",
            boxSizing: "border-box",
            margin: 0,
            padding: 24,
            background: "hsl(222, 18%, 6%)",
            color: "hsl(210, 14%, 92%)",
            fontFamily: "system-ui, sans-serif",
          }}
        >
          <h1 style={{ fontSize: 18, fontWeight: 600, margin: "0 0 8px" }}>Something went wrong</h1>
          <pre
            style={{
              fontSize: 12,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              color: "hsl(218, 10%, 60%)",
              maxWidth: 768,
            }}
          >
            {this.state.err.message}
          </pre>
          <p style={{ fontSize: 12, color: "hsl(218, 10%, 50%)", marginTop: 16, maxWidth: 560 }}>
            Open the browser devtools (F12) for the full error and stack. After fixing the
            code, hard-refresh the page.
          </p>
        </div>
      );
    }
    return this.props.children;
  }
}

// One QueryClient for the whole app. Defaults are tuned for a
// surveillance dashboard: stale-while-revalidate so navigating back to
// a page shows last-known data instantly, then refetches in background.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      gcTime: 5 * 60_000,
      refetchOnWindowFocus: true,
      retry: 1,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <RootErrorBoundary>
            <App />
          </RootErrorBoundary>
        </BrowserRouter>
      </QueryClientProvider>
  </StrictMode>,
);
