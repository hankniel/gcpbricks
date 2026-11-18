import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Alert, ChevronDownIcon, ChevronRightIcon, Empty, SegmentedControlButton, SegmentedControlGroup, Spacer, TableCell, TableRow,
  TableSkeleton, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { api, type BqOwner, type BqReachedTable, type CostTop } from "../api";
import Chart from "./Chart";
import { Table, TableHeader } from "./Table";
import { Tile, usd } from "./TaskMetrics";
import { useLoad } from "../util";

/** A table's Catalog page; another project's opens with ?project=. */
export function TableLink({ fqn }: { fqn: string }) {
  const [project, dataset, table] = fqn.split(".");
  return <Link to={`/catalog/${dataset}/${table}?project=${project}`} title={fqn}>{dataset}.{table}</Link>;
}

const tib = (v: number) => `${v.toFixed(3)} TiB`;
const tops = (by: CostTop[]) => by.map((b) => `${b.name} ${usd(b.usd)}`).join(" · ");
/** A dbt node id's last part: model.<project>.<name>. */
const short = (s: string) => s.startsWith("model.") || s.startsWith("snapshot.") || s.startsWith("seed.") ? s.split(".").slice(2).join(".") : s;

type Node = { key: string; name: string; depth: number; usd: number; tib: number; jobs: number; workflow: string | null;
              children: Node[]; leaf?: BqOwner };

/** The owners' paths as a tree, each level summed. */
function tree(owners: BqOwner[]): Node[] {
  const root: Node = { key: "", name: "", depth: -1, usd: 0, tib: 0, jobs: 0, workflow: null, children: [] };
  for (const o of owners) {
    let at = root;
    o.path.forEach((name, i) => {
      const key = o.path.slice(0, i + 1).join("\u0000");
      let next = at.children.find((c) => c.key === key);
      if (!next) {
        next = { key, name, depth: i, usd: 0, tib: 0, jobs: 0, workflow: null, children: [] };
        at.children.push(next);
      }
      next.usd += o.usd; next.tib += o.tib; next.jobs += o.jobs;
      // The job level of a platform path opens its Job page.
      if (o.kind === "platform" && i === 2 && o.workflow) next.workflow = o.workflow;
      if (i === o.path.length - 1) next.leaf = o;
      at = next;
    });
  }
  const sort = (n: Node) => { n.children.sort((a, b) => b.usd - a.usd); n.children.forEach(sort); };
  sort(root);
  return root.children;
}

function OwnerTree({ owners, total }: { owners: BqOwner[]; total: number }) {
  const { theme } = useDesignSystemTheme();
  const nodes = useMemo(() => tree(owners), [owners]);
  const [open, setOpen] = useState<Set<string>>(() => new Set(nodes.slice(0, 1).map((n) => n.key)));
  const toggle = (k: string) => setOpen((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; });
  const rows: Node[] = [];
  const walk = (ns: Node[]) => ns.forEach((n) => { rows.push(n); if (open.has(n.key)) walk(n.children); });
  walk(nodes);
  return (
    <Table scrollable empty={rows.length ? undefined : <Empty title="No query jobs in this window" description="" />}>
      <TableRow isHeader>
        <TableHeader componentId="bq.owner.name">What ran it</TableHeader>
        <TableHeader componentId="bq.owner.usd" align="right" css={{ flex: "0 0 110px" }}>Cost</TableHeader>
        <TableHeader componentId="bq.owner.share" css={{ flex: "0 0 140px" }}>Share</TableHeader>
        <TableHeader componentId="bq.owner.tib" align="right" css={{ flex: "0 0 120px" }}>Billed</TableHeader>
        <TableHeader componentId="bq.owner.jobs" align="right" css={{ flex: "0 0 80px" }}>Jobs</TableHeader>
      </TableRow>
      {rows.map((n) => (
        <TableRow key={n.key}>
          <TableCell>
            <span css={{ display: "inline-flex", alignItems: "center", gap: theme.spacing.xs, paddingLeft: n.depth * 20, minWidth: 0 }}>
              {n.children.length
                ? <span role="button" tabIndex={0} onClick={() => toggle(n.key)} onKeyDown={(e) => e.key === "Enter" && toggle(n.key)}
                        css={{ cursor: "pointer", display: "inline-flex" }}>{open.has(n.key) ? <ChevronDownIcon /> : <ChevronRightIcon />}</span>
                : <span css={{ width: 16 }} />}
              {n.workflow ? <Link to={`/jobs/${n.workflow}`}>{n.name}</Link>
                : <Typography.Text ellipsis title={n.name}>{short(n.name)}</Typography.Text>}
              {n.leaf && n.leaf.tables.length > 0 && <Typography.Text color="secondary" size="sm" ellipsis>
                &nbsp;→ {n.leaf.tables.length === 1 ? n.leaf.tables[0].split(".").slice(1).join(".") : `${n.leaf.tables.length} tables`}</Typography.Text>}
            </span>
          </TableCell>
          <TableCell css={{ flex: "0 0 110px", justifyContent: "flex-end" }}>{usd(n.usd)}</TableCell>
          <TableCell css={{ flex: "0 0 140px" }}>
            <div css={{ width: 120, height: 8, background: theme.colors.backgroundSecondary, borderRadius: 4 }}>
              <div css={{ width: `${total ? Math.min(100, (n.usd / total) * 100) : 0}%`, height: 8, borderRadius: 4,
                          background: theme.colors.actionPrimaryBackgroundDefault }} />
            </div>
          </TableCell>
          <TableCell css={{ flex: "0 0 120px", justifyContent: "flex-end" }}>{tib(n.tib)}</TableCell>
          <TableCell css={{ flex: "0 0 80px", justifyContent: "flex-end" }}>{n.jobs.toLocaleString()}</TableCell>
        </TableRow>))}
    </Table>
  );
}

function Reached({ tables }: { tables: BqReachedTable[] }) {
  return (
    <Table scrollable>
      <TableRow isHeader>
        <TableHeader componentId="bq.up.table">Table</TableHeader>
        <TableHeader componentId="bq.up.depth" align="right" css={{ flex: "0 0 90px" }}>Hops up</TableHeader>
        <TableHeader componentId="bq.up.usd" align="right" css={{ flex: "0 0 110px" }}>Build cost</TableHeader>
      </TableRow>
      {tables.map((t) => (
        <TableRow key={t.table}>
          <TableCell><TableLink fqn={t.table} /></TableCell>
          <TableCell css={{ flex: "0 0 90px", justifyContent: "flex-end" }}>{t.depth}</TableCell>
          <TableCell css={{ flex: "0 0 110px", justifyContent: "flex-end" }}>{t.built === false ? "not built" : usd(t.usd)}</TableCell>
        </TableRow>))}
    </Table>
  );
}

type View = "owners" | "built" | "read" | "models";

/** The Costs page's BigQuery section: the window's query cost by what ran it, by table built and
 *  read, and each model's upstream data. */
export function BigQueryBreakdown({ days }: { days: number }) {
  const { theme } = useDesignSystemTheme();
  const [view, setView] = useState<View>("owners");
  const [model, setModel] = useState<string | null>(null);
  const { data, error } = useLoad(() => api.costsBigquery(days), [days]);
  if (error) return <Alert componentId="bq.error" type="error" message="Couldn't break BigQuery down" description={error} closable={false} />;
  if (!data) return <TableSkeleton lines={8} />;
  return (
    <>
      <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: theme.spacing.sm, flexWrap: "wrap" }}>
        <Typography.Text color="secondary">
          {data.scope.text} {usd(data.total_usd)} at {usd(data.usd_per_tib)}/TiB.
        </Typography.Text>
        <SegmentedControlGroup componentId="bq.view" name="bq-view" value={view} onChange={(e) => setView(e.target.value as View)}>
          <SegmentedControlButton value="owners">By what ran it</SegmentedControlButton>
          <SegmentedControlButton value="built">Tables built</SegmentedControlButton>
          <SegmentedControlButton value="read">Tables read</SegmentedControlButton>
          <SegmentedControlButton value="models">Models</SegmentedControlButton>
        </SegmentedControlGroup>
      </div>
      {data.scope.kind === "user" && <><Spacer size="sm" /><Alert componentId="bq.scope" type="warning" closable={false}
        message="Other users' jobs are left out" description={data.scope.text} /></>}
      <Spacer size="sm" />
      {view === "owners" && <OwnerTree owners={data.owners} total={data.total_usd} />}
      {view === "built" && (
        <>
          <Typography.Text color="secondary" size="sm">
            A job's whole cost goes to the table it wrote; {usd(data.no_table_usd)} went to jobs that wrote no table (SELECTs, scripts' temporaries).
          </Typography.Text>
          <Table scrollable empty={data.built.length ? undefined : <Empty title="No table was written" description="" />}>
            <TableRow isHeader>
              <TableHeader componentId="bq.b.table">Table</TableHeader>
              <TableHeader componentId="bq.b.usd" align="right" css={{ flex: "0 0 110px" }}>Build cost</TableHeader>
              <TableHeader componentId="bq.b.tib" align="right" css={{ flex: "0 0 110px" }}>Billed</TableHeader>
              <TableHeader componentId="bq.b.jobs" align="right" css={{ flex: "0 0 70px" }}>Jobs</TableHeader>
              <TableHeader componentId="bq.b.by">Built by</TableHeader>
            </TableRow>
            {data.built.map((b) => (
              <TableRow key={b.table}>
                <TableCell><TableLink fqn={b.table} /></TableCell>
                <TableCell css={{ flex: "0 0 110px", justifyContent: "flex-end" }}>{usd(b.usd)}</TableCell>
                <TableCell css={{ flex: "0 0 110px", justifyContent: "flex-end" }}>{tib(b.tib)}</TableCell>
                <TableCell css={{ flex: "0 0 70px", justifyContent: "flex-end" }}>{b.jobs}</TableCell>
                <TableCell><Typography.Text color="secondary" size="sm" ellipsis title={tops(b.by)}>{tops(b.by)}</Typography.Text></TableCell>
              </TableRow>))}
          </Table>
        </>)}
      {view === "read" && (
        <>
          <Typography.Text color="secondary" size="sm">
            BigQuery doesn't bill a job by the tables it read, so a table's figure is the whole cost of the jobs that read it.
            A job reading several tables counts in each: these don't add up.
          </Typography.Text>
          <Table scrollable>
            <TableRow isHeader>
              <TableHeader componentId="bq.r.table">Table</TableHeader>
              <TableHeader componentId="bq.r.usd" align="right" css={{ flex: "0 0 140px" }}>Jobs reading it</TableHeader>
              <TableHeader componentId="bq.r.jobs" align="right" css={{ flex: "0 0 70px" }}>Jobs</TableHeader>
              <TableHeader componentId="bq.r.by">Read by</TableHeader>
            </TableRow>
            {data.read.map((x) => (
              <TableRow key={x.table}>
                <TableCell><TableLink fqn={x.table} /></TableCell>
                <TableCell css={{ flex: "0 0 140px", justifyContent: "flex-end" }}>{usd(x.jobs_usd)}</TableCell>
                <TableCell css={{ flex: "0 0 70px", justifyContent: "flex-end" }}>{x.jobs}</TableCell>
                <TableCell><Typography.Text color="secondary" size="sm" ellipsis title={tops(x.by)}>{tops(x.by)}</Typography.Text></TableCell>
              </TableRow>))}
          </Table>
        </>)}
      {view === "models" && (
        <>
          <Typography.Text color="secondary" size="sm">
            What building each serving version's feature and label tables cost, with everything upstream of them, followed
            back through the jobs that wrote each table. A table several models share counts in each.
          </Typography.Text>
          {data.models_error && <Alert componentId="bq.models.error" type="warning" closable={false} message="Models left out" description={data.models_error} />}
          <Table scrollable empty={data.models.length ? undefined : <Empty title="No model names its training tables" description="A version's train_window.json names them." />}>
            <TableRow isHeader>
              <TableHeader componentId="bq.m.model">Model</TableHeader>
              <TableHeader componentId="bq.m.usd" align="right" css={{ flex: "0 0 120px" }}>Data cost</TableHeader>
              <TableHeader componentId="bq.m.tables" align="right" css={{ flex: "0 0 90px" }}>Tables</TableHeader>
              <TableHeader componentId="bq.m.roots">Trains on</TableHeader>
            </TableRow>
            {data.models.map((m) => (
              <TableRow key={m.model} onClick={() => setModel(model === m.model ? null : m.model)} css={{ cursor: "pointer" }}>
                <TableCell><Link to={`/models/${m.model}/versions/${m.version}`} onClick={(e) => e.stopPropagation()}>{m.name} v{m.version}</Link></TableCell>
                <TableCell css={{ flex: "0 0 120px", justifyContent: "flex-end" }}>{usd(m.usd)}</TableCell>
                <TableCell css={{ flex: "0 0 90px", justifyContent: "flex-end" }}>{m.tables.length}</TableCell>
                <TableCell><Typography.Text color="secondary" size="sm" ellipsis>
                  {m.roots.map((r) => r.split(".").slice(1).join(".")).join(", ")}
                  {m.unresolved.length > 0 && ` · upstream unknown for ${m.unresolved.length}`}</Typography.Text></TableCell>
              </TableRow>))}
          </Table>
          {model && (() => {
            const m = data.models.find((x) => x.model === model)!;
            return (
              <>
                <Spacer size="md" />
                <Typography.Title level={5}>{m.name} v{m.version}: its tables and everything upstream</Typography.Title>
                {m.unresolved.length > 0 && <><Alert componentId="bq.m.unresolved" type="info" closable={false}
                  message="Upstream not followed"
                  description={`No job in this window wrote ${m.unresolved.join(", ")}, so its sources can't be read from jobs: a view, or loaded outside BigQuery's query jobs.`} /><Spacer size="sm" /></>}
                <Reached tables={m.tables} />
              </>);
          })()}
        </>)}
    </>
  );
}

const WINDOWS = [7, 30, 90];

/** A Catalog table's Cost tab: what building it cost, by day and by whom, who read it, and
 *  what everything upstream of it cost to build. */
export function TableCostTab({ fullName }: { fullName: string }) {
  const { theme } = useDesignSystemTheme();
  const [days, setDays] = useState(30);
  const { data, error } = useLoad(() => api.tableCost(fullName, days), [fullName, days]);
  return (
    <>
      <Spacer size="sm" />
      <div css={{ display: "flex", justifyContent: "flex-end" }}>
        <SegmentedControlGroup componentId="tcost.window" name="tcost-window" value={String(days)} onChange={(e) => setDays(Number(e.target.value))}>
          {WINDOWS.map((w) => <SegmentedControlButton key={w} value={String(w)}>Last {w} days</SegmentedControlButton>)}
        </SegmentedControlGroup>
      </div>
      <Spacer size="sm" />
      {error && <Alert componentId="tcost.error" type="error" message="Couldn't load this table's cost" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={6} />}
      {data && (
        <>
          <Typography.Text color="secondary">{data.scope.text} At list price, before discounts.</Typography.Text>
          <Spacer size="sm" />
          <div css={{ display: "flex", gap: theme.spacing.sm, flexWrap: "wrap" }}>
            <Tile name="To build it" value={usd(data.build?.usd ?? 0)} hint={data.build ? `${data.build.jobs} jobs, ${tib(data.build.tib)}` : "no job wrote it"} />
            <Tile name="Upstream, to build" value={usd(data.upstream_usd)} hint={`${data.upstream.length} tables upstream`} />
            <Tile name="Jobs that read it" value={usd(data.read?.jobs_usd ?? 0)} hint={data.read ? `${data.read.jobs} jobs, whole cost` : "no job read it"} />
          </div>
          {data.build && (
            <>
              <Spacer size="md" />
              <Typography.Title level={5}>Build cost by day</Typography.Title>
              <Chart kind="bar" height={180} xs={data.build.by_day.map((d) => d.day.slice(5))}
                     series={[{ name: "Build", points: data.build.by_day.map((d) => ({ x: d.day.slice(5), y: d.usd })) }]} />
            </>)}
          <Spacer size="md" />
          <div css={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))", gap: theme.spacing.md }}>
            <Section title="Built by" empty="No job in this window wrote this table.">
              {data.build && <Costs rows={data.build.by} what="Built by" />}
            </Section>
            <Section title="Read by" note="Each reader's whole job cost, which may read other tables too." empty="No job in this window read this table.">
              {data.read && <Costs rows={data.read.by} what="Read by" />}
            </Section>
            <Section title={`Upstream${data.upstream.length ? ` · ${usd(data.upstream_usd)} to build` : ""}`}
                     empty="No job in this window wrote this table from another.">
              {data.upstream.length > 0 && <Reached tables={data.upstream} />}
            </Section>
            <Section title="Feeds" empty="No job in this window wrote another table from this one.">
              {data.feeds.length > 0 && (
                <Table scrollable>
                  <TableRow isHeader><TableHeader componentId="tcost.feeds.h">Table</TableHeader></TableRow>
                  {data.feeds.map((t) => <TableRow key={t}><TableCell><TableLink fqn={t} /></TableCell></TableRow>)}
                </Table>)}
            </Section>
          </div>
        </>)}
    </>
  );
}

/** A titled block of the Cost tab; `empty` when it has nothing to show. */
function Section({ title, note, empty, children }: { title: string; note?: string; empty: string; children: React.ReactNode }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div css={{ minWidth: 0, border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd, padding: theme.spacing.md }}>
      <Typography.Title level={5} css={{ "&&": { marginTop: 0 } }}>{title}</Typography.Title>
      {note && <Typography.Text color="secondary" size="sm">{note}</Typography.Text>}
      {children || <Typography.Text color="secondary">{empty}</Typography.Text>}
    </div>
  );
}

/** Who built or read a table, and what their jobs cost: bundle / job / task / model. */
function Costs({ rows, what }: { rows: CostTop[]; what: string }) {
  return (
    <Table scrollable>
      <TableRow isHeader>
        <TableHeader componentId="tcost.who">{what}</TableHeader>
        <TableHeader componentId="tcost.usd" align="right" css={{ flex: "0 0 100px" }}>Cost</TableHeader>
      </TableRow>
      {rows.map((r) => {
        const name = r.name.split(" / ").map(short).join(" / ");
        return (
          <TableRow key={r.name}>
            <TableCell><Typography.Text ellipsis title={r.name}>{name}</Typography.Text></TableCell>
            <TableCell css={{ flex: "0 0 100px", justifyContent: "flex-end" }}>{usd(r.usd)}</TableCell>
          </TableRow>);
      })}
    </Table>
  );
}
