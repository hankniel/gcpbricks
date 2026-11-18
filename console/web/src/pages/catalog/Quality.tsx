import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Accordion, Alert, Button, DashboardIcon, Empty, GearIcon, HistoryIcon, KeyValueList, KeyValueListItem, MetricCard,
  SimpleSelect, SimpleSelectOption, Spacer, SparklineChart, TableCell, TableRow, TableSkeleton, Tag, Typography,
  useDesignSystemTheme,
} from "@databricks/design-system";
import { api, Column, MonitorLeftover, QualityMonitor } from "../../api";
import { TableLink } from "../../components/BigQueryCosts";
import { number } from "../../components/Chart";
import MonitorModal from "../../components/MonitorModal";
import RefreshHistoryModal from "../../components/RefreshHistoryModal";
import { useToast } from "../../components/Toast";
import { Status } from "../../components/Status";
import { Table, TableHeader } from "../../components/Table";
import { runState, useLoad, when } from "../../util";
import {
  day, delta, drifted, flagged, latestAnomalies, latestDrift, monitorLink, n, qualityMetrics, score, split, STATUS_COLOR,
  TABLE, worse,
} from "./qualityShared";

/** Catalog Explorer's Quality tab, as Databricks lays it out: the table's data profiling at a
 *  glance (health of its latest window, what to look at first, its configuration), with Configure,
 *  View refresh history and View dashboard; the dashboard holds the deep dive. Without a monitor,
 *  Create monitor. */
export default function QualityTab({ dataset, table, project, fullName, columns }: {
  dataset: string; table: string; project?: string; fullName: string; columns: Column[];
}) {
  const { data, error, reload } = useLoad(() => api.tableMonitors(dataset, table, project), [dataset, table, project]);
  const left = useLoad(() => api.tableMonitorLeftovers(dataset, table, project), [dataset, table, project]);
  const targets = useLoad(() => api.consoleMonitorTargets(), []);
  const notify = useToast();
  const [picked, setPicked] = useState(0);
  const [creating, setCreating] = useState(false);
  const deploy = async (key: string, spec: Record<string, unknown>, target: string) => {
    const r = await api.deployMonitor(target, key, spec);
    notify({ title: `Monitor ${key} deployed to ${target}`, description: r.note ?? "Its refresh job is ready: refresh the metrics to fill the dashboard." });
    reload();
  };
  if (error) return <Alert componentId="quality.error" type="error" message="Couldn't read the quality monitors" description={error} closable={false} />;
  if (!data) return <TableSkeleton lines={6} />;
  const leftovers = <Leftovers rows={left.data ?? []} />;
  if (!data.length) {
    return (
      <>
        {leftovers}
        <Empty title="Data profiling is not enabled"
               description="Create a monitor to capture profile and drift metrics for this table, and model performance for an inference table."
               button={<Button componentId="quality.create" type="primary" onClick={() => setCreating(true)}>Create monitor</Button>} />
        <MonitorModal open={creating} onClose={() => setCreating(false)} table={fullName} columns={columns}
                      deploy={targets.data?.length ? deploy : undefined} targets={targets.data ?? []} />
      </>
    );
  }
  const m = data[Math.min(picked, data.length - 1)];
  return (
    <>
      {leftovers}
      {data.length > 1 && (
        <div css={{ maxWidth: 420, marginBottom: 12 }}>
          <SimpleSelect componentId="quality.monitor" id="quality-monitor" label="Monitor" value={String(picked)}
                        onChange={(e) => setPicked(Number(e.target.value))}>
            {data.map((x, i) => <SimpleSelectOption key={i} value={String(i)}>{`${x.key} · ${x.bundle} (${x.target})${x.via ? ` · via ${x.via.split(".").slice(1).join(".")}` : ""}`}</SimpleSelectOption>)}
          </SimpleSelect>
        </div>
      )}
      {m.via && (
        <div css={{ marginBottom: 12 }}>
          <Alert componentId="quality.via" type="info" closable={false}
                 message={<>Monitored through the view <TableLink fqn={m.via} /></>}
                 description={`Monitor ${m.key} (${m.bundle}, ${m.target}) profiles that view, which reads this table: its metrics below are the view's.`} />
        </div>)}
      <Overview key={`${m.bundle}/${m.target}/${m.key}`} m={m} fullName={m.via ?? fullName} columns={m.via ? [] : columns} deploy={deploy}
                onDeleted={() => { setPicked(0); reload(); }} />
    </>
  );
}

function Overview({ m, fullName, columns, deploy, onDeleted }: {
  m: QualityMonitor; fullName: string; columns: Column[];
  deploy: (key: string, spec: Record<string, unknown>, target: string) => Promise<void>; onDeleted: () => void;
}) {
  const { theme } = useDesignSystemTheme();
  const notify = useToast();
  // A console-created monitor is updated and deleted here; a bundle's, through its YAML.
  const ours = m.bundle === CONSOLE_BUNDLE;
  const remove = async () => {
    await api.deleteMonitor(m.target, m.key);
    notify({ title: `Monitor ${m.key} deleted`, description: "Its refresh job is retired; the metric tables stay." });
    onDeleted();
  };
  const nav = useNavigate();
  const [granularity, setGranularity] = useState(m.granularities[0] ?? "");
  const [history, setHistory] = useState(false);
  const [configure, setConfigure] = useState(false);
  const { data, error } = useLoad(() => api.quality(m, { granularity, model: "", slice_key: "", slice_value: "" }), [m.bundle, m.target, m.key, granularity]);
  const job = useLoad(() => api.job(m.workflow), [m.workflow]);
  const [starting, setStarting] = useState(false);
  const refresh = async () => {
    setStarting(true);
    try {
      await api.runNow(m.workflow, {});
      notify({ title: `Refreshing ${m.key}`, description: "Its refresh job started; the metrics show here once it finishes." });
      job.reload();
    } catch (e) {
      notify({ title: "Couldn't start the refresh", description: (e as Error).message });
    } finally {
      setStarting(false);
    }
  };
  const last = job.data?.runs[0];
  const dash = (q = "") => `${monitorLink(m)}${q ? `?${q}` : granularity ? `?granularity=${encodeURIComponent(granularity)}` : ""}`;
  return (
    <>
      <div css={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: theme.spacing.md, flexWrap: "wrap" }}>
        <div>
          <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm, flexWrap: "wrap" }}>
            <Typography.Title level={3} css={{ "&&": { margin: 0 } }}>Data profiling</Typography.Title>
            <Tag componentId="quality.type">{m.profile_type === "InferenceLog" ? "Inference" : m.profile_type === "TimeSeries" ? "Time series" : "Snapshot"}</Tag>
            {m.problem_type && <Tag componentId="quality.problem">{m.problem_type === "PROBLEM_TYPE_REGRESSION" ? "Regression" : "Classification"}</Tag>}
            <Tag componentId="quality.anomaly" color={m.anomalies_table ? "lime" : undefined}>
              Anomaly detection {m.anomalies_table ? "on" : "off"}</Tag>
          </div>
          <Typography.Text color="secondary">
            {last ? <>Last refresh {when(last.start, true)} · <Status state={runState(last)} /> · </> : job.data ? "Not refreshed yet · " : ""}
            {m.schedule ? `Refreshes on ${m.schedule.quartz_cron_expression} (${m.schedule.timezone_id})` : "Refreshed manually"}
          </Typography.Text>
        </div>
        <div css={{ display: "flex", gap: theme.spacing.sm, flexWrap: "wrap" }}>
          <Button componentId="quality.refresh" loading={starting} disabled={!!last && runState(last) === "ACTIVE"}
                  onClick={refresh}>Refresh metrics</Button>
          <Button componentId="quality.history" icon={<HistoryIcon />} onClick={() => setHistory(true)}>View refresh history</Button>
          <Button componentId="quality.configure" icon={<GearIcon />} onClick={() => setConfigure(true)}>Configure</Button>
          <Button componentId="quality.dashboard" type="primary" icon={<DashboardIcon />} onClick={() => nav(dash())}>View dashboard</Button>
        </div>
      </div>
      <Spacer size="md" />
      {m.granularities.length > 1 && (
        <div css={{ width: 180, marginBottom: theme.spacing.md }}>
          <SimpleSelect componentId="quality.granularity" id="quality-granularity" label="Granularity" value={granularity}
                        onChange={(e) => setGranularity(e.target.value)}>
            {m.granularities.map((g) => <SimpleSelectOption key={g} value={g}>{g}</SimpleSelectOption>)}
          </SimpleSelect>
        </div>
      )}
      {error && <Alert componentId="quality.metrics.error" type="error" message="Couldn't read the metric tables" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={6} />}
      {data && data.refreshed === false && <NotRefreshed m={m} running={!!last && runState(last) === "ACTIVE"} refresh={refresh} starting={starting} />}
      {data && data.refreshed !== false && <Summary m={m} data={data} dash={dash} />}
      <Spacer size="lg" />
      <Accordion componentId="quality.config" defaultActiveKey={[]}>
        <Accordion.Panel header="Monitor configuration" key="config"><Config m={m} /></Accordion.Panel>
      </Accordion>
      <RefreshHistoryModal m={m} open={history} onClose={() => setHistory(false)} />
      <MonitorModal open={configure} onClose={() => setConfigure(false)} table={fullName} columns={columns} monitor={m}
                    deploy={ours ? deploy : undefined} remove={ours ? remove : undefined} />
    </>
  );
}

/** A monitor set up but never refreshed: no metric tables yet. What it will write, and how to fill it. */
function NotRefreshed({ m, running, refresh, starting }: { m: QualityMonitor; running: boolean; refresh: () => void; starting: boolean }) {
  return (
    <Empty title="Set up, not refreshed yet"
           description={<>
             Monitor {m.key} ({m.bundle}, {m.target}) is deployed for this table: {m.schedule
               ? `it refreshes on ${m.schedule.quartz_cron_expression} (${m.schedule.timezone_id})`
               : "it has no schedule of its own, so it refreshes when a job runs it (its bundle's run_job_task) or from here"}.
             A refresh creates its metric tables: <TableLink fqn={m.profile_metrics_table} />, <TableLink fqn={m.drift_metrics_table} />
             {m.anomalies_table ? <>, <TableLink fqn={m.anomalies_table} /></> : null}.
           </>}
           button={<Button componentId="quality.refresh.first" type="primary" loading={starting} disabled={running} onClick={refresh}>
             {running ? "Refreshing…" : "Refresh metrics now"}</Button>} />
  );
}

/** Deleted monitors whose metric tables are still there, as Databricks notes a deleted monitor's assets. */
function Leftovers({ rows }: { rows: MonitorLeftover[] }) {
  if (!rows.length) return null;
  return (
    <div css={{ marginBottom: 12 }}>
      {rows.map((r) => (
        <Alert key={`${r.bundle}/${r.target}/${r.key}`} componentId="quality.leftovers" type="warning" closable
               message={`Monitor ${r.key} (${r.bundle}, ${r.target}) was deleted ${r.retired}, but not its output tables`}
               description={<>The following still exist; delete them in BigQuery if they're no longer wanted:
                 <ul css={{ margin: "4px 0 0" }}>{r.tables.map((t) => <li key={t}><TableLink fqn={t} /></li>)}</ul></>} />))}
    </div>
  );
}

type Data = Awaited<ReturnType<typeof api.quality>>;
/** The console's own bundle of monitors created here (console.monitor_deploy). */
const CONSOLE_BUNDLE = "console-monitors";

/** The latest window against the baseline (else the window before): rows, model quality, label
 *  coverage, drift and anomalies, each a card with its trend; then what to look at first. */
function Summary({ m, data, dash }: { m: QualityMonitor; data: Data; dash: (q?: string) => string }) {
  const { theme } = useDesignSystemTheme();
  const { input, baseline, windows, latest } = split(data);
  if (!windows.length) {
    return <Empty title="No metrics yet" description="The metric tables are empty: refresh the metrics from View refresh history." />;
  }
  const table = new Map(input.filter((r) => r.column_name === TABLE).map((r) => [r.start ?? "", r]));
  const base = baseline.find((r) => r.column_name === TABLE);
  const prev = windows.length > 1 ? table.get(windows[windows.length - 2]) : undefined;
  const now = table.get(latest);
  const series = (k: string) => windows.map((w) => n(table.get(w)?.[k]) ?? 0);
  const [qKey, qName, lowerBetter] = qualityMetrics(m)[0] ?? [];
  // Labels arrive as a window's rows age (a D14 label, 14 days after its snapshot): model quality reads
  // on the latest window that has them, not the latest window.
  const labelled = qKey ? windows.filter((w) => n(table.get(w)?.[qKey]) != null) : [];
  const lastLabelled = labelled[labelled.length - 1];
  const prevLabelled = labelled.length > 1 ? table.get(labelled[labelled.length - 2]) : undefined;
  const atLabelled = lastLabelled ? table.get(lastLabelled) : undefined;
  const qNow = qKey ? n(atLabelled?.[qKey]) : null;
  const qThen = qKey ? (base && n(base[qKey]) != null ? n(base[qKey]) : n(prevLabelled?.[qKey])) : null;
  const against = base && qKey && n(base[qKey]) != null ? "baseline" : "previous labelled window";
  // The monitor's own metrics (the release gate's, as a rule), on the same window.
  const custom = m.custom_metrics.slice(0, 2);
  const label = m.label_col ? new Map(input.filter((r) => r.column_name === m.label_col).map((r) => [r.start ?? "", r])) : null;
  const coverage = (w: string) => {
    const total = n(table.get(w)?.count), lab = n(label?.get(w)?.count);
    return total && lab != null ? (100 * lab) / total : null;
  };
  const hasBaseline = data.drift.some((d) => d.drift_type === "BASELINE");
  const view = hasBaseline ? "BASELINE" : "CONSECUTIVE";
  const drift = latestDrift(data.drift, latest, view);
  const driftedNow = drift.filter(drifted);
  const anomalies = latestAnomalies(data.anomalies ?? []);
  const open = anomalies.filter(flagged);
  const tone = (bad: boolean | null, text: string) => (
    <Typography.Text color={bad == null ? "secondary" : bad ? "error" : "success"}>{text}</Typography.Text>);
  const spark = (values: number[], label: string, color = "category-1") => (
    <div css={{ height: 32, marginTop: theme.spacing.xs }}>
      <SparklineChart values={values} color={color as "category-1"} fillInAreaBelowSparkline aria-label={label} />
    </div>
  );
  return (
    <>
      <Typography.Text color="secondary" size="sm">
        Latest window {day(latest)} ({data.granularity || "snapshot"}), whole table, all models.
        {qKey && (lastLabelled ? ` Model quality on ${day(lastLabelled)}, the latest window with labels.` : " No window has labels yet.")}
      </Typography.Text>
      <Spacer size="sm" />
      <div css={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: theme.spacing.md }}>
        <MetricCard componentId="quality.card.rows" label="Rows" metric={number(n(now?.count))} fullHeight
                    comparison={tone(null, delta(n(now?.count), n(prev?.count), "previous window"))}>
          {spark(series("count"), `Rows per window, ${windows.length} windows`)}
        </MetricCard>
        {qKey && (
          <MetricCard componentId="quality.card.quality" label={qName!} fullHeight
                      metric={qNow == null ? "No labels yet" : number(qNow)}
                      labelTooltip={`Model quality on the labelled rows of the latest window with labels, ${lowerBetter ? "lower" : "higher"} is better.`}
                      comparison={qNow == null ? "Labels arrive as the rows age" : tone(worse(qNow, qThen, !!lowerBetter), delta(qNow, qThen, against))}>
            {spark(labelled.map((w) => n(table.get(w)?.[qKey]) ?? 0), `${qName} per labelled window`, "category-2")}
          </MetricCard>
        )}
        {qKey && custom.map((c) => {
          const v = n(atLabelled?.[c]);
          return (
            <MetricCard key={c} componentId="quality.card.custom" label={c} fullHeight metric={v == null ? "-" : number(v)}
                        labelTooltip="One of the monitor's own metrics (custom_metrics), on the latest window with labels."
                        comparison={lastLabelled ? `Snapshots of ${day(lastLabelled)}` : "No labels yet"}>
              {spark(labelled.map((w) => n(table.get(w)?.[c]) ?? 0), `${c} per labelled window`, "category-4")}
            </MetricCard>);
        })}
        {label && (
          <MetricCard componentId="quality.card.coverage" label="Label coverage" fullHeight
                      metric={coverage(latest) == null ? "-" : `${number(coverage(latest))}%`}
                      labelTooltip="Rows of the window whose label has arrived: a partial window's model quality reads on those."
                      comparison={lastLabelled && lastLabelled !== latest
                        ? `Of the latest window's rows · ${number(coverage(lastLabelled))}% on ${day(lastLabelled)}` : "Of the latest window's rows"}>
            {spark(windows.map((w) => coverage(w) ?? 0), "Label coverage per window", "category-3")}
          </MetricCard>
        )}
        <MetricCard componentId="quality.card.drift" label="Drifted columns" fullHeight
                    metric={`${driftedNow.length} of ${drift.length}`}
                    labelTooltip="PSI ≥ 0.2 or a test's p-value under 0.05."
                    comparison={`Against the ${hasBaseline ? "baseline" : "previous window"}`} />
        {m.anomalies_table && (
          <MetricCard componentId="quality.card.anomalies" label="Open anomalies" fullHeight
                      metric={open.length} comparison={`Of ${anomalies.length} watched series`} />
        )}
      </div>
      <Spacer size="md" />
      <div css={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(380px, 1fr))", gap: theme.spacing.md }}>
        <Card title="Top drifted columns" link={<Link to={dash("section=drift")}>See all drift</Link>}>
          {!drift.length ? <Typography.Text color="secondary">No drift for this window yet.</Typography.Text> : (
            <Table>
              <TableRow isHeader>
                <TableHeader componentId="quality.top.h.col">Column</TableHeader>
                <TableHeader componentId="quality.top.h.score">PSI / JS</TableHeader>
                <TableHeader componentId="quality.top.h.state">State</TableHeader>
              </TableRow>
              {drift.slice(0, 5).map((d) => (
                <TableRow key={d.column_name}>
                  <TableCell><Link to={dash(`section=drift&column=${encodeURIComponent(d.column_name)}`)}>{d.column_name}</Link></TableCell>
                  <TableCell>{number(score(d))}</TableCell>
                  <TableCell>{drifted(d) ? <Tag componentId="quality.top.tag" color="coral">Drifted</Tag> : "Stable"}</TableCell>
                </TableRow>))}
            </Table>
          )}
        </Card>
        {m.anomalies_table ? (
          <Card title="Anomalies" link={<Link to={dash("section=anomalies")}>See all anomalies</Link>}>
            {!anomalies.length ? <Typography.Text color="secondary">Nothing judged yet: anomalies are judged after each refresh.</Typography.Text> : (
              <Table>
                <TableRow isHeader>
                  <TableHeader componentId="quality.anom.h.series">Column · metric</TableHeader>
                  <TableHeader componentId="quality.anom.h.value">Value</TableHeader>
                  <TableHeader componentId="quality.anom.h.status">Status</TableHeader>
                </TableRow>
                {anomalies.slice(0, 5).map((a) => (
                  <TableRow key={`${a.column_name}·${a.metric}`}>
                    <TableCell>{a.column_name} · {a.metric}</TableCell>
                    <TableCell>{number(n(a.value))}</TableCell>
                    <TableCell><Tag componentId="quality.anom.tag" color={STATUS_COLOR[a.status] ?? "default"}>{a.status}</Tag></TableCell>
                  </TableRow>))}
              </Table>
            )}
          </Card>
        ) : (
          <Card title="Data integrity" link={<Link to={dash("section=integrity")}>See integrity</Link>}>
            <Integrity input={input} latest={latest} />
          </Card>
        )}
      </div>
    </>
  );
}

/** The columns with the most nulls in the latest window. */
function Integrity({ input, latest }: { input: Data["profile"]; latest: string }) {
  const rows = input.filter((r) => r.start === latest && r.column_name !== TABLE && (n(r.percent_null) ?? 0) > 0)
    .sort((a, b) => (n(b.percent_null) ?? 0) - (n(a.percent_null) ?? 0)).slice(0, 5);
  if (!rows.length) return <Typography.Text color="secondary">No column has nulls in the latest window.</Typography.Text>;
  return (
    <Table>
      <TableRow isHeader>
        <TableHeader componentId="quality.int.h.col">Column</TableHeader>
        <TableHeader componentId="quality.int.h.null">Null %</TableHeader>
      </TableRow>
      {rows.map((r) => (
        <TableRow key={r.column_name}><TableCell>{r.column_name}</TableCell><TableCell>{number(n(r.percent_null))}%</TableCell></TableRow>))}
    </Table>
  );
}

function Card({ title, link, children }: { title: string; link?: React.ReactNode; children: React.ReactNode }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div css={{ border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd, padding: theme.spacing.md,
                background: theme.colors.backgroundPrimary, minWidth: 0 }}>
      <div css={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: theme.spacing.sm }}>
        <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>{title}</Typography.Title>
        {link}
      </div>
      {children}
    </div>
  );
}

export function Config({ m }: { m: QualityMonitor }) {
  const tableLink = (fqn: string) => {
    const [project, ds, t] = fqn.split(".");
    return <Link to={`/catalog/${ds}/${t}?project=${encodeURIComponent(project)}`}>{fqn}</Link>;
  };
  const code = (s: string) => <Typography.Text code>{s}</Typography.Text>;
  return (
    <div css={{ maxWidth: 760 }}>
      <KeyValueList aria-label="Monitor configuration">
        <KeyValueListItem label="Monitor">{m.key} in <Link to={`/bundles/${m.bundle}`}>{m.bundle}</Link> ({m.target})</KeyValueListItem>
        <KeyValueListItem label="Refresh job"><Link to={`/jobs/${m.workflow}`}>{m.job}</Link></KeyValueListItem>
        {m.timestamp_col && <KeyValueListItem label="Timestamp column">{code(m.timestamp_col)}</KeyValueListItem>}
        {m.granularities.length > 0 && <KeyValueListItem label="Granularities">{m.granularities.join(", ")}</KeyValueListItem>}
        {m.prediction_col && <KeyValueListItem label="Prediction column">{code(m.prediction_col)}</KeyValueListItem>}
        {m.prediction_col && <KeyValueListItem label="Label column">{m.label_col ? code(m.label_col) : "None"}</KeyValueListItem>}
        {m.model_id_col && <KeyValueListItem label="Model ID column">{code(m.model_id_col)}</KeyValueListItem>}
        {m.prediction_proba_col && <KeyValueListItem label="Probability column">{code(m.prediction_proba_col)}</KeyValueListItem>}
        <KeyValueListItem label="Baseline table">{m.baseline_table_name ? tableLink(m.baseline_table_name) : "None: drift is against the previous window only"}</KeyValueListItem>
        <KeyValueListItem label="Slicing expressions">{m.slicing_exprs.length ? m.slicing_exprs.map((s) => <Tag key={s} componentId="quality.slice.tag">{s}</Tag>) : "None"}</KeyValueListItem>
        {[...m.custom_metrics, ...(m.drift_custom_metrics ?? [])].length > 0 && (
          <KeyValueListItem label="Custom metrics">
            {[...m.custom_metrics, ...(m.drift_custom_metrics ?? []).map((x) => `${x} (drift)`)].join(", ")}
          </KeyValueListItem>
        )}
        <KeyValueListItem label="Profile metrics table">{tableLink(m.profile_metrics_table)}</KeyValueListItem>
        <KeyValueListItem label="Drift metrics table">{tableLink(m.drift_metrics_table)}</KeyValueListItem>
        {m.anomalies_table && <KeyValueListItem label="Anomalies table">{tableLink(m.anomalies_table)}</KeyValueListItem>}
      </KeyValueList>
    </div>
  );
}
