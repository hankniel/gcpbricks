import { useState } from "react";
import {
  Alert, Button, Empty, FolderCloudIcon, Input, LayerGraphIcon, ModelsIcon, RefreshIcon, SearchIcon,
  SimpleSelect, SimpleSelectOption, Spacer, TableCell, TableIcon, TableRow, TableSkeleton, Typography,
  WorkflowsIcon, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "./Table";
import { api, LineageNode } from "../api";
import { useLoad, when } from "../util";
import { IconTile, jobHref, LineageGraphOverlay, NodeLink, ObjectIcon } from "./LineageGraph";

type Row = { n: LineageNode; direction: "Upstream" | "Downstream" };
type Category = { key: string; label: string; icon: JSX.Element; rows: Row[] };

/** Databricks' Lineage tab: what is connected, by kind, in a list; the graph one click away. */
export default function LineageTab({ center, title, project }: { center: string; title: string; project: string }) {
  const { theme } = useDesignSystemTheme();
  const { data, error, reload } = useLoad(() => api.lineage(center), [center]);
  const [q, setQ] = useState("");
  const [dir, setDir] = useState("all");
  const [cat, setCat] = useState<string | null>(null);
  const [graph, setGraph] = useState(false);

  const rows: Row[] = data ? [...data.upstream.map((n) => ({ n, direction: "Upstream" as const })),
                              ...data.downstream.map((n) => ({ n, direction: "Downstream" as const }))] : [];
  const keep = (r: Row) => (dir === "all" || r.direction.toLowerCase() === dir) && r.n.name.toLowerCase().includes(q.toLowerCase());
  // A job is no node: it is the process on an edge, listed here as Databricks lists Workflows.
  const jobs = rows.filter((r) => r.n.via && keep(r));
  const cats: Category[] = [
    { key: "tables", label: "Tables", icon: <TableIcon />, rows: rows.filter((r) => r.n.kind === "table" && keep(r)) },
    { key: "models", label: "Models", icon: <ModelsIcon />, rows: rows.filter((r) => r.n.kind === "model" && keep(r)) },
    { key: "jobs", label: "Workflows", icon: <WorkflowsIcon />, rows: jobs },
    { key: "paths", label: "Storage paths", icon: <FolderCloudIcon />, rows: rows.filter((r) => r.n.kind === "path" && keep(r)) },
  ];
  // Until one is picked, open on the first kind that has anything, as Databricks does.
  const current = cats.find((c) => c.key === cat) ?? cats.find((c) => c.rows.length) ?? cats[0];

  return (
    <>
      <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm, flexWrap: "wrap" }}>
        <div css={{ width: 400 }}>
          <Input componentId="lineage.tab.filter" prefix={<SearchIcon />} placeholder="Filter lineage" value={q}
                 onChange={(e) => setQ(e.target.value)} allowClear />
        </div>
        <div css={{ width: 220 }}>
          <SimpleSelect componentId="lineage.tab.direction" id="lineage-direction" value={dir} onChange={(e) => setDir(e.target.value)}>
            <SimpleSelectOption value="all">All connections</SimpleSelectOption>
            <SimpleSelectOption value="upstream">Upstream</SimpleSelectOption>
            <SimpleSelectOption value="downstream">Downstream</SimpleSelectOption>
          </SimpleSelect>
        </div>
        <span css={{ flex: 1 }} />
        <Button componentId="lineage.tab.refresh" icon={<RefreshIcon />} aria-label="Refresh" onClick={reload} />
        <Button componentId="lineage.tab.graph" type="primary" icon={<LayerGraphIcon />} onClick={() => setGraph(true)}>See lineage graph</Button>
      </div>
      <Spacer size="md" />
      {error && <Alert componentId="lineage.tab.error" type="error" message="Couldn't load lineage" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={4} />}
      {data && (
        <div css={{ display: "grid", gridTemplateColumns: "180px minmax(0, 1fr)", gap: theme.spacing.md }}>
          <nav aria-label="Lineage kinds" css={{ borderRight: `1px solid ${theme.colors.border}` }}>
            {cats.map((c) => (
              <button key={c.key} onClick={() => setCat(c.key)} css={{
                all: "unset", boxSizing: "border-box", cursor: "pointer", width: "100%", display: "flex", alignItems: "center",
                gap: theme.spacing.sm, padding: `${theme.spacing.sm}px ${theme.spacing.md}px`, color: theme.colors.textPrimary, fontSize: 13,
                background: c.key === current.key ? theme.colors.actionDefaultBackgroundHover : undefined,
                borderRight: c.key === current.key ? `2px solid ${theme.colors.actionPrimaryBackgroundDefault}` : "2px solid transparent",
                "&:hover": { background: theme.colors.actionDefaultBackgroundHover },
              }}>{c.icon}<span css={{ flex: 1 }}>{c.label}</span>
                <Typography.Text color="secondary" size="sm">{c.rows.length}</Typography.Text></button>
            ))}
          </nav>
          <div css={{ minWidth: 0 }}>
            <Table scrollable empty={current.rows.length ? undefined : (
              <Empty title={`No ${current.label.toLowerCase()}`} description={`No ${current.label.toLowerCase()} are connected to ${title}${q || dir !== "all" ? " that match the filters" : ""}.`} />)}>
              <TableRow isHeader>
                <TableHeader componentId="lineage.tab.h.name">{current.key === "jobs" ? "Job name" : current.key === "models" ? "Model version" : current.key === "paths" ? "Path" : "Table name"}</TableHeader>
                {(current.key === "tables" || current.key === "paths") && <TableHeader componentId="lineage.tab.h.last">Last activity</TableHeader>}
                <TableHeader componentId="lineage.tab.h.dir">Lineage direction</TableHeader>
              </TableRow>
              {current.rows.map((r) => (
                <TableRow key={`${current.key}:${r.n.id}:${r.direction}`}>
                  <TableCell>
                    <span css={{ display: "inline-flex", verticalAlign: "top", alignItems: "center", gap: theme.spacing.sm, minWidth: 0 }}>
                      {current.key === "jobs" ? (
                        <><IconTile size={24}><WorkflowsIcon /></IconTile>
                          <a href={jobHref(r.n.via!.name, project)} target="_blank" rel="noopener noreferrer">{r.n.via!.name}</a></>
                      ) : (
                        <><IconTile size={24}><ObjectIcon kind={r.n.kind} type={r.n.type} /></IconTile>
                          <NodeLink n={r.n} project={project}>{r.n.kind === "table" ? r.n.label : r.n.kind === "model" ? r.n.label : r.n.name}</NodeLink></>
                      )}
                    </span>
                  </TableCell>
                  {(current.key === "tables" || current.key === "paths") && <TableCell>{r.n.last_seen ? when(r.n.last_seen, true) : "-"}</TableCell>}
                  <TableCell>{r.direction}</TableCell>
                </TableRow>
              ))}
            </Table>
          </div>
        </div>
      )}
      {graph && <LineageGraphOverlay center={center} title={title} project={project} onClose={() => setGraph(false)} />}
    </>
  );
}
