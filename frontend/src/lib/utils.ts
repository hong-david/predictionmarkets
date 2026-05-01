import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** Concatenate class names, with Tailwind conflict resolution. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** 12,345 -> "12,345" with grouping separators. */
export function fmtInt(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString();
}

/** Decimal with N digits, "—" for null. Used for prices (3 dp default). */
export function fmtPrice(n: number | null | undefined, digits = 3): string {
  if (n == null) return "—";
  return Number(n).toFixed(digits);
}

/** Approximate dollars paid, rounded for compact table display. */
export function fmtDollars(n: number | null | undefined): string {
  if (n == null) return "—";
  return `$${Math.round(n).toLocaleString()}`;
}

/** ISO timestamp -> "5m ago" / "2d ago". Null-safe. */
export function fmtAgo(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const ms = now - new Date(iso).getTime();
  if (Number.isNaN(ms)) return "—";
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

/** ISO -> local wall time (uses the browser’s timezone). */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Same instant in UTC (for comparing to stored `timestamptz` and to Kalshi API docs). */
export function fmtTimeUtc(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "UTC",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  }).format(d);
}

/** US Eastern, for lining up with Kalshi’s public charts (ET). */
export function fmtTimeEastern(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  }).format(d);
}

/** Local, Eastern, and UTC strings for the same print (trade tape audit). */
export function tradeTimestampsForAudit(iso: string | null | undefined): {
  local: string;
  eastern: string;
  utc: string;
} {
  if (!iso) return { local: "—", eastern: "—", utc: "—" };
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return { local: "—", eastern: "—", utc: "—" };
  return {
    local: fmtTime(iso),
    eastern: fmtTimeEastern(iso),
    utc: fmtTimeUtc(iso),
  };
}
