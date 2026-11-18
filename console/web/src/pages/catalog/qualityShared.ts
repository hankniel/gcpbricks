import { AnomalyRow, DriftRow, ProfileRow, QualityMetrics, QualityMonitor } from "../../api";
import { number } from "../../components/Chart";

export const n = (v: unknown): number | null => (v == null || v === "" ? null : Number(v));
export const pct = (v: unknown) => (n(v) == null ? "-" : `${number(n(v))}%`);
export const day = (iso: string | null) => (iso ? iso.slice(0, 16).replace("T", " ").replace(/ 00:00$/, "") : "baseline");
export const TABLE = ":table";
/** A select's "all models" / "whole table": Radix's Select can't hold "" as a value. */
export const ALL = "\u0001all";
/** Databricks' guidance: PSI 0.1-0.2 moderate, 0.2+ significant; a test's p-value under 0.05. */
export const PSI_DRIFT = 0.2;
export const P_DRIFT = 0.05;

/** [column, label, lower is better]. */
export const REGRESSION: [string, string, boolean][] = [
  ["mean_absolute_percentage_error", "MAPE", true], ["mean_average_error", "MAE", true],
  ["root_mean_squared_error", "RMSE", true], ["mean_squared_error", "MSE", true], ["r2_score", "R²", false]];
export const CLASSIFICATION: [string, string, boolean][] = [
  ["accuracy_score", "Accuracy", false], ["f1_macro", "F1 (macro)", false], ["precision_macro", "Precision (macro)", false],
  ["recall_macro", "Recall (macro)", false]];
export const PROBABILITY: [string, string, boolean][] = [
  ["roc_auc_weighted", "ROC AUC (weighted)", false], ["roc_auc_macro", "ROC AUC (macro)", false], ["log_loss", "Log loss", true]];
export const FAIRNESS: [keyof ProfileRow, string, string][] = [
  ["predictive_parity", "Predictive parity", "precision"], ["predictive_equality", "Predictive equality", "false positive rate"],
  ["equal_opportunity", "Equal opportunity", "recall"], ["statistical_parity", "Statistical parity", "acceptance rate"]];

export function qualityMetrics(m: QualityMonitor): [string, string, boolean][] {
  if (m.profile_type !== "InferenceLog") return [];
  return m.problem_type === "PROBLEM_TYPE_REGRESSION" ? REGRESSION
    : [...CLASSIFICATION, ...(m.prediction_proba_col ? PROBABILITY : [])];
}

/** The column's drift score: PSI for a number, Jensen-Shannon distance for a category. */
export const score = (d: DriftRow) => n(d.population_stability_index) ?? n(d.js_distance);
export const pvalue = (d: DriftRow) => n(d.ks_pvalue) ?? n(d.chi_squared_pvalue);
export const drifted = (d: DriftRow) => (n(d.population_stability_index) ?? 0) >= PSI_DRIFT || (pvalue(d) ?? 1) < P_DRIFT;
export const flagged = (a: AnomalyRow) => a.is_anomaly === true || a.is_anomaly === "true";
export const STATUS_COLOR = { Unhealthy: "coral", Training: "default", Error: "lemon", Healthy: "teal" } as const;

/** The pieces of one read every view starts from: the INPUT and BASELINE profile rows and the windows, oldest first. */
export function split(data: QualityMetrics) {
  const input = data.profile.filter((r) => r.log_type === "INPUT");
  const baseline = data.profile.filter((r) => r.log_type === "BASELINE");
  const windows = [...new Set(input.map((r) => r.start ?? ""))].sort();
  return { input, baseline, windows, latest: windows[windows.length - 1] };
}

/** Each series' latest judged point (column · metric), flagged ones first. */
export function latestAnomalies(rows: AnomalyRow[]): AnomalyRow[] {
  const key = (a: AnomalyRow) => `${a.column_name} · ${a.metric}`;
  const by = new Map<string, AnomalyRow>();
  for (const a of rows) if (!by.has(key(a)) || by.get(key(a))!.start < a.start) by.set(key(a), a);
  return [...by.values()].sort((a, b) => Number(flagged(b)) - Number(flagged(a)) || key(a).localeCompare(key(b)));
}

/** The latest window's drift per column against `view`, drifted and highest-scoring first. */
export function latestDrift(drift: DriftRow[], latest: string, view: "BASELINE" | "CONSECUTIVE"): DriftRow[] {
  return drift.filter((d) => d.drift_type === view && d.column_name !== TABLE && d.start === latest)
    .sort((a, b) => Number(drifted(b)) - Number(drifted(a)) || (score(b) ?? -1) - (score(a) ?? -1));
}

/** "+3.2 vs baseline" style text for a metric's change: direction in words, never color alone. */
export function delta(now: number | null, then: number | null, against: string): string {
  if (now == null || then == null) return `No ${against} to compare with`;
  const d = now - then;
  if (d === 0) return `No change vs ${against}`;
  return `${d > 0 ? "+" : "−"}${number(Math.abs(d))} vs ${against}`;
}

/** Whether a change is a worsening, for a metric where lower (or higher) is better. */
export function worse(now: number | null, then: number | null, lowerIsBetter: boolean): boolean | null {
  if (now == null || then == null || now === then) return null;
  return lowerIsBetter ? now > then : now < then;
}

export const monitorLink = (m: { bundle: string; target: string; key: string }) =>
  `/quality/${m.bundle}/${m.target}/${m.key}`;
