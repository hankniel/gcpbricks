import { useMemo, useState } from "react";
import {
  Alert, Modal, Spacer, TableCell, TableRow, TableSkeleton, Tag, Tooltip, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "./Table";
import { api, BqStage, DbtModel, MetricUnit } from "../api";
import Chart from "./Chart";
import { clock, duration, secondsText, useLoad } from "../util";

export function bytesText(b?: number | null): string {
  if (b == null) return "-";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0, v = b;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i && v < 10 ? 1 : 0)} ${units[i]}`;
}
export const usd = (v?: number | null) => v == null ? "-" : v === 0 ? "$0" : v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
const pct = (v?: number | null) => v == null ? "-" : `${Math.round(v * 100)}%`;
const slot = (ms?: number | null) => ms == null ? "-" : ms < 3_600_000 ? `${secondsText(ms / 1000)}` : `${(ms / 3_600_000).toFixed(1)} slot-h`;

export function Tile({ name, value, hint }: { name: string; value: string; hint?: string }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div css={{ border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd,
                padding: `${theme.spacing.sm}px ${theme.spacing.md}px`, minWidth: 120 }}>
      <Typography.Text color="secondary" size="sm">{name}</Typography.Text>
      <div><Typography.Text bold css={{ fontSize: 18 }}>{value}</Typography.Text></div>
      {hint && <Typography.Text color="secondary" size="sm">{hint}</Typography.Text>}
    </div>
  );
}

function machineText(u: MetricUnit): string {
  const m = u.machine;
  if (m.machine_type) return `${m.machine_type}${m.accelerator ? ` + ${m.accelerators}× ${m.accelerator}` : ""}${(m.replicas ?? 1) > 1 ? ` × ${m.replicas}` : ""}`;
  return `${m.cpu ?? "?"} vCPU · ${bytesText(m.memory_bytes)}${(m.tasks ?? 1) > 1 ? ` × ${m.tasks} tasks` : ""}`;
}

/** A unit's CPU and memory (and accelerator) as % per minute, and its network per minute. */
export function UnitCharts({ u }: { u: MetricUnit }) {
  const { theme } = useDesignSystemTheme();
  const xs = (u.series.t as string[]).map((t) => clock(t));
  const line = (key: string, name: string, scale = 100) => ({
    name, points: xs.map((x, i) => {
      const v = (u.series[key] as (number | null)[] | undefined)?.[i];
      return { x, y: v == null ? null : v * scale };
    }) });
  const util = [line("cpu", "CPU %"), line("memory", "Memory %"), ...(u.series.accelerator ? [line("accelerator", "Accelerator %")] : [])];
  const net = [line("net_in", "Received MiB", 1 / 2 ** 20), line("net_out", "Sent MiB", 1 / 2 ** 20)];
  if (!xs.length) return <Typography.Text color="secondary">No samples yet: Monitoring samples once a minute and writes them a few minutes later.</Typography.Text>;
  return (
    <div css={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: theme.spacing.md }}>
      <div><Typography.Text bold>Utilization (per minute, %)</Typography.Text><Chart kind="line" xs={xs} series={util} height={200} /></div>
      <div><Typography.Text bold>Network (per minute, MiB)</Typography.Text><Chart kind="bar" xs={xs} series={net} height={200} /></div>
    </div>
  );
}

type SortKey = "seconds" | "bytes_billed" | "slot_ms" | "cost_usd";

/** A dbt command's models, slowest or costliest first, each opening its BigQuery query profile. */
function DbtModels({ rows, open }: { rows: DbtModel[]; open: (r: DbtModel) => void }) {
  const [by, setBy] = useState<SortKey>("cost_usd");
  const sorted = useMemo(() => [...rows].sort((a, b) => (b[by] ?? -1) - (a[by] ?? -1)), [rows, by]);
  const max = Math.max(1e-9, ...rows.map((r) => r[by] ?? 0));
  const head = (k: SortKey, text: string) => (
    <TableHeader componentId={`metrics.dbt.h.${k}`} align="right" sortable sortDirection={by === k ? "desc" : "none"}
                 onToggleSort={() => setBy(k)}>{text}</TableHeader>);
  return (
    <Table scrollable>
      <TableRow isHeader>
        <TableHeader componentId="metrics.dbt.h.name">Node</TableHeader>
        <TableHeader componentId="metrics.dbt.h.status">Status</TableHeader>
        {head("seconds", "Time")}{head("bytes_billed", "Bytes billed")}{head("slot_ms", "Slot time")}{head("cost_usd", "Est. cost")}
        <TableHeader componentId="metrics.dbt.h.share">Share</TableHeader>
      </TableRow>
      {sorted.map((r) => (
        <TableRow key={`${r.command}-${r.unique_id}`}>
          <TableCell>
            {r.job_id ? <Typography.Link componentId="metrics.dbt.open" onClick={() => open(r)}>{r.name}</Typography.Link> : r.name}
            &nbsp;<Tag componentId="metrics.dbt.kind">{r.resource}</Tag>{r.cache_hit && <Tag componentId="metrics.dbt.cache" color="lime">cached</Tag>}
          </TableCell>
          <TableCell><Tooltip componentId="metrics.dbt.msg" content={r.message ?? ""}><span>{r.status}</span></Tooltip></TableCell>
          <TableCell css={{ justifyContent: "flex-end" }}>{r.seconds == null ? "-" : secondsText(r.seconds)}</TableCell>
          <TableCell css={{ justifyContent: "flex-end" }}>{bytesText(r.bytes_billed ?? r.bytes_processed)}</TableCell>
          <TableCell css={{ justifyContent: "flex-end" }}>{slot(r.slot_ms)}</TableCell>
          <TableCell css={{ justifyContent: "flex-end" }}>{usd(r.cost_usd)}</TableCell>
          <TableCell><div css={{ height: 8, width: `${Math.max(2, ((r[by] ?? 0) / max) * 100)}%`, background: "#077A9D", borderRadius: 2 }} /></TableCell>
        </TableRow>))}
    </Table>
  );
}

/** One BigQuery job as its query profile shows it: each stage's time split (average worker vs
 *  slowest), rows, shuffle and spill, as Spark UI shows a stage's tasks. */
function QueryProfile({ job, onClose }: { job: { project: string; location: string; job_id: string } | null; onClose: () => void }) {
  const { theme } = useDesignSystemTheme();
  const p = useLoad(() => (job ? api.bigqueryJob(job.project, job.location, job.job_id) : Promise.resolve(null)), [job?.job_id]);
  const d = p.data;
  const total = (s: BqStage) => s.wait_max + s.read_max + s.compute_max + s.write_max || 1;
  const PARTS: [keyof BqStage, string, string][] = [["wait", "Wait", "#919191"], ["read", "Read", "#8BCAE7"],
    ["compute", "Compute", "#077A9D"], ["write", "Write", "#FFAB00"]].map(([k, n, c]) => [k as keyof BqStage, n, c]);
  return (
    <Modal componentId="metrics.bq.modal" title={`Query profile · ${job?.job_id ?? ""}`} visible={!!job} size="wide" onCancel={onClose} footer={null}>
      {p.error && <Alert componentId="metrics.bq.err" type="error" message="Couldn't load the job" description={p.error} closable={false} />}
      {!d && !p.error && <TableSkeleton lines={6} />}
      {d && <>
        <div css={{ display: "flex", flexWrap: "wrap", gap: theme.spacing.sm }}>
          <Tile name="Bytes billed" value={bytesText(d.bytes_billed)} hint={`${bytesText(d.bytes_processed)} processed`} />
          <Tile name="Est. cost" value={usd(d.cost_usd)} />
          <Tile name="Slot time" value={slot(d.slot_ms)} />
          <Tile name="Spilled" value={bytesText(d.spilled_bytes)} />
          <Tile name="Partitions read" value={String(d.partitions)} hint={d.cache_hit ? "cache hit" : undefined} />
        </div>
        <Spacer size="sm" />
        {d.insights.map((i, n) => <Alert key={n} componentId="metrics.bq.insight" type={i.level} message={i.text} closable={false} css={{ marginBottom: 8 }} />)}
        <Typography.Text color="secondary">Reads {d.referenced.join(", ") || "-"}{d.destination ? ` · writes ${d.destination}` : ""} · <a href={d.link} target="_blank" rel="noreferrer">Open in BigQuery</a></Typography.Text>
        <Spacer size="sm" />
        <Typography.Title level={5}>Stages</Typography.Title>
        <div css={{ display: "flex", gap: theme.spacing.md, fontSize: 12, marginBottom: 4 }}>
          {PARTS.map(([, n, c]) => <span key={n}><span css={{ display: "inline-block", width: 10, height: 10, background: c, marginRight: 4 }} />{n}</span>)}
          <Typography.Text color="secondary" size="sm">thin bar: average worker · thick bar: slowest worker</Typography.Text>
        </div>
        <Table scrollable>
          <TableRow isHeader>
            <TableHeader componentId="metrics.bq.h.stage">Stage</TableHeader>
            <TableHeader componentId="metrics.bq.h.time" css={{ minWidth: 240 }}>Time (avg / slowest worker)</TableHeader>
            <TableHeader componentId="metrics.bq.h.in" align="right">Rows in</TableHeader>
            <TableHeader componentId="metrics.bq.h.out" align="right">Rows out</TableHeader>
            <TableHeader componentId="metrics.bq.h.shuffle" align="right">Shuffle</TableHeader>
            <TableHeader componentId="metrics.bq.h.spill" align="right">Spilled</TableHeader>
            <TableHeader componentId="metrics.bq.h.slot" align="right">Slot time</TableHeader>
          </TableRow>
          {d.stages.map((s) => (
            <TableRow key={s.id}>
              <TableCell>
                <Tooltip componentId="metrics.bq.steps" content={<pre css={{ margin: 0, whiteSpace: "pre-wrap" }}>
                  {s.steps.map((st) => `${st.kind}: ${st.substeps.join(", ")}`).join("\n")}</pre>}>
                  <span>{s.name}</span>
                </Tooltip>
              </TableCell>
              <TableCell>
                <div css={{ width: "100%" }}>
                  {(["avg", "max"] as const).map((agg) => (
                    <div key={agg} css={{ display: "flex", height: agg === "avg" ? 5 : 10, marginBottom: 2 }}>
                      {PARTS.map(([k, n, c]) => {
                        const v = s[`${String(k)}_${agg}` as keyof BqStage] as number;
                        return v ? <div key={n} title={`${n} (${agg === "avg" ? "average" : "slowest"}): ${secondsText(v / 1000)}`}
                                        css={{ width: `${(v / total(s)) * 100}%`, background: c }} /> : null;
                      })}
                    </div>))}
                  <Typography.Text color="secondary" size="sm">{secondsText((s.end_ms - s.start_ms) / 1000)} · {s.parallel_inputs} inputs</Typography.Text>
                </div>
              </TableCell>
              <TableCell css={{ justifyContent: "flex-end" }}>{s.records_read.toLocaleString()}</TableCell>
              <TableCell css={{ justifyContent: "flex-end" }}>{s.records_written.toLocaleString()}</TableCell>
              <TableCell css={{ justifyContent: "flex-end" }}>{bytesText(s.shuffle_bytes)}</TableCell>
              <TableCell css={{ justifyContent: "flex-end" }}>{s.spilled_bytes ? <Typography.Text color="error">{bytesText(s.spilled_bytes)}</Typography.Text> : "-"}</TableCell>
              <TableCell css={{ justifyContent: "flex-end" }}>{slot(s.slot_ms)}</TableCell>
            </TableRow>))}
        </Table>
        {d.query && <><Spacer size="sm" /><Typography.Title level={5}>SQL</Typography.Title>
          <pre css={{ maxHeight: 280, overflow: "auto", background: theme.colors.backgroundSecondary, padding: theme.spacing.sm,
                      fontSize: 12, borderRadius: theme.borders.borderRadiusMd }}>{d.query}</pre></>}
      </>}
    </Modal>
  );
}

/** A task run's Metrics, as Databricks' Spark UI and query profile show a run: what each
 *  execution used and cost, its BigQuery jobs, and what stands out. */
export default function TaskMetrics({ wf, id, task, running }: { wf: string; id: string; task: string; running: boolean }) {
  const { theme } = useDesignSystemTheme();
  const s = useLoad(() => api.taskStats(wf, id, task), [wf, id, task], running ? 60000 : 0);
  const [unit, setUnit] = useState<string | null>(null);
  const [job, setJob] = useState<{ project: string; location: string; job_id: string } | null>(null);
  const d = s.data;
  if (s.error) return <Alert componentId="metrics.err" type="warning" message="Couldn't load this task's metrics" description={s.error} closable={false} />;
  if (!d) return <TableSkeleton lines={4} />;
  const shown = d.units.find((u) => u.id === unit) ?? d.units[0];
  const peakMem = Math.max(-1, ...d.units.map((u) => u.usage.memory_peak ?? -1));
  const cpu = d.units.map((u) => u.usage.cpu_mean).filter((v): v is number => v != null);
  const bq = d.bigquery.totals;
  const steps = d.kind === "vertex_pipeline_task";
  return (
    <>
      <div css={{ display: "flex", flexWrap: "wrap", gap: theme.spacing.sm }}>
        <Tile name="Duration" value={d.totals.seconds == null ? "-" : secondsText(d.totals.seconds)} />
        <Tile name="Compute (est.)" value={usd(d.totals.compute_usd)}
              hint={steps ? `${d.units.reduce((a, u) => a + (u.usage.machine_hours ?? 0), 0).toFixed(2)} machine-h` : d.units.some((u) => u.usage.estimated) ? "from limits" : undefined} />
        <Tile name="BigQuery (est.)" value={usd(bq.cost_usd)} hint={`${bq.jobs} jobs · ${bytesText(bq.bytes_billed)} billed`} />
        <Tile name="Slot time" value={slot(bq.slot_ms)} />
        <Tile name="Peak memory" value={peakMem < 0 ? "-" : pct(peakMem)} hint="of its limit" />
        <Tile name="Avg CPU" value={cpu.length ? pct(cpu.reduce((a, b) => a + b, 0) / cpu.length) : "-"} />
      </div>
      <Spacer size="sm" />
      {d.insights.map((i, n) => <Alert key={n} componentId="metrics.insight" type={i.level} message={i.text} closable={false} css={{ marginBottom: 8 }} />)}

      {d.units.length > 0 && <>
        <Typography.Title level={5}>{steps ? "Steps" : d.kind === "batch_serve_task" ? "Models" : "Executions"}</Typography.Title>
        <Table scrollable>
          <TableRow isHeader>
            <TableHeader componentId="metrics.u.h.name">{steps ? "Step" : d.kind === "batch_serve_task" ? "Model" : "Execution"}</TableHeader>
            <TableHeader componentId="metrics.u.h.machine">Machine</TableHeader>
            <TableHeader componentId="metrics.u.h.dur" align="right">Duration</TableHeader>
            <TableHeader componentId="metrics.u.h.cpu" align="right">CPU avg / peak</TableHeader>
            <TableHeader componentId="metrics.u.h.mem" align="right">Memory peak</TableHeader>
            <TableHeader componentId="metrics.u.h.net" align="right">Network in</TableHeader>
            <TableHeader componentId="metrics.u.h.cost" align="right">{steps ? "Machine-h" : "Est. cost"}</TableHeader>
          </TableRow>
          {d.units.map((u) => (
            <TableRow key={u.id} css={{ background: shown?.id === u.id ? theme.colors.actionDefaultBackgroundHover : undefined }}>
              <TableCell><Typography.Link componentId="metrics.u.pick" onClick={() => setUnit(u.id)}>{u.label}</Typography.Link>
                &nbsp;<a href={u.link} target="_blank" rel="noreferrer" css={{ fontSize: 12 }}>GCP</a></TableCell>
              <TableCell>{machineText(u)}</TableCell>
              <TableCell css={{ justifyContent: "flex-end", flexDirection: "column", alignItems: "flex-end" }}>
                {u.start ? duration(u.start, u.end) : u.created ? "Waiting" : "-"}
                {u.created && (u.start ? Date.parse(u.start) : Date.now()) - Date.parse(u.created) > 30_000 && (
                  <Typography.Text color="secondary" size="sm"
                                   title="From the job's creation to its start: Vertex provisioning machines, not billed">
                    + {duration(u.created, u.start ?? undefined)} waiting for machines</Typography.Text>)}
              </TableCell>
              <TableCell css={{ justifyContent: "flex-end" }}>{pct(u.usage.cpu_mean)} / {pct(u.usage.cpu_peak)}</TableCell>
              <TableCell css={{ justifyContent: "flex-end" }}>
                {pct(u.usage.memory_peak)}{u.usage.memory_peak_bytes ? ` · ${bytesText(u.usage.memory_peak_bytes)}` : ""}</TableCell>
              <TableCell css={{ justifyContent: "flex-end" }}>{bytesText(u.usage.net_in_bytes)}</TableCell>
              <TableCell css={{ justifyContent: "flex-end" }}>{steps ? (u.usage.machine_hours ?? 0).toFixed(3) : usd(u.cost_usd)}</TableCell>
            </TableRow>))}
        </Table>
        {shown && <><Spacer size="sm" /><Typography.Text color="secondary">{shown.label}</Typography.Text><UnitCharts u={shown} /></>}
        <Spacer size="md" />
      </>}

      {d.dbt.length > 0 ? <>
        <Typography.Title level={5}>dbt nodes</Typography.Title>
        <DbtModels rows={d.dbt} open={(r) => setJob({ project: r.project!, location: r.location!, job_id: r.job_id! })} />
      </> : d.bigquery.jobs.length > 0 && <>
        <Typography.Title level={5}>BigQuery jobs</Typography.Title>
        <Table scrollable>
          <TableRow isHeader>
            <TableHeader componentId="metrics.bq.h.job">Job</TableHeader>
            <TableHeader componentId="metrics.bq.h.dest">Writes</TableHeader>
            <TableHeader componentId="metrics.bq.h.billed" align="right">Bytes billed</TableHeader>
            <TableHeader componentId="metrics.bq.h.slots" align="right">Slot time</TableHeader>
            <TableHeader componentId="metrics.bq.h.cost" align="right">Est. cost</TableHeader>
          </TableRow>
          {d.bigquery.jobs.map((j) => (
            <TableRow key={j.job_id}>
              <TableCell><Typography.Link componentId="metrics.bq.open" onClick={() => setJob(j)}>{j.statement ?? j.type}</Typography.Link>
                &nbsp;<Typography.Text color="secondary" size="sm">{j.job_id.slice(0, 18)}</Typography.Text>
                {j.error && <Tag componentId="metrics.bq.err" color="coral">failed</Tag>}</TableCell>
              <TableCell>{j.destination?.split(".").slice(1).join(".") ?? "-"}</TableCell>
              <TableCell css={{ justifyContent: "flex-end" }}>{bytesText(j.bytes_billed)}</TableCell>
              <TableCell css={{ justifyContent: "flex-end" }}>{slot(j.slot_ms)}</TableCell>
              <TableCell css={{ justifyContent: "flex-end" }}>{usd(j.cost_usd)}</TableCell>
            </TableRow>))}
        </Table>
      </>}
      {d.kind === "dbt_task" && d.dbt.length === 0 && (
        <Typography.Text color="secondary">No dbt run results for this run: runs from before the platform kept them have none.</Typography.Text>)}
      <Spacer size="xs" />
      <Typography.Text color="secondary" size="sm">
        Estimates at list price: BigQuery on-demand ${d.prices.bq_usd_per_tib}/TiB billed, Cloud Run ${d.prices.run_usd_per_vcpu_s}/vCPU-s
        and ${d.prices.run_usd_per_gib_s}/GiB-s. Vertex AI training is shown in machine-hours.
      </Typography.Text>
      <QueryProfile job={job} onClose={() => setJob(null)} />
    </>
  );
}
