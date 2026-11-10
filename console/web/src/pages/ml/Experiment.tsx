import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Alert, BeakerIcon, Button, ChevronDownIcon, ChevronRightIcon, ColumnsIcon, DropdownMenu, Empty, Input,
  OverflowIcon, SearchIcon, SegmentedControlButton, SegmentedControlGroup, SortAscendingIcon, Spacer,
  TableCell, TableRow, TableSkeleton, Tag, Tooltip, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../../components/Table";
import { api, ExpRun } from "../../api";
import PageTitle from "../../components/PageTitle";
import { RetiredNotice } from "../../components/Retired";
import { Status } from "../../components/Status";
import { ago, fmtValue, useLoad, when } from "../../util";

type Clause = { scope: "metrics" | "params"; key: string; op: string; value: string };

/** MLflow's run search: `metrics.rmse < 1 and params.model = "tree"`; any other text matches run names. */
function parse(q: string): { clauses: Clause[]; text: string; error?: string } {
  const clauses: Clause[] = [];
  const text: string[] = [];
  for (const part of q.split(/\s+and\s+/i).map((p) => p.trim()).filter(Boolean)) {
    const m = part.match(/^(metrics|params)\.([\w.-]+|`[^`]+`)\s*(<=|>=|!=|=|<|>)\s*(.+)$/i);
    if (m) clauses.push({ scope: m[1].toLowerCase() as Clause["scope"], key: m[2].replace(/`/g, ""), op: m[3],
                          value: m[4].trim().replace(/^["']|["']$/g, "") });
    else if (/^(metrics|params)\./i.test(part)) return { clauses, text: "", error: `Can't read "${part}": use metrics.<name> < 1 or params.<name> = "value".` };
    else text.push(part.replace(/^["']|["']$/g, ""));
  }
  return { clauses, text: text.join(" ") };
}

function holds(r: ExpRun, c: Clause): boolean {
  const got = (c.scope === "metrics" ? r.metrics : r.params)[c.key];
  if (got === undefined) return false;
  const a = Number(got), b = Number(c.value);
  const numeric = !Number.isNaN(a) && !Number.isNaN(b) && String(got).trim() !== "";
  const [x, y] = numeric ? [a, b] : [String(got), c.value];
  switch (c.op) {
    case "=": return x === y;
    case "!=": return x !== y;
    case "<": return x < y;
    case "<=": return x <= y;
    case ">": return x > y;
    default: return x >= y;
  }
}

const STATES = ["all", "COMPLETE", "RUNNING", "FAILED"] as const;

/** One metric across the shown runs, as MLflow's chart view draws it: a bar per run. */
export function MetricBars({ metric, runs, expId }: { metric: string; runs: ExpRun[]; expId: string }) {
  const { theme } = useDesignSystemTheme();
  const rows = runs.filter((r) => typeof r.metrics[metric] === "number").slice(0, 20);
  const vals = rows.map((r) => r.metrics[metric]);
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const span = hi - lo || 1;
  return (
    <div css={{ border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd, padding: theme.spacing.md, minWidth: 0 }}>
      <Typography.Text bold>{metric}</Typography.Text>
      <Spacer size="sm" />
      {rows.length === 0 ? <Typography.Text color="secondary">No run logged it.</Typography.Text> : rows.map((r) => {
        const v = r.metrics[metric];
        const left = ((Math.min(v, 0) - lo) / span) * 100, width = (Math.abs(v) / span) * 100;
        return (
          <Tooltip key={r.id} componentId="exp.chart.bar" content={`${r.name}: ${fmtValue(v)}`}>
            <Link to={`/experiments/${expId}/runs/${r.id}`} aria-label={`${r.name}: ${fmtValue(v)}`} css={{
              display: "grid", gridTemplateColumns: "140px minmax(0, 1fr) 72px", alignItems: "center", gap: theme.spacing.sm,
              height: 22, color: theme.colors.textPrimary, "&:hover": { background: theme.colors.actionDefaultBackgroundHover } }}>
              <Typography.Text size="sm" color="secondary" ellipsis>{r.name}</Typography.Text>
              <span css={{ position: "relative", height: 12 }}>
                <span css={{ position: "absolute", top: 0, bottom: 0, left: `${left}%`, width: `max(2px, ${width}%)`,
                             background: theme.colors.actionPrimaryBackgroundDefault,
                             borderRadius: v < 0 ? "4px 0 0 4px" : "0 4px 4px 0" }} />
              </span>
              <Typography.Text size="sm" css={{ textAlign: "right" }}>{fmtValue(v)}</Typography.Text>
            </Link>
          </Tooltip>
        );
      })}
      {runs.filter((r) => typeof r.metrics[metric] === "number").length > 20 && (
        <><Spacer size="xs" /><Typography.Hint>The 20 newest runs that logged it</Typography.Hint></>)}
    </div>
  );
}

/** MLflow's experiment page as Databricks shows it: searchable runs with their metrics and params. */
export default function ExperimentPage() {
  const { id = "" } = useParams();
  const { theme } = useDesignSystemTheme();
  const { data, error } = useLoad(() => api.experiment(id), [id]);
  const [q, setQ] = useState("");
  const [state, setState] = useState<(typeof STATES)[number]>("all");
  const [view, setView] = useState<"table" | "chart">("table");
  const [sort, setSort] = useState("created:desc");
  const all = data?.runs ?? [];
  const metricKeys = useMemo(() => [...new Set(all.flatMap((r) => Object.keys(r.metrics)))].sort(), [all]);
  const paramKeys = useMemo(() => [...new Set(all.flatMap((r) => Object.keys(r.params)))].sort(), [all]);
  const [cols, setCols] = useState<Set<string> | null>(null);
  const shown = cols ?? new Set(metricKeys.slice(0, 6).map((k) => `metrics.${k}`));
  const toggle = (c: string) => { const s = new Set(shown); if (s.has(c)) s.delete(c); else s.add(c); setCols(s); };
  const search = parse(q);
  const order = (a: ExpRun, b: ExpRun) => {
    const [key, dir] = sort.split(":");
    const sign = dir === "asc" ? 1 : -1;
    if (key === "created") return sign * (a.created ?? "").localeCompare(b.created ?? "");
    const x = a.metrics[key.slice(8)], y = b.metrics[key.slice(8)];
    if (x === undefined) return 1;
    if (y === undefined) return -1;
    return sign * (x - y);
  };
  const runs = all.filter((r) => (state === "all" || r.state === state)
      && (!search.text || r.name.toLowerCase().includes(search.text.toLowerCase())) && search.clauses.every((c) => holds(r, c)))
    .sort(order);
  // Child runs sit under their parent, as MLflow nests them. A parent shows when it or one of its
  // children matches; one not logged yet (a sweep still running) stands in by its name.
  const [open, setOpen] = useState<Set<string>>(new Set());
  const flip = (name: string) => { const s = new Set(open); if (s.has(name)) s.delete(name); else s.add(name); setOpen(s); };
  const children = new Map<string, ExpRun[]>();
  for (const r of runs) if (r.parent) children.set(r.parent, [...(children.get(r.parent) ?? []), r]);
  const top = runs.filter((r) => !r.parent);
  for (const [name, kids] of children) {
    if (top.some((r) => r.name === name)) continue;
    top.push(all.find((r) => r.name === name && !r.parent) ?? { id: "", name, type: "Run", state: "RUNNING", params: {}, metrics: {},
      parent: null, created: kids.reduce((m, k) => (k.created < m ? k.created : m), kids[0].created), updated: "" });
  }
  top.sort(order);
  const nested = children.size > 0;
  const metricCols = [...shown].filter((c) => c.startsWith("metrics.")).map((c) => c.slice(8));
  const paramCols = [...shown].filter((c) => c.startsWith("params.")).map((c) => c.slice(7));
  const sortLabel = sort.startsWith("created") ? "Created" : sort.split(":")[0].slice(8);
  return (
    <>
      <PageTitle crumbs={[{ to: "/experiments", label: "Experiments" }]} icon={<BeakerIcon />} title={data?.name ?? id}
        actions={data && (
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild><Button componentId="exp.more" icon={<OverflowIcon />} aria-label="More actions" /></DropdownMenu.Trigger>
            <DropdownMenu.Content align="end">
              <DropdownMenu.Item componentId="exp.copy" onClick={() => navigator.clipboard.writeText(data.id)}>Copy experiment ID</DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Root>
        )} />
      <Spacer size="md" />
      {error && <Alert componentId="exp.error" type="error" message="Couldn't load this experiment" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={8} />}
      {data && <RetiredNotice kind="experiment" name={data.name} retired={data.retired} remove={() => api.deleteExperiment(data.id)} back="/experiments" />}
      {data && (
        <>
          <div css={{ display: "flex", gap: theme.spacing.sm, alignItems: "center", flexWrap: "wrap" }}>
            <div css={{ flex: "1 1 420px", maxWidth: 560 }}>
              <Input componentId="exp.search" prefix={<SearchIcon />} value={q} onChange={(e) => setQ(e.target.value)} allowClear
                     placeholder='metrics.rmse < 1 and params.model = "tree"' aria-label="Search runs"
                     validationState={search.error ? "error" : undefined} />
            </div>
            <SegmentedControlGroup componentId="exp.state" name="state" value={state} onChange={(e) => setState(e.target.value)}>
              {STATES.map((s) => <SegmentedControlButton key={s} value={s}>{s === "all" ? "All" : s === "COMPLETE" ? "Finished" : s === "RUNNING" ? "Running" : "Failed"}</SegmentedControlButton>)}
            </SegmentedControlGroup>
            <DropdownMenu.Root>
              <DropdownMenu.Trigger asChild><Button componentId="exp.sort" icon={<SortAscendingIcon />}>Sort: {sortLabel}</Button></DropdownMenu.Trigger>
              <DropdownMenu.Content align="start" css={{ maxHeight: 420, overflow: "auto" }}>
                <DropdownMenu.RadioGroup componentId="exp.sort.group" value={sort} onValueChange={setSort}>
                  <DropdownMenu.RadioItem value="created:desc"><DropdownMenu.ItemIndicator />Newest first</DropdownMenu.RadioItem>
                  <DropdownMenu.RadioItem value="created:asc"><DropdownMenu.ItemIndicator />Oldest first</DropdownMenu.RadioItem>
                  {metricKeys.length > 0 && <DropdownMenu.Separator />}
                  {metricKeys.flatMap((k) => [
                    <DropdownMenu.RadioItem key={`${k}a`} value={`metrics.${k}:asc`}><DropdownMenu.ItemIndicator />{k} ↑</DropdownMenu.RadioItem>,
                    <DropdownMenu.RadioItem key={`${k}d`} value={`metrics.${k}:desc`}><DropdownMenu.ItemIndicator />{k} ↓</DropdownMenu.RadioItem>])}
                </DropdownMenu.RadioGroup>
              </DropdownMenu.Content>
            </DropdownMenu.Root>
            <DropdownMenu.Root>
              <DropdownMenu.Trigger asChild><Button componentId="exp.columns" icon={<ColumnsIcon />}>Columns</Button></DropdownMenu.Trigger>
              <DropdownMenu.Content align="start" css={{ maxHeight: 420, overflow: "auto" }}>
                <DropdownMenu.Label>Metrics</DropdownMenu.Label>
                {metricKeys.map((k) => <DropdownMenu.CheckboxItem key={k} componentId="exp.col.metric" checked={shown.has(`metrics.${k}`)}
                  onCheckedChange={() => toggle(`metrics.${k}`)}><DropdownMenu.ItemIndicator />{k}</DropdownMenu.CheckboxItem>)}
                <DropdownMenu.Separator />
                <DropdownMenu.Label>Parameters</DropdownMenu.Label>
                {paramKeys.map((k) => <DropdownMenu.CheckboxItem key={k} componentId="exp.col.param" checked={shown.has(`params.${k}`)}
                  onCheckedChange={() => toggle(`params.${k}`)}><DropdownMenu.ItemIndicator />{k}</DropdownMenu.CheckboxItem>)}
              </DropdownMenu.Content>
            </DropdownMenu.Root>
            <span css={{ marginLeft: "auto" }}>
              <SegmentedControlGroup componentId="exp.view" name="view" value={view} onChange={(e) => setView(e.target.value)}>
                <SegmentedControlButton value="table">Table</SegmentedControlButton>
                <SegmentedControlButton value="chart">Chart</SegmentedControlButton>
              </SegmentedControlGroup>
            </span>
          </div>
          {search.error && <><Spacer size="xs" /><Typography.Text color="error" size="sm">{search.error}</Typography.Text></>}
          <Spacer size="sm" />
          <Typography.Hint>{runs.length} of {all.length} runs</Typography.Hint>
          <Spacer size="sm" />
          {runs.length === 0 ? (
            <Empty title={all.length ? "No matching runs" : "No runs"}
                   description={all.length ? "No run matches the search and state filter. Broaden them to see more." : "Runs appear here when training logs to this experiment."} />
          ) : view === "table" ? (
            <Table scrollable>
              <TableRow isHeader>
                <TableHeader componentId="exp.h.name" css={{ flex: "2 1 240px" }}>Run Name</TableHeader>
                <TableHeader componentId="exp.h.created">Created</TableHeader>
                <TableHeader componentId="exp.h.state">Status</TableHeader>
                <TableHeader componentId="exp.h.type">Type</TableHeader>
                {metricCols.map((m) => <TableHeader key={m} componentId="exp.h.metric" align="right" title={m}>{m}</TableHeader>)}
                {paramCols.map((p) => <TableHeader key={p} componentId="exp.h.param" title={p}>{p}</TableHeader>)}
              </TableRow>
              {top.flatMap((t) => [t, ...(open.has(t.name) ? children.get(t.name) ?? [] : [])]).map((r) => {
                const kids = r.parent ? 0 : children.get(r.name)?.length ?? 0;
                return (
                <TableRow key={r.id || `pending-${r.name}`}>
                  <TableCell css={{ flex: "2 1 240px", paddingLeft: r.parent ? theme.spacing.lg * 2 : undefined }}>
                    <span css={{ display: "inline-flex", verticalAlign: "top", alignItems: "center", gap: theme.spacing.xs, minWidth: 0 }}>
                      {kids > 0 ? <Button componentId="exp.run.toggle" size="small" onClick={() => flip(r.name)}
                                          icon={open.has(r.name) ? <ChevronDownIcon /> : <ChevronRightIcon />}
                                          aria-label={`${open.has(r.name) ? "Hide" : "Show"} ${r.name}'s child runs`} />
                        : nested && !r.parent ? <span css={{ width: 24, flex: "none" }} /> : null}
                      {r.id ? <Link css={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={r.name} to={`/experiments/${id}/runs/${r.id}`}>
                        {r.name}</Link> : <Typography.Text color="secondary" title="Not logged yet: its child runs are">{r.name}</Typography.Text>}
                      {kids > 0 && <Tag componentId="exp.run.children" css={{ flex: "none" }}>{kids} child run{kids > 1 ? "s" : ""}</Tag>}
                    </span>
                  </TableCell>
                  <TableCell><span title={when(r.created, true)}>{ago(r.created)}</span></TableCell>
                  <TableCell>{r.state ? <Status state={r.state} /> : <Typography.Text color="secondary">-</Typography.Text>}</TableCell>
                  <TableCell>{r.type}</TableCell>
                  {metricCols.map((m) => <TableCell key={m} css={{ justifyContent: "flex-end" }}>{m in r.metrics ? fmtValue(r.metrics[m]) : "-"}</TableCell>)}
                  {paramCols.map((p) => <TableCell key={p}><Typography.Text ellipsis>{p in r.params ? fmtValue(r.params[p]) : "-"}</Typography.Text></TableCell>)}
                </TableRow>
                );
              })}
            </Table>
          ) : (
            metricCols.length === 0 ? <Empty title="No metrics chosen" description="Pick metrics under Columns to chart them." /> : (
              <div css={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(420px, 1fr))", gap: theme.spacing.md }}>
                {metricCols.map((m) => <MetricBars key={m} metric={m} runs={runs} expId={id} />)}
              </div>
            )
          )}
        </>
      )}
    </>
  );
}
