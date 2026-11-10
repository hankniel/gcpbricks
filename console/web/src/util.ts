import { useCallback, useEffect, useRef, useState } from "react";

/** Loads `fn` and reloads it every `everyMs` while the page is open (0: once). New `deps` (another
 *  run, say) clear what the old ones loaded, and an answer to an old request is dropped. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[], everyMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const load = useCallback(() => {
    const mine = generation.current;
    fn().then((d) => { if (mine === generation.current) { setData(d); setError(null); } },
              (e: Error) => { if (mine === generation.current) setError(e.message); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => { generation.current += 1; setData(null); setError(null); }, [load]);
  useEffect(() => {
    load();
    if (!everyMs) return;
    const t = setInterval(load, everyMs);
    return () => clearInterval(t);
  }, [load, everyMs]);
  return { data, error, reload: load };
}

/** A state that is still moving: its page polls fast and its clocks tick. */
export const live = (s?: string | null) => s === "RUNNING" || s === "ACTIVE" || s === "QUEUED" || s === "STARTED";

/** How often a page polls: every few seconds while something on it is live; each refresh of a
 *  running run costs Cloud Logging reads, which allow 60 a minute per project. */
export const pollMs = (anyLive: boolean) => (anyLive ? 5000 : 30000);

/** Re-renders every second while `on`, so durations of what is running count up. */
export function useTick(on: boolean) {
  const [, setNow] = useState(0);
  useEffect(() => {
    if (!on) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [on]);
}

/** Every time the console shows is the team's: Vietnam (GMT+7), whatever the browser's zone. */
export const TIME_ZONE = "Asia/Ho_Chi_Minh";
export const TIME_ZONE_LABEL = "GMT+7";

/** Databricks' format: "May 04, 2026, 01:56 PM" (with the year when `full`), in GMT+7. */
export function when(iso?: string | null, full = false): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString("en-US", { timeZone: TIME_ZONE, month: "short", day: "2-digit",
                                                 ...(full ? { year: "numeric" } : {}), hour: "2-digit", minute: "2-digit" });
}

/** A log line's clock time, "11:20:05 PM", in GMT+7. */
export function clock(iso?: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString("en-US", { timeZone: TIME_ZONE, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** A duration in seconds as Databricks writes one: "45s", "2m 5s", "1h 3m". */
export function secondsText(s: number): string {
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/** Round axis ticks from 0 up to at least `max` seconds: 0, 30s, 1m... or 0, 5m, 10m... */
export function durationTicks(max: number): number[] {
  const steps = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
  const step = steps.find((s) => max / s <= 4) ?? 86400;
  const top = Math.max(step, Math.ceil(max / step) * step);
  return Array.from({ length: top / step + 1 }, (_, i) => i * step);
}

/** A tick's label: whole units only ("30s", "2m", "1h 30m"). */
export function tickText(s: number): string {
  if (s === 0) return "0";
  if (s < 60) return `${s}s`;
  if (s < 3600) return s % 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s / 60}m`;
  return s % 3600 ? `${Math.floor(s / 3600)}h ${(s % 3600) / 60}m` : `${s / 3600}h`;
}

export function duration(start?: string | null, end?: string | null): string {
  if (!start) return "";
  return secondsText(Math.max(0, ((end ? Date.parse(end) : Date.now()) - Date.parse(start)) / 1000));
}

/** What a run's state means to a reader: a skipped run "succeeded" as a Workflow. */
export function runState(r: { state: string; skipped: boolean; with_failures?: boolean }): string {
  return r.skipped ? "SKIPPED" : r.with_failures ? "SUCCEEDED_WITH_FAILURES" : r.state;
}

export const label: Record<string, string> = {
  SUCCEEDED: "Succeeded", SUCCEEDED_WITH_FAILURES: "Succeeded with failures", SUCCESS: "Succeeded", FAILED: "Failed", ACTIVE: "Running",
  RUNNING: "Running", SKIPPED: "Skipped", CANCELLED: "Cancelled", EXCLUDED: "Not run",
  UPSTREAM_FAILED: "Upstream failed", REUSED: "Reused from earlier run", COMPLETE: "Finished", QUEUED: "Queued",
};

/** What Databricks calls each task type. */
export const KIND: Record<string, string> = {
  python_wheel_task: "Python wheel", spark_python_task: "Python script", dbt_task: "dbt",
  condition_task: "If/else condition", run_job_task: "Run Job", for_each_task: "For each",
  vertex_pipeline_task: "Vertex AI pipeline", batch_serve_task: "Batch serving", notebook_task: "Notebook",
};

/** "5 minutes ago", as Databricks' Last modified columns read. */
export function ago(iso?: string | null): string {
  if (!iso) return "";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  const units: [number, string][] = [[31536000, "year"], [2592000, "month"], [86400, "day"], [3600, "hour"], [60, "minute"]];
  for (const [n, u] of units) if (s >= n) { const k = Math.floor(s / n); return `${k} ${u}${k > 1 ? "s" : ""} ago`; }
  return "just now";
}

export function fmtValue(v: unknown): string {
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toPrecision(6).replace(/\.?0+$/, "");
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** A container's peak memory against its limit, as the task run page shows it. */
export function memoryText(m?: { limit: string | null; peak_bytes: number | null; peak_fraction: number | null }): string {
  if (!m) return "-";
  if (m.peak_bytes == null && m.peak_fraction == null) return m.limit ? `No samples (under a minute) · limit ${m.limit}` : "-";
  const used = m.peak_bytes == null ? "" : m.peak_bytes >= 2 ** 30 ? `${(m.peak_bytes / 2 ** 30).toFixed(1)} GiB` : `${Math.round(m.peak_bytes / 2 ** 20)} MiB`;
  const pct = m.peak_fraction == null ? "" : ` (${Math.round(m.peak_fraction * 100)}%)`;
  return `${used || "Peak"} of ${m.limit ?? "?"}${pct}`;
}
