import { Fragment, useEffect, useState } from "react";
import { Link, Outlet, useParams, useSearchParams } from "react-router-dom";
import {
  Alert, Breadcrumb, Button, CatalogIcon, ChevronDownIcon, ChevronRightIcon, CopyIcon, DropdownMenu, Empty,
  Input, NewWindowIcon, OverflowIcon, PlayIcon, RefreshIcon, SchemaIcon, SearchIcon, Spacer, Spinner,
  TableCell, TableRow, TableSkeleton, Tabs, Tag, Tooltip, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../../components/Table";
import { api, Column, Dataset, DatasetDetail, Sample, TableDetail } from "../../api";
import { ColumnTypeIcon, IconTile, OBJECT_TYPE, ObjectIcon, typeText } from "../../components/LineageGraph";
import LineageTab from "../../components/LineageTab";
import SampleGrid from "../../components/SampleGrid";
import { useLoad, when } from "../../util";

export function bytesText(b: number | null): string {
  if (b == null) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = b;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${i ? v.toFixed(1) : v} ${units[i]}`;
}

function bqConsole(project: string, dataset?: string, table?: string): string {
  const page = table ? `&p=${project}&d=${dataset}&t=${table}&page=table` : dataset ? `&p=${project}&d=${dataset}&page=dataset` : "";
  return `https://console.cloud.google.com/bigquery?project=${project}${page}`;
}

// --- the explorer's left pane --------------------------------------------------------------

type TreeState = { open: Record<string, boolean>; tables: Record<string, DatasetDetail | "loading"> };

function TreeRow({ depth, open, onToggle, icon, label, active, to, count }: {
  depth: number; open?: boolean; onToggle?: () => void; icon?: React.ReactNode; label: string; active?: boolean;
  to?: string; count?: number;
}) {
  const { theme } = useDesignSystemTheme();
  const body = (
    <>
      <span css={{ width: 16, flex: "none", display: "grid", placeItems: "center", color: theme.colors.textSecondary }}
            onClick={(e) => { if (onToggle) { e.preventDefault(); e.stopPropagation(); onToggle(); } }}>
        {onToggle ? (open ? <ChevronDownIcon /> : <ChevronRightIcon />) : null}
      </span>
      {icon && <span css={{ color: theme.colors.textSecondary, display: "inline-flex" }}>{icon}</span>}
      <span css={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{label}{count != null ? ` (${count})` : ""}</span>
    </>
  );
  const css = {
    display: "flex", alignItems: "center", gap: 6, height: 28, paddingLeft: 4 + depth * 16, paddingRight: 8,
    color: theme.colors.textPrimary, textDecoration: "none", cursor: "pointer", fontSize: 13,
    borderLeft: `2px solid ${active ? theme.colors.actionPrimaryBackgroundDefault : "transparent"}`,
    background: active ? theme.colors.actionDefaultBackgroundHover : undefined,
    "&:hover": { background: theme.colors.actionDefaultBackgroundHover, textDecoration: "none", color: theme.colors.textPrimary },
  } as const;
  return to ? <Link to={to} css={css} onClick={onToggle && !open ? onToggle : undefined}>{body}</Link>
    : <div css={css} onClick={onToggle}>{body}</div>;
}

/** Catalog Explorer's object browser: the project, its datasets, each dataset's tables. */
function CatalogTree({ project, datasets, reload }: { project: string; datasets: Dataset[] | null; reload: () => void }) {
  const { theme } = useDesignSystemTheme();
  const params = useParams();
  // Another project's table isn't in this tree: highlight nothing.
  const [search] = useSearchParams();
  const elsewhere = !!search.get("project") && search.get("project") !== project;
  const activeDs = elsewhere ? undefined : params.dataset, activeTable = elsewhere ? undefined : params.table;
  const [q, setQ] = useState("");
  const [s, setS] = useState<TreeState>({ open: { [project]: true }, tables: {} });

  const load = (ds: string) => {
    if (s.tables[ds]) return;
    setS((x) => ({ ...x, tables: { ...x.tables, [ds]: "loading" } }));
    api.dataset(ds).then((d) => setS((x) => ({ ...x, tables: { ...x.tables, [ds]: d } })),
                         () => setS((x) => { const t = { ...x.tables }; delete t[ds]; return { ...x, tables: t }; }));
  };
  const toggle = (key: string, ds?: string) => {
    if (ds && !s.open[key]) load(ds);
    setS((x) => ({ ...x, open: { ...x.open, [key]: !x.open[key] } }));
  };
  useEffect(() => {
    if (!activeDs) return;
    load(activeDs);
    setS((x) => ({ ...x, open: { ...x.open, [project]: true, [activeDs]: true, [`${activeDs}/tables`]: true } }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDs, project]);

  const ql = q.toLowerCase();
  const shown = (datasets ?? []).filter((d) => !ql || d.id.toLowerCase().includes(ql)
    || (typeof s.tables[d.id] === "object" && (s.tables[d.id] as DatasetDetail).tables.some((t) => t.id.toLowerCase().includes(ql))));
  return (
    <div css={{ display: "flex", flexDirection: "column", minHeight: 0, height: "100%" }}>
      <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", minHeight: 24,
                  padding: `${theme.spacing.sm}px ${theme.spacing.sm}px ${theme.spacing.xs}px` }}>
        <Typography.Text bold>Catalog</Typography.Text>
        <Tooltip componentId="tree.refresh.tip" content="Refresh">
          <Button componentId="tree.refresh" size="small" icon={<RefreshIcon />} aria-label="Refresh"
                  onClick={() => { setS((x) => ({ ...x, tables: {} })); reload(); }} />
        </Tooltip>
      </div>
      <div css={{ padding: `0 ${theme.spacing.sm}px ${theme.spacing.sm}px` }}>
        <Input componentId="tree.search" prefix={<SearchIcon />} placeholder="Type to search..." value={q}
               onChange={(e) => setQ(e.target.value)} allowClear />
      </div>
      <div css={{ overflow: "auto", flex: 1, paddingBottom: theme.spacing.md }}>
        <TreeRow depth={0} open={!!s.open[project]} onToggle={() => toggle(project)} icon={<CatalogIcon />} label={project}
                 to="/catalog" active={!elsewhere && !activeDs} />
        {s.open[project] && !datasets && <div css={{ paddingLeft: 40 }}><Spinner size="small" /></div>}
        {s.open[project] && shown.map((d) => {
          const t = s.tables[d.id];
          const tables = typeof t === "object" ? t.tables.filter((x) => !ql || d.id.toLowerCase().includes(ql) || x.id.toLowerCase().includes(ql)) : [];
          const open = !!s.open[d.id] || (!!ql && tables.length > 0);
          return (
            <Fragment key={d.id}>
              <TreeRow depth={1} open={open} onToggle={() => toggle(d.id, d.id)} icon={<SchemaIcon />} label={d.id}
                       to={`/catalog/${d.id}`} active={activeDs === d.id && !activeTable} />
              {open && t === "loading" && <div css={{ paddingLeft: 56 }}><Spinner size="small" /></div>}
              {open && typeof t === "object" && (
                <>
                  <TreeRow depth={2} open={!!s.open[`${d.id}/tables`] || !!ql} onToggle={() => toggle(`${d.id}/tables`)}
                           label="Tables" count={t.tables.length} />
                  {(s.open[`${d.id}/tables`] || ql) && tables.map((x) => (
                    <TreeRow key={x.id} depth={3} icon={<ObjectIcon kind="table" type={x.type} />} label={x.id}
                             to={`/catalog/${d.id}/${x.id}`} active={activeDs === d.id && activeTable === x.id} />
                  ))}
                </>
              )}
            </Fragment>
          );
        })}
      </div>
    </div>
  );
}

/** Catalog Explorer: the object browser on the left, the selected object's page beside it. */
export function CatalogLayout() {
  const { theme } = useDesignSystemTheme();
  const me = useLoad(api.me, []);
  const ds = useLoad(api.datasets, []);
  const pad = theme.spacing.md;
  return (
    <div css={{ display: "grid", gridTemplateColumns: "280px minmax(0, 1fr)", margin: -pad, height: `calc(100% + ${2 * pad}px)` }}>
      <aside css={{ borderRight: `1px solid ${theme.colors.border}`, minHeight: 0 }}>
        {me.data && <CatalogTree project={me.data.project} datasets={ds.data} reload={ds.reload} />}
      </aside>
      <div css={{ minWidth: 0, overflow: "auto", padding: `${theme.spacing.sm}px ${pad}px ${pad}px` }}>
        <Outlet />
      </div>
    </div>
  );
}

// --- page head ---------------------------------------------------------------------------------

/** Databricks' object head: Catalog Explorer › ... crumbs, the icon tile, name, copy, then actions. */
function ObjectHeader({ crumbs, icon, title, fullName, consoleUrl, extra }: {
  crumbs: { to?: string; label: string }[]; icon: React.ReactNode; title: string; fullName: string; consoleUrl: string;
  extra?: React.ReactNode;
}) {
  const { theme } = useDesignSystemTheme();
  const [copied, setCopied] = useState(false);
  const copy = () => navigator.clipboard.writeText(fullName).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
  return (
    <>
      <Breadcrumb>
        <Breadcrumb.Item><Link to="/catalog">Catalog Explorer</Link></Breadcrumb.Item>
        {crumbs.map((c) => <Breadcrumb.Item key={c.label}>{c.to ? <Link to={c.to}>{c.label}</Link> : c.label}</Breadcrumb.Item>)}
        <Breadcrumb.Item>{""}</Breadcrumb.Item>
      </Breadcrumb>
      <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: theme.spacing.md, marginTop: 7, minHeight: 32 }}>
        <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm, minWidth: 0 }}>
          <IconTile>{icon}</IconTile>
          <Typography.Title level={2} css={{ "&&": { margin: 0 } }} ellipsis>{title}</Typography.Title>
          <Tooltip componentId="object.copy.tip" content={copied ? "Copied" : `Copy ${fullName}`}>
            <Button componentId="object.copy" icon={<CopyIcon />} aria-label="Copy full name" onClick={copy} />
          </Tooltip>
          {extra}
        </div>
        <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm, flex: "none" }}>
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <Button componentId="object.more" icon={<OverflowIcon />} aria-label="More actions" />
            </DropdownMenu.Trigger>
            <DropdownMenu.Content align="end">
              <DropdownMenu.Item componentId="object.more.copy" onClick={copy}>Copy full name</DropdownMenu.Item>
              <DropdownMenu.Item componentId="object.more.console" onClick={() => window.open(consoleUrl, "_blank", "noopener")}>Open in BigQuery</DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Root>
          <Button componentId="object.open" icon={<NewWindowIcon />} onClick={() => window.open(consoleUrl, "_blank", "noopener")}>Open in BigQuery</Button>
        </div>
      </div>
      <div css={{ height: 7 }} />
    </>
  );
}

function Description({ text }: { text: string }) {
  return (
    <>
      <Typography.Title level={4} css={{ "&&": { margin: "0 0 4px" } }}>Description</Typography.Title>
      <Typography.Paragraph color={text ? undefined : "secondary"} css={{ "&&": { marginBottom: 16 } }}>{text || "No description"}</Typography.Paragraph>
    </>
  );
}

/** The gray card Catalog Explorer puts a list in: filter and counts on top, the table below. */
function ListCard({ toolbar, children }: { toolbar: React.ReactNode; children: React.ReactNode }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div css={{ background: theme.colors.backgroundSecondary, border: `1px solid ${theme.colors.border}`,
                borderRadius: theme.borders.borderRadiusLg ?? 8, overflow: "hidden" }}>
      <div css={{ padding: 12, display: "flex", alignItems: "center", gap: theme.spacing.sm }}>{toolbar}</div>
      <div css={{ background: theme.colors.backgroundPrimary }}>{children}</div>
    </div>
  );
}

/** "16 schemas": the count beside a list's filter, as Catalog Explorer writes it. */
function CountPill({ label, n }: { label: string; n: number }) {
  return <Typography.Text>{n} {label.toLowerCase()}</Typography.Text>;
}

function KeyValues({ rows }: { rows: [string, React.ReactNode][] }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div css={{ maxWidth: 860 }}>
      {rows.map(([k, v]) => (
        <div key={k} css={{ display: "grid", gridTemplateColumns: "220px 1fr", gap: theme.spacing.md, padding: `${theme.spacing.sm}px 0`,
                            borderBottom: `1px solid ${theme.colors.border}` }}>
          <Typography.Text color="secondary">{k}</Typography.Text>
          <div css={{ minWidth: 0, overflowWrap: "anywhere" }}><Typography.Text>{v}</Typography.Text></div>
        </div>
      ))}
    </div>
  );
}

// --- the project (catalog) ------------------------------------------------------------------

export function CatalogPage() {
  const { theme } = useDesignSystemTheme();
  const me = useLoad(api.me, []);
  const { data, error } = useLoad(api.datasets, []);
  const [q, setQ] = useState("");
  const project = me.data?.project ?? "";
  const rows = (data ?? []).filter((d) => d.id.toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <ObjectHeader crumbs={[]} icon={<CatalogIcon />} title={project} fullName={project} consoleUrl={bqConsole(project)} />
      <Tabs.Root componentId="catalog.tabs" defaultValue="overview">
        <Tabs.List>
          <Tabs.Trigger value="overview">Overview</Tabs.Trigger>
          <Tabs.Trigger value="details">Details</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="overview">
          <Description text="The GCP project's BigQuery datasets." />
          {error && <Alert componentId="catalog.error" type="error" message="Couldn't list datasets" description={error} closable={false} />}
          {!data && !error && <TableSkeleton lines={6} />}
          {data && (
            <ListCard toolbar={<>
              <div css={{ width: 225 }}><Input componentId="catalog.filter" prefix={<SearchIcon />} placeholder="Filter datasets"
                                               value={q} onChange={(e) => setQ(e.target.value)} allowClear /></div>
              <CountPill label="Datasets" n={data.length} />
            </>}>
              <Table scrollable empty={rows.length ? undefined : <Empty title="No datasets" description="No dataset matches the filter." />}>
                <TableRow isHeader>
                  <TableHeader componentId="catalog.h.name">Name</TableHeader>
                  <TableHeader componentId="catalog.h.location">Location</TableHeader>
                  <TableHeader componentId="catalog.h.labels">Labels</TableHeader>
                </TableRow>
                {rows.map((d) => (
                  <TableRow key={d.id}>
                    <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.sm, alignItems: "center" }}>
                      <IconTile size={24}><SchemaIcon /></IconTile><Link to={`/catalog/${d.id}`}>{d.id}</Link></span></TableCell>
                    <TableCell>{d.location}</TableCell>
                    <TableCell>{Object.entries(d.labels).map(([k, v]) => <Tag key={k} componentId="catalog.label">{k}: {v}</Tag>)}</TableCell>
                  </TableRow>
                ))}
              </Table>
            </ListCard>
          )}
        </Tabs.Content>
        <Tabs.Content value="details">
          <KeyValues rows={[["Project", project], ["Datasets", data ? String(data.length) : "-"],
                            ["Region of the console", me.data?.region ?? "-"]]} />
        </Tabs.Content>
      </Tabs.Root>
    </>
  );
}

// --- a dataset (schema) ---------------------------------------------------------------------

export function DatasetPage() {
  const { dataset = "" } = useParams();
  const { theme } = useDesignSystemTheme();
  const me = useLoad(api.me, []);
  const { data: d, error } = useLoad(() => api.dataset(dataset), [dataset]);
  const [q, setQ] = useState("");
  const project = me.data?.project ?? "";
  const rows = (d?.tables ?? []).filter((t) => t.id.toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <ObjectHeader crumbs={[{ to: "/catalog", label: project }]} icon={<SchemaIcon />} title={dataset}
                    fullName={`${project}.${dataset}`} consoleUrl={bqConsole(project, dataset)} />
      {error && <Alert componentId="dataset.error" type="error" message="Couldn't load this dataset" description={error} closable={false} />}
      {!d && !error && <TableSkeleton lines={8} />}
      {d && (
        <Tabs.Root componentId="dataset.tabs" defaultValue="overview">
          <Tabs.List>
            <Tabs.Trigger value="overview">Overview</Tabs.Trigger>
            <Tabs.Trigger value="details">Details</Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="overview">
            <Description text={d.description} />
            <ListCard toolbar={<>
              <div css={{ width: 225 }}><Input componentId="dataset.filter" prefix={<SearchIcon />} placeholder="Filter tables"
                                               value={q} onChange={(e) => setQ(e.target.value)} allowClear /></div>
              <CountPill label="Tables" n={d.tables.length} />
            </>}>
              <Table scrollable empty={rows.length ? undefined : <Empty title="No tables" description={d.tables.length ? "No table matches the filter." : "This dataset has no tables."} />}>
                <TableRow isHeader>
                  <TableHeader componentId="dataset.h.name">Name</TableHeader>
                  <TableHeader componentId="dataset.h.type">Type</TableHeader>
                  <TableHeader componentId="dataset.h.created">Created at</TableHeader>
                  <TableHeader componentId="dataset.h.part">Partitioned by</TableHeader>
                  <TableHeader componentId="dataset.h.cluster">Clustered by</TableHeader>
                </TableRow>
                {rows.map((t) => (
                  <TableRow key={t.id}>
                    <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", gap: theme.spacing.sm, alignItems: "center", minWidth: 0 }}>
                      <IconTile size={24}><ObjectIcon kind="table" type={t.type} /></IconTile>
                      <Link to={`/catalog/${dataset}/${t.id}`}>{t.id}</Link></span></TableCell>
                    <TableCell>{OBJECT_TYPE[t.type] ?? t.type}</TableCell>
                    <TableCell>{when(t.created, true)}</TableCell>
                    <TableCell>{t.partitioning ?? "-"}</TableCell>
                    <TableCell><Typography.Text ellipsis>{t.clustering.join(", ") || "-"}</Typography.Text></TableCell>
                  </TableRow>
                ))}
              </Table>
            </ListCard>
          </Tabs.Content>
          <Tabs.Content value="details">
            <KeyValues rows={[
              ["Full name", <Typography.Text code>{`${project}.${dataset}`}</Typography.Text>],
              ["Location", d.location], ["Tables", String(d.tables.length)],
              ["Created at", when(d.created, true)], ["Last modified", when(d.updated, true)],
              ["Default table expiration", d.default_table_expiration_ms ? `${Number(d.default_table_expiration_ms) / 86400000} days` : "Never"],
              ...Object.entries(d.labels).map(([k, v]) => [`Label ${k}`, v] as [string, string]),
            ]} />
          </Tabs.Content>
        </Tabs.Root>
      )}
    </>
  );
}

// --- a table ---------------------------------------------------------------------------------

function ColumnRows({ cols, t, depth = 0, path = "" }: { cols: Column[]; t: TableDetail; depth?: number; path?: string }) {
  const { theme } = useDesignSystemTheme();
  return (
    <>
      {cols.map((c) => {
        const full = path + c.name;
        const keyTags = depth === 0 ? [
          ...(t.partitioning?.field === c.name ? ["Partition"] : []),
          ...(t.clustering.includes(c.name) ? ["Cluster"] : []),
        ] : [];
        return (
          <Fragment key={full}>
            <TableRow>
              <TableCell>
                <span css={{ display: "inline-flex", verticalAlign: "top", alignItems: "center", gap: theme.spacing.sm, paddingLeft: depth * theme.spacing.lg, minWidth: 0 }}>
                  <ColumnTypeIcon type={c.type} mode={c.mode} />
                  <Typography.Text ellipsis>{c.name}</Typography.Text>
                  {keyTags.map((k) => <Tag key={k} componentId="table.col.key">{k}</Tag>)}
                </span>
              </TableCell>
              <TableCell>{typeText(c.type, c.mode)}{c.mode === "REQUIRED" ? " not null" : ""}</TableCell>
              <TableCell><Typography.Text color={c.description ? undefined : "secondary"} ellipsis>{c.description}</Typography.Text></TableCell>
            </TableRow>
            {c.fields.length > 0 && <ColumnRows cols={c.fields} t={t} depth={depth + 1} path={`${full}.`} />}
          </Fragment>
        );
      })}
    </>
  );
}

function SampleData({ t }: { t: TableDetail }) {
  const free = useLoad<Sample | null>(() => (t.previewable ? api.preview(t.dataset, t.id, t.project) : Promise.resolve(null)), [t.full_name]);
  const [ran, setRan] = useState<Sample | null>(null);
  const [running, setRunning] = useState(false);
  const [qError, setQError] = useState<string | null>(null);
  const sample = free.data ?? ran;
  const types = Object.fromEntries(t.schema.map((c) => [c.name, c]));
  const run = () => {
    setRunning(true); setQError(null);
    api.queryPreview(t.dataset, t.id, t.project).then(setRan, (e: Error) => setQError(e.message)).finally(() => setRunning(false));
  };
  if (free.error) return <Alert componentId="sample.error" type="error" message="Couldn't preview this table" description={free.error} closable={false} />;
  if (t.previewable && !free.data) return <TableSkeleton lines={8} />;
  return (
    <>
      {!sample && (
        <Empty title={`A ${(OBJECT_TYPE[t.type] ?? t.type).toLowerCase()} has no free preview`}
          description="Running a query reads it: SELECT * LIMIT 50, refused by BigQuery if it would bill over 100 MB."
          button={<Button componentId="sample.run" type="primary" icon={<PlayIcon />} loading={running} onClick={run}>Run query</Button>} />
      )}
      {qError && <Alert componentId="sample.qerror" type="error" message="The query didn't run" description={qError} closable={false} />}
      {sample && (
        <>
          <Typography.Text color="secondary" size="sm">
            {sample.source === "preview"
              ? `${sample.rows.length} of ${sample.total_rows.toLocaleString()} rows, from BigQuery's table preview: no query ran, nothing billed.`
              : `${sample.rows.length} rows from a query that billed ${bytesText(sample.billed_bytes)}.`}
          </Typography.Text>
          <Spacer size="sm" />
          <SampleGrid sample={sample} types={types} />
        </>
      )}
    </>
  );
}

/** Catalog Explorer's table page: Overview (columns), Sample Data, Details, Lineage. */
export function TablePage() {
  const { dataset = "", table = "" } = useParams();
  const { theme } = useDesignSystemTheme();
  const me = useLoad(api.me, []);
  const [search] = useSearchParams();
  const other = search.get("project") ?? undefined;
  const { data: t, error } = useLoad(() => api.table(dataset, table, other), [dataset, table, other]);
  const [q, setQ] = useState("");
  const ours = me.data?.project ?? "";
  // A table shared from another project: no tree or dataset page for it, only this one.
  const project = other ?? ours;
  const cols = (t?.schema ?? []).filter((c) => c.name.toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <ObjectHeader crumbs={other && other !== ours ? [{ label: project }, { label: dataset }]
                              : [{ to: "/catalog", label: project }, { to: `/catalog/${dataset}`, label: dataset }]}
                    icon={<ObjectIcon kind="table" type={t?.type} />} title={table} fullName={`${project}.${dataset}.${table}`}
                    consoleUrl={bqConsole(project, dataset, table)}
                    extra={t && t.type !== "TABLE" && <Tag componentId="table.type">{OBJECT_TYPE[t.type] ?? t.type}</Tag>} />
      {error && <Alert componentId="table.error" type="error" message="Couldn't load this table" description={error} closable={false} />}
      {!t && !error && <TableSkeleton lines={8} />}
      {t && (
        <Tabs.Root componentId="table.tabs" defaultValue="overview">
          <Tabs.List>
            <Tabs.Trigger value="overview">Overview</Tabs.Trigger>
            <Tabs.Trigger value="sample">Sample Data</Tabs.Trigger>
            <Tabs.Trigger value="details">Details</Tabs.Trigger>
            <Tabs.Trigger value="lineage">Lineage</Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="overview">
            <Description text={t.description} />
            <ListCard toolbar={
              <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: theme.spacing.md, flex: 1 }}>
                <div css={{ width: 225 }}><Input componentId="table.cols.filter" prefix={<SearchIcon />} placeholder="Filter columns..."
                                                 value={q} onChange={(e) => setQ(e.target.value)} allowClear /></div>
                <Typography.Text color="secondary">{t.schema.length} columns</Typography.Text>
              </div>
            }>
              <Table scrollable>
                <TableRow isHeader>
                  <TableHeader componentId="table.cols.h.name">Column</TableHeader>
                  <TableHeader componentId="table.cols.h.type">Type</TableHeader>
                  <TableHeader componentId="table.cols.h.desc">Comment</TableHeader>
                </TableRow>
                <ColumnRows cols={cols} t={t} />
              </Table>
            </ListCard>
          </Tabs.Content>
          <Tabs.Content value="sample"><SampleData t={t} /></Tabs.Content>
          <Tabs.Content value="details">
            <KeyValues rows={[
              ["Full name", <Typography.Text code>{t.full_name}</Typography.Text>],
              ["Type", OBJECT_TYPE[t.type] ?? t.type], ["Location", t.location],
              ["Created at", when(t.created, true)], ["Last modified", when(t.updated, true)],
              ...(t.expires ? [["Expires", when(t.expires, true)] as [string, string]] : []),
              ["Rows", t.rows?.toLocaleString() ?? "-"], ["Size", bytesText(t.bytes)],
              ["Partitioned by", t.partitioning ? `${t.partitioning.field} (${t.partitioning.type})` : "-"],
              ["Partition filter", t.require_partition_filter ? "Required" : "Not required"],
              ["Clustered by", t.clustering.join(", ") || "-"],
              ...(t.external ? [["External source", `${t.external.format}: ${t.external.uris.join(", ")}`] as [string, string]] : []),
              ...Object.entries(t.labels).map(([k, v]) => [`Label ${k}`, v] as [string, string]),
            ]} />
            {t.view_query && (
              <>
                <Spacer size="md" />
                <Typography.Title level={4}>View definition</Typography.Title>
                <pre css={{ background: theme.colors.backgroundSecondary, padding: theme.spacing.md, borderRadius: theme.borders.borderRadiusMd,
                            overflow: "auto", fontSize: 12, fontFamily: "\"DM Mono\", monospace" }}>{t.view_query}</pre>
              </>
            )}
          </Tabs.Content>
          <Tabs.Content value="lineage">
            {ours && <LineageTab center={`table:${t.full_name}`} title={t.full_name} project={ours} />}
          </Tabs.Content>
        </Tabs.Root>
      )}
    </>
  );
}
