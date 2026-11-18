import { useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import {
  Alert, Button, DashboardIcon, Empty, HistoryIcon, MetricCard, RefreshIcon, SegmentedControlButton, SegmentedControlGroup,
  SimpleSelect, SimpleSelectOption, Spacer, SparklineChart, TableCell, TableRow, TableSkeleton, Tabs, Tag, Tooltip, Typography,
  useDesignSystemTheme,
} from "@databricks/design-system";
import { api, AnomalyRow, ColumnDist, DriftRow, ProfileRow, QualityMetrics, QualityMonitor, QualityPick, SliceRow } from "../api";
import Chart, { number, Series } from "../components/Chart";
import PageTitle from "../components/PageTitle";
import RefreshHistoryModal from "../components/RefreshHistoryModal";
import { Table, TableHeader } from "../components/Table";
import { useLoad } from "../util";
import {
  ALL, day, delta, drifted, FAIRNESS, flagged, latestAnomalies, latestDrift, n, P_DRIFT, pct, pvalue, PSI_DRIFT,
  qualityMetrics, score, split, STATUS_COLOR, TABLE, worse,
} from "./catalog/qualityShared";

type View = "BASELINE" | "CONSECUTIVE";
type Section = "overview" | "performance" | "drift" | "integrity" | "fairness" | "anomalies";
const RANGES = [14, 30, 90];
const SELECTED = "rgba(7, 122, 157, 0.08)";

/** A monitor's dashboard: the deep dive behind its Quality tab. Laid out as ML monitoring tools
 *  read a model (Evidently's reports, Arize's performance tracing, NannyML's summary): health over
 *  time first, then performance with its breakdown by slice, then drift down to one column's
 *  distribution, then data integrity. Every pick (granularity, range, model, slice, comparison,
 *  section, column) is in the URL, so a view can be shared and the Quality tab links into it. */
export default function QualityDashboardPage() {
  const { bundle = "", target = "", key = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) if (v) next.set(k, v); else next.delete(k);
    setParams(next, { replace: true });
  };
  const pick: QualityPick = { granularity: params.get("granularity") ?? "", model: params.get("model") ?? "",
                              slice_key: params.get("slice_key") ?? "", slice_value: params.get("slice_value") ?? "" };
  const range = Number(params.get("windows") ?? 30);
  const [tick, setTick] = useState(0);
  const [history, setHistory] = useState(false);
  const ref = { bundle, target, key };
  const { data, error } = useLoad(() => api.quality(ref, pick).then((d) => ({ ...d, range })),
    [bundle, target, key, pick.granularity, pick.model, pick.slice_key, pick.slice_value, range, tick]);
  const m = data?.monitor;
  const [, ds, table] = (m?.table_name ?? "..").split(".");
  const project = m?.table_name.split(".")[0];
  return (
    <div>
      <PageTitle crumbs={m ? [{ to: "/catalog", label: "Catalog" }, { to: `/catalog/${ds}`, label: ds },
                             { to: `/catalog/${ds}/${table}?project=${encodeURIComponent(project ?? "")}`, label: table }] : []}
                 icon={<DashboardIcon />} title={m ? `${table} monitor` : key}
                 addOns={m && <>
                   <Tag componentId="qdash.type">{m.profile_type === "InferenceLog" ? "Inference" : m.profile_type === "TimeSeries" ? "Time series" : "Snapshot"}</Tag>
                   {m.problem_type && <Tag componentId="qdash.problem">{m.problem_type === "PROBLEM_TYPE_REGRESSION" ? "Regression" : "Classification"}</Tag>}
                 </>}
                 actions={m && <>
                   <Button componentId="qdash.history" icon={<HistoryIcon />} onClick={() => setHistory(true)}>View refresh history</Button>
                   <Tooltip componentId="qdash.refresh.tip" content="Reads the metric tables again. It doesn't recompute them: that is a refresh of the monitor.">
                     <Button componentId="qdash.refresh" icon={<RefreshIcon />} onClick={() => setTick(tick + 1)}>Refresh</Button>
                   </Tooltip>
                 </>} />
      <Spacer size="md" />
      {error && <Alert componentId="qdash.error" type="error" message="Couldn't read the metric tables" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={10} />}
      {data && m && <>
        <Filters m={m} data={data} pick={pick} range={range} set={set} />
        <Spacer size="md" />
        <Body m={m} data={data} pick={pick} range={range} section={(params.get("section") as Section) ?? "overview"}
              column={params.get("column")} view={params.get("compare") as View | null} set={set} />
        <RefreshHistoryModal m={m} open={history} onClose={() => setHistory(false)} />
      </>}
    </div>
  );
}

/** The bar every section reads through: granularity, range, model, slice and what drift compares with. */
function Filters({ m, data, pick, range, set }: {
  m: QualityMonitor; data: QualityMetrics; pick: QualityPick; range: number; set: (p: Record<string, string | null>) => void;
}) {
  const { theme } = useDesignSystemTheme();
  const slice = pick.slice_key ? `${pick.slice_key}\u0000${pick.slice_value}` : "";
  return (
    <div css={{ position: "sticky", top: 0, zIndex: 2, background: theme.colors.backgroundPrimary, padding: `${theme.spacing.sm}px 0`,
                borderBottom: `1px solid ${theme.colors.border}`, display: "flex", gap: theme.spacing.md, flexWrap: "wrap", alignItems: "flex-end" }}>
      {m.granularities.length > 0 && (
        <SegmentedControlGroup componentId="qdash.granularity" name="granularity" value={data.granularity}
                               onChange={(e) => set({ granularity: e.target.value })}>
          {m.granularities.map((g) => <SegmentedControlButton key={g} value={g}>{g}</SegmentedControlButton>)}
        </SegmentedControlGroup>
      )}
      {m.granularities.length > 0 && (
        <SegmentedControlGroup componentId="qdash.range" name="range" value={String(range)} onChange={(e) => set({ windows: e.target.value })}>
          {RANGES.map((r) => <SegmentedControlButton key={r} value={String(r)}>Last {r}</SegmentedControlButton>)}
        </SegmentedControlGroup>
      )}
      {m.model_id_col && (
        <div css={{ width: 200 }}>
          <SimpleSelect componentId="qdash.model" id="qdash-model" label="Model" value={pick.model || ALL}
                        onChange={(e) => set({ model: e.target.value === ALL ? null : e.target.value })}>
            <SimpleSelectOption value={ALL}>All models</SimpleSelectOption>
            {data.options.models.map((x) => <SimpleSelectOption key={x} value={x}>{x}</SimpleSelectOption>)}
          </SimpleSelect>
        </div>
      )}
      {m.slicing_exprs.length > 0 && (
        <div css={{ width: 260 }}>
          <SimpleSelect componentId="qdash.slice" id="qdash-slice" label="Slice" value={slice || ALL}
                        onChange={(e) => {
                          const [k, v = ""] = e.target.value === ALL ? [""] : e.target.value.split("\u0000");
                          set({ slice_key: k || null, slice_value: k ? v : null });
                        }}>
            <SimpleSelectOption value={ALL}>Whole table</SimpleSelectOption>
            {data.options.slices.map((s) => (
              <SimpleSelectOption key={`${s.key}=${s.value}`} value={`${s.key}\u0000${s.value}`}>{`${s.key} = ${s.value}`}</SimpleSelectOption>))}
          </SimpleSelect>
        </div>
      )}
      <Typography.Text color="secondary" size="sm" css={{ marginLeft: "auto" }}>
        Read {number(data.billed_bytes / 1024 / 1024)} MB of the metric tables
      </Typography.Text>
    </div>
  );
}

function Body({ m, data, pick, range, section, column, view, set }: {
  m: QualityMonitor; data: QualityMetrics; pick: QualityPick; range: number; section: Section; column: string | null;
  view: View | null; set: (p: Record<string, string | null>) => void;
}) {
  const { input, baseline, windows: all } = split(data);
  const windows = all.slice(-range);
  const hasBaseline = data.drift.some((d) => d.drift_type === "BASELINE");
  const compare: View = view ?? (hasBaseline ? "BASELINE" : "CONSECUTIVE");
  if (!windows.length) {
    return <Empty title="No metrics for this pick" description="The metric tables hold nothing for it: refresh the monitor, or pick another model or slice." />;
  }
  const sections: [Section, string][] = [
    ["overview", "Overview"],
    ...(m.profile_type === "InferenceLog" ? [["performance", "Model performance"] as [Section, string]] : []),
    ["drift", "Drift"], ["integrity", "Data integrity"],
    ...(m.problem_type === "PROBLEM_TYPE_CLASSIFICATION" ? [["fairness", "Fairness and bias"] as [Section, string]] : []),
    ...(m.anomalies_table ? [["anomalies", "Anomalies"] as [Section, string]] : []),
  ];
  const current = sections.some(([s]) => s === section) ? section : "overview";
  return (
    <Tabs.Root componentId="qdash.sections" value={current} onValueChange={(v) => set({ section: v === "overview" ? null : v })}>
      <Tabs.List>{sections.map(([s, l]) => <Tabs.Trigger key={s} value={s}>{l}</Tabs.Trigger>)}</Tabs.List>
      <Tabs.Content value="overview">
        <Overview m={m} data={data} input={input} baseline={baseline} windows={windows} compare={compare} set={set} />
      </Tabs.Content>
      {m.profile_type === "InferenceLog" && (
        <Tabs.Content value="performance">
          <Performance m={m} input={input} baseline={baseline} windows={windows} pick={pick} range={range} set={set} />
        </Tabs.Content>
      )}
      <Tabs.Content value="drift">
        <Drift m={m} data={data} windows={windows} compare={compare} hasBaseline={hasBaseline} column={column} pick={pick} range={range} set={set} />
      </Tabs.Content>
      <Tabs.Content value="integrity"><Integrity data={data} input={input} baseline={baseline} windows={windows} compare={compare} /></Tabs.Content>
      {m.problem_type === "PROBLEM_TYPE_CLASSIFICATION" && (
        <Tabs.Content value="fairness"><Fairness m={m} data={data} input={input} windows={windows} /></Tabs.Content>
      )}
      {m.anomalies_table && <Tabs.Content value="anomalies"><Anomalies rows={(data.anomalies ?? []).filter((a) => windows.includes(a.start))} /></Tabs.Content>}
    </Tabs.Root>
  );
}

// --- building blocks ------------------------------------------------------------------------

function Panel({ title, hint, actions, children, span = 1 }: {
  title: string; hint?: string; actions?: React.ReactNode; children: React.ReactNode; span?: 1 | 2;
}) {
  const { theme } = useDesignSystemTheme();
  return (
    <section css={{ border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd, padding: theme.spacing.md,
                    background: theme.colors.backgroundPrimary, minWidth: 0, gridColumn: span === 2 ? "1 / -1" : undefined }}>
      <div css={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: theme.spacing.sm }}>
        <div>
          <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>{title}</Typography.Title>
          {hint && <Typography.Text color="secondary" size="sm">{hint}</Typography.Text>}
        </div>
        {actions}
      </div>
      <Spacer size="sm" />
      {children}
    </section>
  );
}

/** Two columns of panels (a span-2 panel takes the row), one on a narrow screen. */
const Grid = ({ children }: { children: React.ReactNode }) => {
  const { theme } = useDesignSystemTheme();
  return <div css={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: theme.spacing.md, marginTop: theme.spacing.md,
                     "@media (max-width: 1100px)": { gridTemplateColumns: "minmax(0, 1fr)" } }}>{children}</div>;
};

/** Rows × windows, each cell shaded by its value on one scale (max at full): a heatmap. `bad`
 *  marks a cell past its threshold with an outline, so state never rests on color alone. */
function Heatmap({ rows, windows, value, format, bad, onPick, picked, perRow = false }: {
  rows: string[]; windows: string[]; value: (row: string, w: string) => number | null; format: (v: number) => string;
  bad?: (row: string, w: string, v: number) => boolean; onPick?: (row: string) => void; picked?: string | null;
  /** Each row on its own scale: for rows of different units. */
  perRow?: boolean;
}) {
  const { theme } = useDesignSystemTheme();
  const values = rows.flatMap((r) => windows.map((w) => value(r, w))).filter((v): v is number => v != null);
  const max = Math.max(1e-9, ...values.map(Math.abs));
  const rowMax = new Map(rows.map((r) => [r, Math.max(1e-9, ...windows.map((w) => Math.abs(value(r, w) ?? 0)))]));
  const every = Math.ceil(windows.length / 10);
  return (
    <div css={{ overflowX: "auto" }}>
      <div css={{ display: "grid", gridTemplateColumns: `minmax(140px, 220px) repeat(${windows.length}, minmax(18px, 1fr))`, gap: 2,
                  fontSize: 11, minWidth: 160 + 20 * windows.length }}>
        <div />
        {windows.map((w, i) => <div key={w} css={{ color: theme.colors.textSecondary, textAlign: "center", whiteSpace: "nowrap", overflow: "hidden" }}>
          {i % every === 0 ? day(w).slice(5, 10) : ""}</div>)}
        {rows.map((r) => [
          <div key={`l-${r}`} onClick={() => onPick?.(r)}
               css={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", paddingRight: 6, cursor: onPick ? "pointer" : undefined,
                      fontWeight: r === picked ? 600 : undefined }} title={r}>{r}</div>,
          ...windows.map((w) => {
            const v = value(r, w);
            return (
              <div key={`${r}-${w}`} title={`${r} · ${day(w)}: ${v == null ? "no value" : format(v)}`} onClick={() => onPick?.(r)}
                   css={{ height: 18, borderRadius: 2, cursor: onPick ? "pointer" : undefined,
                          background: v == null ? theme.colors.backgroundSecondary : `rgba(7, 122, 157, ${0.08 + 0.85 * (Math.abs(v) / (perRow ? rowMax.get(r)! : max))})`,
                          outline: v != null && bad?.(r, w, v) ? `2px solid ${theme.colors.textValidationDanger}` : undefined, outlineOffset: -2 }} />
            );
          }),
        ])}
      </div>
    </div>
  );
}

const spark = (values: number[], label: string, color = "category-1") => (
  <div css={{ height: 32, marginTop: 4 }}>
    <SparklineChart values={values} color={color as "category-1"} fillInAreaBelowSparkline aria-label={label} />
  </div>
);

// --- overview -------------------------------------------------------------------------------

/** Health over time: the headline numbers, then one row per signal across the windows. */
function Overview({ m, data, input, baseline, windows, compare, set }: {
  m: QualityMonitor; data: QualityMetrics; input: ProfileRow[]; baseline: ProfileRow[]; windows: string[]; compare: View;
  set: (p: Record<string, string | null>) => void;
}) {
  const { theme } = useDesignSystemTheme();
  const table = new Map(input.filter((r) => r.column_name === TABLE).map((r) => [r.start ?? "", r]));
  const base = baseline.find((r) => r.column_name === TABLE);
  const latest = windows[windows.length - 1];
  const prev = windows.length > 1 ? table.get(windows[windows.length - 2]) : undefined;
  const now = table.get(latest);
  const metrics = qualityMetrics(m);
  const [qKey, qName, lowerBetter] = metrics[0] ?? [];
  const qThen = qKey ? (base && n(base[qKey]) != null ? n(base[qKey]) : n(prev?.[qKey])) : null;
  const driftBy = (w: string) => data.drift.filter((d) => d.drift_type === compare && d.column_name !== TABLE && d.start === w);
  const nullMax = (w: string) => Math.max(0, ...input.filter((r) => r.start === w && r.column_name !== TABLE).map((r) => n(r.percent_null) ?? 0));
  const anomalyBy = (w: string) => (data.anomalies ?? []).filter((a) => a.start === w && flagged(a)).length;
  const signals: [string, (w: string) => number | null, (v: number) => string, ((v: number) => boolean)?][] = [
    ["Rows", (w) => n(table.get(w)?.count), (v) => number(v)],
    ...(qKey ? [[qName!, (w: string) => n(table.get(w)?.[qKey]), (v: number) => number(v)] as [string, (w: string) => number | null, (v: number) => string]] : []),
    ["Drifted columns", (w) => (driftBy(w).length ? driftBy(w).filter(drifted).length : null), (v) => `${v} column(s)`, (v) => v > 0],
    ["Highest null %", (w) => nullMax(w), (v) => `${number(v)}%`],
    ...(m.anomalies_table ? [["Anomalies", anomalyBy, (v: number) => `${v} flagged`, (v: number) => v > 0] as
      [string, (w: string) => number | null, (v: number) => string, (v: number) => boolean]] : []),
  ];
  const drift = latestDrift(data.drift, latest, compare);
  const open = latestAnomalies(data.anomalies ?? []).filter(flagged);
  return (
    <>
      <Spacer size="md" />
      <div css={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: theme.spacing.md }}>
        <MetricCard componentId="qdash.card.rows" label={`Rows, ${day(latest)}`} metric={number(n(now?.count))} fullHeight
                    comparison={delta(n(now?.count), n(prev?.count), "previous window")}>
          {spark(windows.map((w) => n(table.get(w)?.count) ?? 0), "Rows per window")}
        </MetricCard>
        {metrics.slice(0, 2).map(([k, name, lower], i) => {
          const v = n(now?.[k]), then = base && n(base[k]) != null ? n(base[k]) : n(prev?.[k]);
          const bad = worse(v, then, lower);
          return (
            <MetricCard key={k} componentId="qdash.card.quality" label={name} fullHeight metric={v == null ? "No labels yet" : number(v)}
                        comparison={<Typography.Text color={bad == null ? "secondary" : bad ? "error" : "success"}>
                          {delta(v, then, base && n(base[k]) != null ? "baseline" : "previous window")}{bad == null ? "" : bad ? " (worse)" : " (better)"}
                        </Typography.Text>}
                        buttonText="Breakdown" onButtonClick={() => set({ section: "performance" })}>
              {spark(windows.map((w) => n(table.get(w)?.[k]) ?? 0), `${name} per window`, i ? "category-3" : "category-2")}
            </MetricCard>
          );
        })}
        <MetricCard componentId="qdash.card.drift" label="Drifted columns" fullHeight metric={`${drift.filter(drifted).length} of ${drift.length}`}
                    comparison={`Against the ${compare === "BASELINE" ? "baseline" : "previous window"}`}
                    buttonText="Investigate" onButtonClick={() => set({ section: "drift" })} />
        {m.anomalies_table && (
          <MetricCard componentId="qdash.card.anom" label="Open anomalies" fullHeight metric={open.length}
                      comparison="Series whose latest window is outside its expected range"
                      buttonText="Investigate" onButtonClick={() => set({ section: "anomalies" })} />
        )}
      </div>
      <Grid>
        <Panel span={2} title="Health over time"
               hint={`Each signal per window, darker for a larger value on its own scale. An outline marks a window with drifted columns or anomalies.${qKey ? ` ${qName}: ${lowerBetter ? "lower" : "higher"} is better${qThen != null ? `, baseline ${number(qThen)}` : ""}.` : ""}`}>
          <Heatmap perRow rows={signals.map(([name]) => name)} windows={windows}
                   value={(r, w) => signals.find(([name]) => name === r)![1](w)}
                   format={(v) => number(v)} bad={(r, _w, v) => signals.find(([name]) => name === r)![3]?.(v) ?? false} />
          <SignalOutlines signals={signals} windows={windows} />
        </Panel>
      </Grid>
    </>
  );
}

/** The signals with a threshold, as a strip of flagged windows (the heatmap's outlines, listed). */
function SignalOutlines({ signals, windows }: {
  signals: [string, (w: string) => number | null, (v: number) => string, ((v: number) => boolean)?][]; windows: string[];
}) {
  const flaggedWindows = signals.filter(([, , , bad]) => bad).map(([name, value, format, bad]) => ({
    name, hits: windows.filter((w) => { const v = value(w); return v != null && bad!(v); }).map((w) => `${day(w)} (${format(value(w)!)})`),
  }));
  return (
    <>
      <Spacer size="sm" />
      {flaggedWindows.map(({ name, hits }) => (
        <Typography.Paragraph key={name} css={{ "&&": { margin: 0, fontSize: 12 } }}>
          <Typography.Text bold>{name}:</Typography.Text>{" "}
          {hits.length ? hits.slice(-6).join(", ") + (hits.length > 6 ? ` and ${hits.length - 6} earlier` : "") : "none in this range"}
        </Typography.Paragraph>
      ))}
    </>
  );
}

// --- model performance ----------------------------------------------------------------------

function Performance({ m, input, baseline, windows, pick, range, set }: {
  m: QualityMonitor; input: ProfileRow[]; baseline: ProfileRow[]; windows: string[]; pick: QualityPick; range: number;
  set: (p: Record<string, string | null>) => void;
}) {
  const options = qualityMetrics(m);
  const [metric, setMetric] = useState(options[0][0]);
  const [, name, lowerBetter] = options.find(([k]) => k === metric) ?? options[0];
  const table = new Map(input.filter((r) => r.column_name === TABLE).map((r) => [r.start ?? "", r]));
  const by = (col: string | null) => new Map(input.filter((r) => r.column_name === col).map((r) => [r.start ?? "", r]));
  const pred = by(m.prediction_col), label = by(m.label_col);
  const base = baseline.find((r) => r.column_name === TABLE);
  const xs = windows.map(day);
  const coverage = windows.map((w) => {
    const total = n(table.get(w)?.count), lab = n(label.get(w)?.count);
    return total && lab != null ? (100 * lab) / total : null;
  });
  const latest = windows[windows.length - 1];
  const confusion = table.get(latest)?.confusion_matrix ?? [];
  return (
    <>
      <Spacer size="md" />
      <SegmentedControlGroup componentId="qdash.perf.metric" name="metric" value={metric} onChange={(e) => setMetric(e.target.value)}>
        {options.map(([k, l]) => <SegmentedControlButton key={k} value={k}>{l}</SegmentedControlButton>)}
      </SegmentedControlGroup>
      {!m.label_col && <><Spacer size="sm" /><Alert componentId="qdash.perf.nolabel" type="info" closable={false}
        message="This monitor has no label column, so model quality isn't measured. Prediction drift is on the Drift tab." /></>}
      <Grid>
        <Panel span={2} title={`${name} over time`} hint={`On each window's labelled rows; ${lowerBetter ? "lower" : "higher"} is better. A window whose labels are still arriving is partial: read it with its label coverage below.`}>
          <Chart kind="line" xs={xs} height={260} width={1280} series={[
            { name, points: xs.map((x, i) => ({ x, y: n(table.get(windows[i])?.[metric]) })) },
            ...(base && n(base[metric]) != null ? [{ name: "baseline", points: xs.map((x) => ({ x, y: n(base[metric]) })) }] : [])]} />
        </Panel>
        <Panel title="Label coverage" hint="% of each window's rows whose label has arrived.">
          <Chart kind="bar" xs={xs} height={180} series={[{ name: "labelled %", points: xs.map((x, i) => ({ x, y: coverage[i] })) }]} />
        </Panel>
        {m.problem_type === "PROBLEM_TYPE_REGRESSION" ? (
          <Panel title="Prediction vs label" hint="Each window's average prediction and average label: a gap that opens is bias.">
            <Chart kind="line" xs={xs} height={180} series={[
              { name: `avg ${m.prediction_col}`, points: xs.map((x, i) => ({ x, y: n(pred.get(windows[i])?.avg) })) },
              ...(m.label_col ? [{ name: `avg ${m.label_col}`, points: xs.map((x, i) => ({ x, y: n(label.get(windows[i])?.avg) })) }] : [])]} />
          </Panel>
        ) : (
          <Panel title={`Confusion matrix, ${day(latest)}`} hint="Labelled rows of the latest window.">
            {confusion.length ? <ConfusionMatrix cells={confusion} /> : <Typography.Text color="secondary">No labelled rows in this window.</Typography.Text>}
          </Panel>
        )}
        <SliceBreakdown m={m} metric={metric} name={name} lowerBetter={lowerBetter} pick={pick} range={range} set={set} />
      </Grid>
    </>
  );
}

/** Arize's performance tracing: every slice's metric in the latest window against the whole
 *  table's, worst first; a row drills the dashboard into that slice. */
function SliceBreakdown({ m, metric, name, lowerBetter, pick, range, set }: {
  m: QualityMonitor; metric: string; name: string; lowerBetter: boolean; pick: QualityPick; range: number;
  set: (p: Record<string, string | null>) => void;
}) {
  const { data, error } = useLoad(() => api.qualitySlices(m, pick.granularity, pick.model), [m.bundle, m.target, m.key, pick.granularity, pick.model]);
  const rows = useMemo(() => {
    if (!data) return [];
    // A window's labels arrive as its rows age (a D14 label 14 days on): the slices compare on the
    // latest window whose whole-table metric has them, and trend over the windows that do.
    const windows = [...new Set(data.rows.filter((r) => !r.slice_key && n(r[metric]) != null).map((r) => r.start))]
      .sort().slice(-range);
    const latest = windows[windows.length - 1];
    const whole = data.rows.find((r) => r.start === latest && !r.slice_key);
    const wholeV = n(whole?.[metric]);
    const groups = new Map<string, SliceRow[]>();
    for (const r of data.rows) if (r.slice_key && windows.includes(r.start)) {
      const k = `${r.slice_key}\u0000${r.slice_value}`;
      groups.set(k, [...(groups.get(k) ?? []), r]);
    }
    return [...groups.entries()].map(([k, rs]) => {
      const now = rs.find((r) => r.start === latest);
      const v = n(now?.[metric]);
      const gap = v == null || wholeV == null ? null : (lowerBetter ? v - wholeV : wholeV - v);
      return { k, key: rs[0].slice_key, value: rs[0].slice_value, rows: n(now?.count), v, gap,
               trend: windows.map((w) => n(rs.find((r) => r.start === w)?.[metric]) ?? 0) };
    }).sort((a, b) => (b.gap ?? -Infinity) - (a.gap ?? -Infinity));
  }, [data, metric, lowerBetter, range]);
  const latestLabelled = useMemo(() => {
    const ws = (data?.rows ?? []).filter((r) => !r.slice_key && n(r[metric]) != null).map((r) => r.start).sort();
    return ws.length ? ws[ws.length - 1].slice(0, 10) : null;
  }, [data, metric]);
  return (
    <Panel span={2} title="Performance by slice"
           hint={`Each slice's ${name} in the latest window with labels${rows.length && latestLabelled ? ` (${latestLabelled})` : ""}, its rows there, and how much worse than the whole table it is. Worst first: pick one to see the whole dashboard for it.`}>
      {data && !rows.length && m.slicing_exprs.length > 0 && <Typography.Text color="secondary">No window has labels yet: a slice's {name} shows once its rows' labels arrive.</Typography.Text>}
      {!m.slicing_exprs.length && <Typography.Text color="secondary">The monitor has no slicing expressions: add some in Configure to break performance down.</Typography.Text>}
      {error && <Alert componentId="qdash.slices.error" type="error" message="Couldn't read the slices" description={error} closable={false} />}
      {m.slicing_exprs.length > 0 && !data && !error && <TableSkeleton lines={4} />}
      {data && rows.length > 0 && (
        <div css={{ maxHeight: 420, overflow: "auto" }}>
          <Table scrollable>
            <TableRow isHeader>
              <TableHeader componentId="qdash.slices.h.slice">Slice</TableHeader>
              <TableHeader componentId="qdash.slices.h.rows">Rows</TableHeader>
              <TableHeader componentId="qdash.slices.h.metric">{name}</TableHeader>
              <TableHeader componentId="qdash.slices.h.gap">Worse than whole table by</TableHeader>
              <TableHeader componentId="qdash.slices.h.trend">Trend</TableHeader>
            </TableRow>
            {rows.map((r) => (
              <TableRow key={r.k} onClick={() => set({ slice_key: r.key, slice_value: r.value, section: null })}
                        css={{ cursor: "pointer", background: pick.slice_key === r.key && pick.slice_value === r.value ? SELECTED : undefined }}>
                <TableCell><Typography.Link componentId="qdash.slices.link">{`${r.key} = ${r.value}`}</Typography.Link></TableCell>
                <TableCell>{number(r.rows)}</TableCell>
                <TableCell>{number(r.v)}</TableCell>
                <TableCell>{r.gap == null ? "-" : number(Math.abs(r.gap)) === "0" ? <Typography.Text color="secondary">About the same</Typography.Text>
                  : r.gap > 0 ? <Typography.Text color="error">{number(r.gap)}</Typography.Text>
                  : <Typography.Text color="secondary">Better by {number(-r.gap)}</Typography.Text>}</TableCell>
                <TableCell><div css={{ width: 120, height: 28 }}>
                  <SparklineChart values={r.trend} aria-label={`${name} of ${r.key} = ${r.value} per window`} viewBoxHeightAndWidth={{ width: 120, height: 28 }} />
                </div></TableCell>
              </TableRow>
            ))}
          </Table>
        </div>
      )}
    </Panel>
  );
}

function ConfusionMatrix({ cells }: { cells: { prediction: string; label: string; count: string }[] }) {
  const { theme } = useDesignSystemTheme();
  const classes = [...new Set(cells.flatMap((c) => [c.label, c.prediction]))].sort();
  const at = new Map(cells.map((c) => [`${c.label}\u0000${c.prediction}`, Number(c.count)]));
  const max = Math.max(1, ...at.values());
  return (
    <div css={{ display: "grid", gridTemplateColumns: `120px repeat(${classes.length}, minmax(56px, 1fr))`, maxWidth: 160 + 96 * classes.length, fontSize: 12 }}>
      <div css={{ color: theme.colors.textSecondary }}>label \ predicted</div>
      {classes.map((c) => <div key={c} css={{ textAlign: "center", fontWeight: 600 }}>{c}</div>)}
      {classes.map((l) => [
        <div key={`l-${l}`} css={{ fontWeight: 600 }}>{l}</div>,
        ...classes.map((p) => {
          const v = at.get(`${l}\u0000${p}`) ?? 0;
          return <div key={`${l}-${p}`} css={{ textAlign: "center", padding: 6, margin: 1, borderRadius: 4,
                                              background: `rgba(7, 122, 157, ${0.08 + 0.7 * (v / max)})` }}>{v.toLocaleString()}</div>;
        }),
      ])}
    </div>
  );
}

// --- drift ----------------------------------------------------------------------------------

function Drift({ m, data, windows, compare, hasBaseline, column, pick, range, set }: {
  m: QualityMonitor; data: QualityMetrics; windows: string[]; compare: View; hasBaseline: boolean; column: string | null;
  pick: QualityPick; range: number; set: (p: Record<string, string | null>) => void;
}) {
  const latest = windows[windows.length - 1];
  const all = latestDrift(data.drift, latest, compare);
  // The model's output first (Evidently's prediction drift), then the features.
  const ranked = m.prediction_col ? [...all.filter((d) => d.column_name === m.prediction_col), ...all.filter((d) => d.column_name !== m.prediction_col)] : all;
  const picked = column ?? ranked[0]?.column_name ?? null;
  const rows = data.drift.filter((d) => d.drift_type === compare && d.column_name !== TABLE);
  const at = (c: string) => new Map(rows.filter((d) => d.column_name === c).map((d) => [d.start, d]));
  const tableRow = data.drift.find((d) => d.drift_type === compare && d.column_name === TABLE && d.start === latest);
  const heat = all.slice(0, 20).map((d) => d.column_name);
  const lookup = new Map(rows.map((d) => [`${d.column_name}\u0000${d.start}`, d]));
  return (
    <>
      <Spacer size="md" />
      <div css={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
        <SegmentedControlGroup componentId="qdash.drift.compare" name="compare" value={compare} onChange={(e) => set({ compare: e.target.value })}>
          {hasBaseline && <SegmentedControlButton value="BASELINE">Against baseline</SegmentedControlButton>}
          <SegmentedControlButton value="CONSECUTIVE">Against previous window</SegmentedControlButton>
        </SegmentedControlGroup>
        <Typography.Text color="secondary">
          {all.filter(drifted).length} of {all.length} columns drifted in {day(latest)}
          {tableRow && (n(tableRow.columns_added) || n(tableRow.columns_missing)) ? ` · ${tableRow.columns_added} column(s) newly filled, ${tableRow.columns_missing} now empty` : ""}
        </Typography.Text>
      </div>
      <Grid>
        <Panel title="Columns by drift" hint={`PSI for numbers, Jensen-Shannon distance for categories. Drifted: PSI ≥ ${PSI_DRIFT} or a test's p-value under ${P_DRIFT}.`}>
          {!ranked.length ? <Typography.Text color="secondary">No drift rows for this window.</Typography.Text> : (
            <div css={{ maxHeight: 520, overflow: "auto" }}>
              <Table scrollable>
                <TableRow isHeader>
                  <TableHeader componentId="qdash.drift.h.col">Column</TableHeader>
                  <TableHeader componentId="qdash.drift.h.score">Score</TableHeader>
                  <TableHeader componentId="qdash.drift.h.p">p-value</TableHeader>
                  <TableHeader componentId="qdash.drift.h.trend">Trend</TableHeader>
                </TableRow>
                {ranked.map((d) => {
                  const s = at(d.column_name);
                  return (
                    <TableRow key={d.column_name} onClick={() => set({ column: d.column_name })}
                              css={{ cursor: "pointer", background: d.column_name === picked ? SELECTED : undefined }}>
                      <TableCell wrapContent>
                        <div css={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, minWidth: 0 }}>
                          <Typography.Text bold={d.column_name === picked} css={{ overflowWrap: "anywhere" }}>{d.column_name}</Typography.Text>
                          {d.column_name === m.prediction_col && <Tag componentId="qdash.drift.pred">Prediction</Tag>}
                          {drifted(d) && <Tag componentId="qdash.drift.tag" color="coral">Drifted</Tag>}
                        </div>
                      </TableCell>
                      <TableCell>{number(score(d))}</TableCell>
                      <TableCell>{pvalue(d) == null ? "-" : pvalue(d)! < 0.001 ? "< 0.001" : number(pvalue(d))}</TableCell>
                      <TableCell><div css={{ width: 100, height: 24 }}>
                        <SparklineChart values={windows.map((w) => (s.get(w) ? score(s.get(w)!) ?? 0 : 0))} viewBoxHeightAndWidth={{ width: 100, height: 24 }}
                                        color={drifted(d) ? "negative" : "category-1"} aria-label={`${d.column_name} drift score per window`} />
                      </div></TableCell>
                    </TableRow>
                  );
                })}
              </Table>
            </div>
          )}
        </Panel>
        {picked && <ColumnDrift m={m} column={picked} series={at(picked)} windows={windows} compare={compare} pick={pick} range={range} />}
        {heat.length > 1 && (
          <Panel span={2} title="Drift over time" hint="The 20 most drifted columns' scores per window; an outline is a drifted window. Pick a row to inspect it.">
            <Heatmap rows={heat} windows={windows} picked={picked} onPick={(c) => set({ column: c })}
                     value={(c, w) => { const d = lookup.get(`${c}\u0000${w}`); return d ? score(d) : null; }}
                     format={(v) => number(v)} bad={(c, w) => { const d = lookup.get(`${c}\u0000${w}`); return !!d && drifted(d); }} />
            <DriftOutlineNote rows={heat} windows={windows} lookup={lookup} />
          </Panel>
        )}
      </Grid>
    </>
  );
}

function DriftOutlineNote({ rows, windows, lookup }: { rows: string[]; windows: string[]; lookup: Map<string, DriftRow> }) {
  const count = rows.reduce((a, c) => a + windows.filter((w) => { const d = lookup.get(`${c}\u0000${w}`); return d && drifted(d); }).length, 0);
  return <Typography.Text color="secondary" size="sm">{count} drifted column-window(s) in this view.</Typography.Text>;
}

/** One column: its distribution now against what drift compares it with (a number's from its
 *  quantiles, a category's from its frequent items), and its drift score over time. */
function ColumnDrift({ m, column, series, windows, compare, pick, range }: {
  m: QualityMonitor; column: string; series: Map<string, DriftRow>; windows: string[]; compare: View; pick: QualityPick; range: number;
}) {
  const { data, error } = useLoad(() => api.qualityColumn(m, column, pick),
    [m.bundle, m.target, m.key, column, pick.granularity, pick.model, pick.slice_key, pick.slice_value, range]);
  const xs = windows.map(day);
  const latest = windows[windows.length - 1];
  const s = series.get(latest);
  const numeric = s ? n(s.population_stability_index) != null || n(s.ks_pvalue) != null : null;
  return (
    <Panel title={column} hint={`Distribution in ${day(latest)} against ${compare === "BASELINE" ? "the baseline" : "the window before"}, and its drift score per window.`}>
      {error && <Alert componentId="qdash.col.error" type="error" message="Couldn't read the column" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={4} />}
      {data && <Distribution rows={data.rows} latest={latest} compare={compare} windows={windows} numeric={numeric} />}
      <Spacer size="sm" />
      <Typography.Text bold size="sm">{numeric === false ? "JS distance" : "PSI"} per window</Typography.Text>
      <Chart kind="line" xs={xs} height={150} series={[
        { name: column, points: xs.map((x, i) => ({ x, y: series.get(windows[i]) ? score(series.get(windows[i])!) : null })) },
        ...(numeric !== false ? [{ name: `threshold ${PSI_DRIFT}`, points: xs.map((x) => ({ x, y: PSI_DRIFT })) }] : [])]} />
      {s && (
        <Typography.Text color="secondary" size="sm">
          {n(s.ks_pvalue) != null ? `KS ${number(n(s.ks_statistic))} (p ${number(n(s.ks_pvalue))})` : n(s.chi_squared_pvalue) != null ? `χ² ${number(n(s.chi_squared_statistic))} (p ${number(n(s.chi_squared_pvalue))})` : ""}
          {n(s.wasserstein_distance) != null ? ` · Wasserstein ${number(n(s.wasserstein_distance))}` : ""}
          {n(s.tv_distance) != null ? ` · TV ${number(n(s.tv_distance))}` : ""}
          {n(s.percent_null_delta) != null ? ` · Δ null ${number(n(s.percent_null_delta))} pts` : ""}
        </Typography.Text>
      )}
    </Panel>
  );
}

function Distribution({ rows, latest, compare, windows, numeric }: {
  rows: ColumnDist[]; latest: string; compare: View; windows: string[]; numeric: boolean | null;
}) {
  const now = rows.find((r) => r.log_type === "INPUT" && r.start === latest);
  const prevWindow = windows[windows.length - 2];
  const ref = compare === "BASELINE" ? rows.find((r) => r.log_type === "BASELINE") : rows.find((r) => r.log_type === "INPUT" && r.start === prevWindow);
  const refName = compare === "BASELINE" ? "baseline" : day(prevWindow ?? null);
  if (!now) return <Typography.Text color="secondary">No profile of this column in the latest window.</Typography.Text>;
  const qs = (r?: ColumnDist) => (r?.quantiles ?? []).map(n).filter((v): v is number => v != null);
  const q1 = qs(now), q0 = qs(ref);
  if ((numeric ?? true) && q1.length > 1) {
    // A histogram from quantiles: each bin's share of the quantile points that fall in it.
    const lo = Math.min(...q1, ...q0), hi = Math.max(...q1, ...q0), bins = 20, width = (hi - lo) / bins || 1;
    const hist = (q: number[]) => Array.from({ length: bins }, (_, b) =>
      q.length ? (100 * q.filter((v) => (b === bins - 1 ? v >= lo + b * width : v >= lo + b * width && v < lo + (b + 1) * width)).length) / q.length : null);
    const xs = Array.from({ length: bins }, (_, b) => number(lo + (b + 0.5) * width));
    const series: Series[] = [{ name: day(latest), points: xs.map((x, i) => ({ x, y: hist(q1)[i] })) },
      ...(q0.length ? [{ name: refName, points: xs.map((x, i) => ({ x, y: hist(q0)[i] })) }] : [])];
    return <>
      <Typography.Text bold size="sm">Share of values per bin (%)</Typography.Text>
      <Chart kind="bar" xs={xs} height={180} series={series} />
    </>;
  }
  const share = (r?: ColumnDist) => {
    const total = n(r?.count) ?? 0;
    return new Map((r?.frequent_items ?? []).map((t) => [t.item, total ? (100 * Number(t.count)) / total : 0]));
  };
  const s1 = share(now), s0 = share(ref);
  const items = [...new Set([...s1.keys(), ...s0.keys()])].sort((a, b) => (s1.get(b) ?? 0) - (s1.get(a) ?? 0)).slice(0, 12);
  if (!items.length) return <Typography.Text color="secondary">No distribution kept for this column.</Typography.Text>;
  return <>
    <Typography.Text bold size="sm">Share of rows per value (%), top {items.length}</Typography.Text>
    <Chart kind="bar" xs={items} height={180} series={[
      { name: day(latest), points: items.map((x) => ({ x, y: s1.get(x) ?? 0 })) },
      ...(ref ? [{ name: refName, points: items.map((x) => ({ x, y: s0.get(x) ?? 0 })) }] : [])]} />
  </>;
}

// --- data integrity -------------------------------------------------------------------------

function Integrity({ data, input, baseline, windows, compare }: {
  data: QualityMetrics; input: ProfileRow[]; baseline: ProfileRow[]; windows: string[]; compare: View;
}) {
  const xs = windows.map(day);
  const latest = windows[windows.length - 1];
  const table = new Map(input.filter((r) => r.column_name === TABLE).map((r) => [r.start ?? "", r]));
  const cols = input.filter((r) => r.start === latest && r.column_name !== TABLE);
  const byKey = new Map(input.map((r) => [`${r.column_name}\u0000${r.start}`, r]));
  const maxNull = (c: string) => Math.max(0, ...windows.map((w) => n(byKey.get(`${c}\u0000${w}`)?.percent_null) ?? 0));
  const nullCols = [...new Set(input.filter((r) => r.column_name !== TABLE).map((r) => r.column_name))]
    .filter((c) => maxNull(c) > 0).sort((a, b) => maxNull(b) - maxNull(a)).slice(0, 20);
  const schema = windows.map((w) => data.drift.find((d) => d.drift_type === compare && d.column_name === TABLE && d.start === w));
  return (
    <Grid>
      <Panel title="Rows per window" hint="A drop or a gap is missing data upstream.">
        <Chart kind="bar" xs={xs} height={180} series={[{ name: "rows", points: xs.map((x, i) => ({ x, y: n(table.get(windows[i])?.count) })) }]} />
      </Panel>
      <Panel title="Schema changes" hint={`Columns that became filled or empty against the ${compare === "BASELINE" ? "baseline" : "previous window"}.`}>
        <Chart kind="bar" xs={xs} height={180} series={[
          { name: "newly filled", points: xs.map((x, i) => ({ x, y: n(schema[i]?.columns_added) })) },
          { name: "now empty", points: xs.map((x, i) => ({ x, y: n(schema[i]?.columns_missing) })) }]} />
      </Panel>
      <Panel span={2} title="Null % over time" hint="The 20 columns with the most nulls in this range.">
        {nullCols.length ? <Heatmap rows={nullCols} windows={windows} value={(c, w) => n(byKey.get(`${c}\u0000${w}`)?.percent_null)} format={(v) => `${number(v)}%`} />
          : <Typography.Text color="secondary">No column has nulls in this range.</Typography.Text>}
      </Panel>
      <Panel span={2} title={`Column profile, ${day(latest)}`} hint="Each column's statistics in the latest window, the baseline's in brackets.">
        <ProfileTable rows={cols} baseline={baseline} />
      </Panel>
    </Grid>
  );
}

function ProfileTable({ rows, baseline }: { rows: ProfileRow[]; baseline: ProfileRow[] }) {
  const base = new Map(baseline.map((r) => [r.column_name, r]));
  return (
    <div css={{ maxHeight: 480, overflow: "auto" }}>
      <Table scrollable>
        <TableRow isHeader>
          <TableHeader componentId="qdash.prof.h.col">Column</TableHeader>
          <TableHeader componentId="qdash.prof.h.count">Count</TableHeader>
          <TableHeader componentId="qdash.prof.h.null">Null %</TableHeader>
          <TableHeader componentId="qdash.prof.h.zero">Zero %</TableHeader>
          <TableHeader componentId="qdash.prof.h.distinct">Distinct</TableHeader>
          <TableHeader componentId="qdash.prof.h.avg">Avg / top values</TableHeader>
          <TableHeader componentId="qdash.prof.h.range">Min · median · max</TableHeader>
        </TableRow>
        {rows.map((r) => {
          const b = base.get(r.column_name);
          return (
            <TableRow key={r.column_name}>
              <TableCell>{r.column_name}<Typography.Text color="secondary" size="sm" css={{ marginLeft: 6 }}>{r.data_type}</Typography.Text></TableCell>
              <TableCell>{number(n(r.count))}</TableCell>
              <TableCell>{pct(r.percent_null)}{b ? ` (${pct(b.percent_null)})` : ""}</TableCell>
              <TableCell>{pct(r.percent_zeros)}</TableCell>
              <TableCell>{number(n(r.distinct_count))}</TableCell>
              <TableCell>{n(r.avg) == null ? (r.top_items ?? []).slice(0, 3).map((t) => t.item).join(", ") || "-"
                : `${number(n(r.avg))}${b && n(b.avg) != null ? ` (${number(n(b.avg))})` : ""}`}</TableCell>
              <TableCell>{n(r.min) == null ? "-" : `${number(n(r.min))} · ${number(n(r.median))} · ${number(n(r.max))}`}</TableCell>
            </TableRow>
          );
        })}
      </Table>
    </div>
  );
}

// --- fairness and anomalies -----------------------------------------------------------------

/** Fairness and bias of a true/false slice: its true (protected) group against its false group,
 *  per class, protected minus unprotected, on the true rows. */
function Fairness({ m, data, input, windows }: { m: QualityMonitor; data: QualityMetrics; input: ProfileRow[]; windows: string[] }) {
  const latest = input.find((r) => r.column_name === TABLE && r.start === windows[windows.length - 1]);
  const values = FAIRNESS.map(([k]) => new Map(((latest?.[k] as { key: string; value: string | null }[] | null) ?? []).map((x) => [x.key, n(x.value)])));
  const classes = [...new Set(values.flatMap((v) => [...v.keys()]))].sort();
  const boolSlices = data.options.slices.filter((s) => s.value === "true").map((s) => s.key);
  return (
    <Grid>
      <Panel span={2} title="Fairness and bias"
             hint={`For a true/false slice: the true group (protected) against the false group, per class, protected minus unprotected. 0 is parity. On labelled rows; statistical parity on every prediction${m.label_col ? "" : " (no label column: only statistical parity)"}.`}>
        {!classes.length ? (
          <Typography.Text color="secondary">
            {boolSlices.length ? `Pick a slice's true group in the filter bar (${boolSlices.map((k) => `${k} = true`).join(", ")}) to see its fairness.`
              : "Slice the monitor by a true/false expression (Configure › slicing expressions) to compare its two groups."}
          </Typography.Text>
        ) : (
          <>
            <Typography.Text bold>{data.slice_key} = true against false, {day(windows[windows.length - 1])}</Typography.Text>
            <Table scrollable>
              <TableRow isHeader>
                <TableHeader componentId="qdash.fair.h.metric">Metric</TableHeader>
                {classes.map((c) => <TableHeader key={c} componentId="qdash.fair.h.class">{c}</TableHeader>)}
              </TableRow>
              {FAIRNESS.map(([k, name, what], i) => (
                <TableRow key={k}>
                  <TableCell><Tooltip componentId="qdash.fair.tip" content={`Difference in ${what}`}><span>{name}</span></Tooltip></TableCell>
                  {classes.map((c) => <TableCell key={c}>{values[i].get(c) == null ? "-" : number(values[i].get(c))}</TableCell>)}
                </TableRow>
              ))}
            </Table>
          </>
        )}
      </Panel>
    </Grid>
  );
}

/** Anomalies in the profile metrics' series (the monitor's anomaly_detection): each watched
 *  column and metric's latest judged window against its expected range, and one series' history. */
function Anomalies({ rows }: { rows: AnomalyRow[] }) {
  const key = (a: AnomalyRow) => `${a.column_name} · ${a.metric}`;
  const latest = useMemo(() => latestAnomalies(rows), [rows]);
  const hits = useMemo(() => {
    const c = new Map<string, number>();
    for (const a of rows) if (flagged(a)) c.set(key(a), (c.get(key(a)) ?? 0) + 1);
    return c;
  }, [rows]);
  const [picked, setPicked] = useState<string | null>(null);
  const series = picked ?? (latest[0] ? key(latest[0]) : null);
  const points = rows.filter((a) => key(a) === series);
  const xs = points.map((a) => day(a.start));
  return (
    <Grid>
      <Panel title="Watched series" hint="Each series' latest judged window against the range its own history expects. A series without enough history is Training.">
        {!rows.length ? <Typography.Text color="secondary">Nothing judged yet: anomalies are judged after each refresh.</Typography.Text> : (
          <div css={{ maxHeight: 480, overflow: "auto" }}>
            <Table scrollable>
              <TableRow isHeader>
                <TableHeader componentId="qdash.anom.h.series">Column · metric</TableHeader>
                <TableHeader componentId="qdash.anom.h.value">Value</TableHeader>
                <TableHeader componentId="qdash.anom.h.range">Expected</TableHeader>
                <TableHeader componentId="qdash.anom.h.status">Status</TableHeader>
                <TableHeader componentId="qdash.anom.h.hits">Flagged windows</TableHeader>
              </TableRow>
              {latest.map((a) => (
                <TableRow key={key(a)} onClick={() => setPicked(key(a))} css={{ cursor: "pointer", background: key(a) === series ? SELECTED : undefined }}>
                  <TableCell><Typography.Text bold={key(a) === series}>{key(a)}</Typography.Text></TableCell>
                  <TableCell>{number(n(a.value))}</TableCell>
                  <TableCell>{n(a.lower_bound) == null ? "-" : `${number(n(a.lower_bound))} – ${number(n(a.upper_bound))}`}</TableCell>
                  <TableCell><Tag componentId="qdash.anom.tag" color={STATUS_COLOR[a.status] ?? "default"}>{a.status}</Tag></TableCell>
                  <TableCell>{hits.get(key(a)) ?? 0}</TableCell>
                </TableRow>
              ))}
            </Table>
          </div>
        )}
      </Panel>
      {series && (
        <Panel title={series} hint="The value against its expected range per judged window.">
          <Chart kind="line" xs={xs} height={240} series={[
            { name: "value", points: points.map((a, i) => ({ x: xs[i], y: n(a.value) })) },
            { name: "lower", points: points.map((a, i) => ({ x: xs[i], y: n(a.lower_bound) })) },
            { name: "upper", points: points.map((a, i) => ({ x: xs[i], y: n(a.upper_bound) })) }]} />
        </Panel>
      )}
    </Grid>
  );
}

