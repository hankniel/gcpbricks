import { useState } from "react";
import {
  Alert, DollarIcon, Empty, SegmentedControlButton, SegmentedControlGroup, Spacer, TableCell, TableRow, TableSkeleton, Tag,
  Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { api, type CostItem, type CostSource } from "../api";
import Chart from "../components/Chart";
import PageTitle from "../components/PageTitle";
import { Table, TableHeader } from "../components/Table";
import { Tile, usd } from "../components/TaskMetrics";
import { ago, useLoad, when } from "../util";

const SOURCES: { key: CostSource; label: string }[] = [
  { key: "bigquery", label: "BigQuery" }, { key: "cloud_run", label: "Cloud Run" },
  { key: "vertex", label: "Vertex AI" }, { key: "notebooks", label: "Notebooks" },
];
const LABEL = Object.fromEntries(SOURCES.map((s) => [s.key, s.label])) as Record<CostSource, string>;
const WINDOWS = [7, 30, 90];

/** What an item's detail column says: its owner, machine, jobs or minimum instances, whichever it has. */
function detail(i: CostItem): string {
  return [i.owner, i.machine, i.usd_per_hour != null ? `${usd(i.usd_per_hour)}/h` : null,
          i.disk_usd ? `disk ${usd(i.disk_usd)}` : null,
          i.jobs != null ? `${i.jobs} jobs, ${(i.tib_billed ?? 0).toFixed(3)} TiB billed` : null,
          i.min_instances ? `${i.min_instances} min instance(s)` : null, i.min_replicas ? `${i.min_replicas} min replica(s)` : null,
          i.unpriced ? `not priced: ${i.unpriced}` : null].filter(Boolean).join(" · ");
}

/** The Costs page: list-price estimates of the project's BigQuery, Cloud Run, Vertex AI and notebook runtimes,
 *  per day and per service, and what bills right now. */
export default function CostsPage() {
  const { theme } = useDesignSystemTheme();
  const [days, setDays] = useState(30);
  const [only, setOnly] = useState<CostSource | null>(null);
  const { data, error } = useLoad(() => api.costs(days), [days]);
  const t = data?.totals ?? {};
  const items = (data?.items ?? []).filter((i) => (!only || i.source === only) && (i.usd > 0.005 || (i.usd_upper ?? 0) > 0.005 || i.unpriced));
  const burn = (data?.running ?? []).reduce((a, r) => a + (r.usd_per_hour || 0), 0);
  return (
    <>
      <PageTitle title="Costs" icon={<DollarIcon />} actions={
        <SegmentedControlGroup componentId="costs.window" name="window" value={String(days)} onChange={(e) => setDays(Number(e.target.value))}>
          {WINDOWS.map((w) => <SegmentedControlButton key={w} value={String(w)}>Last {w} days</SegmentedControlButton>)}
        </SegmentedControlGroup>} />
      <Spacer size="sm" />
      <Typography.Text color="secondary">
        Estimates at list price, before discounts, credits and committed use: what GCP records of each service's use, priced
        from {data?.price_source ?? "the Cloud Billing Catalog"}. The billing account itself isn't read.
      </Typography.Text>
      <Spacer size="md" />
      {error && <Alert componentId="costs.error" type="error" message="Couldn't load costs" description={error} closable={false} />}
      {data && Object.entries(data.errors).map(([s, e]) => (
        <div key={s} css={{ marginBottom: theme.spacing.sm }}>
          <Alert componentId="costs.source.error" type="warning" closable={false}
                 message={`${LABEL[s as CostSource]} left out`} description={e} />
        </div>))}
      {!data && !error && <TableSkeleton lines={10} />}
      {data && (
        <>
          <div css={{ display: "flex", gap: theme.spacing.sm, flexWrap: "wrap" }}>
            <Tile name={`Total, ${days} days`} value={usd(t.all)}
                  hint={t.notebooks_upper != null ? `up to ${usd((t.all ?? 0) - (t.notebooks ?? 0) + t.notebooks_upper)}` : undefined} />
            {SOURCES.filter((s) => s.key in t).map((s) => (
              <Tile key={s.key} name={s.label} value={s.key === "notebooks" ? `≥ ${usd(t.notebooks)}` : usd(t[s.key])}
                    hint={s.key === "notebooks" && t.notebooks_upper != null ? `at most ${usd(t.notebooks_upper)}` : undefined} />))}
            <Tile name="Billing right now" value={`${usd(burn)}/h`} hint={`${usd(burn * 24)}/day if it keeps running`} />
          </div>
          <Spacer size="lg" />
          <Typography.Title level={4}>By day</Typography.Title>
          <Chart kind="bar" stacked height={240} xs={data.days.map((d) => d.day.slice(5))}
                 series={SOURCES.filter((s) => s.key in t).map((s) => ({
                   name: s.label, points: data.days.map((d) => ({ x: d.day.slice(5), y: d[s.key] ?? null })) }))} />
          <Spacer size="lg" />
          <Typography.Title level={4}>Running now</Typography.Title>
          <Table empty={data.running.length ? undefined : <Empty title="Nothing bills by the hour right now" description="Runtimes, always-on services, endpoints and running jobs show here." />}>
            <TableRow isHeader>
              <TableHeader componentId="costs.run.source" css={{ flex: "0 0 120px" }}>Source</TableHeader>
              <TableHeader componentId="costs.run.name">Name</TableHeader>
              <TableHeader componentId="costs.run.rate" align="right" css={{ flex: "0 0 110px" }}>Per hour</TableHeader>
              <TableHeader componentId="costs.run.since" css={{ flex: "0 0 150px" }}>Since</TableHeader>
              <TableHeader componentId="costs.run.detail">Detail</TableHeader>
            </TableRow>
            {data.running.map((r) => (
              <TableRow key={`${r.source}/${r.name}`}>
                <TableCell css={{ flex: "0 0 120px" }}><Tag componentId="costs.run.tag">{LABEL[r.source]}</Tag></TableCell>
                <TableCell>{r.name}<Typography.Text color="secondary">&nbsp;· {r.kind}</Typography.Text></TableCell>
                <TableCell css={{ flex: "0 0 110px", justifyContent: "flex-end" }}>{usd(r.usd_per_hour)}</TableCell>
                <TableCell css={{ flex: "0 0 150px" }}><span title={when(r.since, true)}>{r.since ? ago(r.since) : "-"}</span></TableCell>
                <TableCell>{[r.owner, r.detail].filter(Boolean).join(" · ")}</TableCell>
              </TableRow>))}
          </Table>
          <Spacer size="lg" />
          <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: theme.spacing.sm }}>
            <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>By service</Typography.Title>
            <SegmentedControlGroup componentId="costs.only" name="only" value={only ?? "all"}
                                   onChange={(e) => setOnly(e.target.value === "all" ? null : e.target.value as CostSource)}>
              <SegmentedControlButton value="all">All</SegmentedControlButton>
              {SOURCES.filter((s) => s.key in t).map((s) => <SegmentedControlButton key={s.key} value={s.key}>{s.label}</SegmentedControlButton>)}
            </SegmentedControlGroup>
          </div>
          <Spacer size="sm" />
          <Table scrollable empty={items.length ? undefined : <Empty title="No costs in this window" description={only ? "Try All sources." : "Nothing used billable compute or queries."} />}>
            <TableRow isHeader>
              <TableHeader componentId="costs.h.source" css={{ flex: "0 0 120px" }}>Source</TableHeader>
              <TableHeader componentId="costs.h.name">Name</TableHeader>
              <TableHeader componentId="costs.h.kind" css={{ flex: "0 0 130px" }}>Kind</TableHeader>
              <TableHeader componentId="costs.h.usd" align="right" css={{ flex: "0 0 150px" }}>Cost</TableHeader>
              <TableHeader componentId="costs.h.detail">Detail</TableHeader>
            </TableRow>
            {items.map((i) => (
              <TableRow key={`${i.source}/${i.kind}/${i.name}`}>
                <TableCell css={{ flex: "0 0 120px" }}><Tag componentId="costs.tag">{LABEL[i.source]}</Tag></TableCell>
                <TableCell>{i.name}</TableCell>
                <TableCell css={{ flex: "0 0 130px" }}>{i.kind}</TableCell>
                <TableCell css={{ flex: "0 0 150px", justifyContent: "flex-end" }}>
                  <span title={i.usd_upper != null && i.usd_upper > i.usd + 0.005 ? "Colab's idle shutdown isn't logged: at least the first, at most the second" : undefined}>
                    {usd(i.usd)}{i.usd_upper != null && i.usd_upper > i.usd + 0.005 ? ` – ${usd(i.usd_upper)}` : ""}</span></TableCell>
                <TableCell>{detail(i)}</TableCell>
              </TableRow>))}
          </Table>
          <Spacer size="lg" />
          <details>
            <summary css={{ cursor: "pointer", color: theme.colors.textSecondary }}>How these are estimated, and the prices used</summary>
            <ul>{data.notes.map((n) => <li key={n}><Typography.Text color="secondary">{n}</Typography.Text></li>)}</ul>
            <Table>
              <TableRow isHeader>
                <TableHeader componentId="costs.p.sku" css={{ flex: "0 0 160px" }}>SKU</TableHeader>
                <TableHeader componentId="costs.p.desc">Description</TableHeader>
                <TableHeader componentId="costs.p.usd" align="right" css={{ flex: "0 0 180px" }}>List price</TableHeader>
              </TableRow>
              {data.prices.map((p) => (
                <TableRow key={p.sku}>
                  <TableCell css={{ flex: "0 0 160px" }}><code>{p.sku}</code></TableCell>
                  <TableCell>{p.description}</TableCell>
                  <TableCell css={{ flex: "0 0 180px", justifyContent: "flex-end" }}>${p.usd} / {p.unit}</TableCell>
                </TableRow>))}
            </Table>
          </details>
        </>
      )}
    </>
  );
}
