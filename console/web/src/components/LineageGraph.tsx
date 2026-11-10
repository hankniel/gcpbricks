import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  BaseEdge, Background, BackgroundVariant, Controls, Edge, EdgeLabelRenderer, EdgeProps, Handle, MarkerType, Node,
  Position, ReactFlow, getBezierPath,
} from "@xyflow/react";
import {
  Alert, BinaryIcon, BracketsCurlyIcon, BracketsSquareIcon, Button, CalendarIcon, CheckboxIcon, ClockIcon, CloseIcon,
  DecimalIcon, FloatIcon, FolderCloudIcon, GlobeIcon, Input, LettersIcon, ModelsIcon, NumbersIcon, PlusCircleFillIcon,
  Spinner, TableGlobeIcon, TableIcon, TableLightningIcon, TableViewIcon, Tag, Tooltip, Typography, WorkflowsIcon,
  useDesignSystemTheme,
} from "@databricks/design-system";
import { api, Lineage, LineageNode } from "../api";

// --- shared bits: object and column type icons, as Catalog Explorer draws them -------------

/** BigQuery's legacy type names, as GoogleSQL writes them (Databricks shows types in lowercase). */
const TYPE_NAME: Record<string, string> = { INTEGER: "int64", FLOAT: "float64", BOOLEAN: "bool", RECORD: "struct" };
export function typeText(type: string, mode?: string): string {
  const t = TYPE_NAME[type] ?? type.toLowerCase();
  return mode === "REPEATED" ? `array<${t}>` : t;
}

export function ColumnTypeIcon({ type, mode }: { type: string; mode?: string }) {
  const { theme } = useDesignSystemTheme();
  const css = { color: theme.colors.textSecondary, flex: "none" } as const;
  if (mode === "REPEATED") return <BracketsSquareIcon css={css} />;
  switch (type) {
    case "STRING": return <LettersIcon css={css} />;
    case "INTEGER": case "INT64": return <NumbersIcon css={css} />;
    case "FLOAT": case "FLOAT64": return <FloatIcon css={css} />;
    case "NUMERIC": case "BIGNUMERIC": return <DecimalIcon css={css} />;
    case "BOOLEAN": case "BOOL": return <CheckboxIcon css={css} />;
    case "TIMESTAMP": case "DATETIME": case "TIME": return <ClockIcon css={css} />;
    case "DATE": return <CalendarIcon css={css} />;
    case "BYTES": return <BinaryIcon css={css} />;
    case "GEOGRAPHY": return <GlobeIcon css={css} />;
    default: return <BracketsCurlyIcon css={css} />;
  }
}

export const OBJECT_TYPE: Record<string, string> = { TABLE: "Table", VIEW: "View", MATERIALIZED_VIEW: "Materialized view",
  EXTERNAL: "External table", SNAPSHOT: "Snapshot", CLONE: "Clone" };

export function ObjectIcon({ kind, type }: { kind: string; type?: string }) {
  if (kind === "model") return <ModelsIcon />;
  if (kind === "path") return <FolderCloudIcon />;
  if (type === "VIEW") return <TableViewIcon />;
  if (type === "MATERIALIZED_VIEW") return <TableLightningIcon />;
  if (type === "EXTERNAL") return <TableGlobeIcon />;
  return <TableIcon />;
}

/** The gray rounded tile an object's icon sits in, on cards, headers and list rows. */
export function IconTile({ children, size = 32 }: { children: React.ReactNode; size?: number }) {
  const { theme } = useDesignSystemTheme();
  return (
    <span css={{ width: size, height: size, flex: "none", display: "grid", placeItems: "center", color: theme.colors.textSecondary,
                 borderRadius: theme.borders.borderRadiusMd, background: theme.colors.backgroundSecondary }}>{children}</span>
  );
}

export function kindText(n: LineageNode): string {
  if (n.kind === "model") return "Model version";
  if (n.kind === "path") return "Storage path";
  return OBJECT_TYPE[n.type ?? "TABLE"] ?? "Table";
}

/** A node's own name and the line under it: table and dataset, model and version, bucket. */
export function nodeTitle(n: LineageNode, project: string): { title: string; sub: string } {
  if (n.kind === "table") return { title: n.table ?? n.name, sub: n.project === project ? n.dataset ?? "" : `${n.project}.${n.dataset}` };
  if (n.kind === "model") return { title: n.model_name ?? n.label, sub: `Version ${n.version}` };
  return { title: n.name, sub: "Cloud Storage" };
}

/** Where a node opens: a table's catalog page, a version's page, a bucket in the GCP console. */
export function nodeHref(n: LineageNode, project: string): { to?: string; href?: string } {
  if (n.kind === "table" && n.project === project) return { to: `/catalog/${n.dataset}/${n.table}` };
  if (n.kind === "table" && n.readable !== false) return { to: `/catalog/${n.dataset}/${n.table}?project=${n.project}` };
  if (n.kind === "model") return { to: `/models/${n.model_id}/versions/${n.version}` };
  if (n.kind === "path") return { href: `https://console.cloud.google.com/storage/browser/${n.name.replace("gs://", "")}?project=${project}` };
  return {};
}

export function jobHref(name: string, project: string) {
  return `https://console.cloud.google.com/run/jobs/details/us-central1/${name}/executions?project=${project}`;
}

export function NodeLink({ n, project, children }: { n: LineageNode; project: string; children: React.ReactNode }) {
  const h = nodeHref(n, project);
  if (h.to) return <Link to={h.to}>{children}</Link>;
  if (h.href) return <a href={h.href} target="_blank" rel="noopener noreferrer">{children}</a>;
  return <>{children}</>;
}

// --- the graph -------------------------------------------------------------------------------

const COLS_SHOWN = 8;
const CARD_W = 300;

type CardData = { n: LineageNode; project: string; center: boolean; canUp: boolean; canDown: boolean; busy: boolean;
  expand: (id: string, dir: "up" | "down") => void };

function Expand({ side, label, onClick }: { side: "left" | "right"; label: string; onClick: () => void }) {
  const { theme } = useDesignSystemTheme();
  return (
    <Tooltip componentId="lineage.expand.tip" content={label}>
      <button className="nodrag" onClick={(e) => { e.stopPropagation(); onClick(); }} aria-label={label} css={{
        all: "unset", position: "absolute", top: 18, [side]: -9, width: 18, height: 18, display: "grid", placeItems: "center",
        cursor: "pointer", borderRadius: "50%", background: theme.colors.backgroundPrimary, color: theme.colors.textSecondary,
        fontSize: 18, "&:hover": { color: theme.colors.actionPrimaryBackgroundDefault },
      }}><PlusCircleFillIcon /></button>
    </Tooltip>
  );
}

/** A lineage card as Databricks draws one: its type above, icon, name and schema, then its columns. */
function LineageCard({ data }: { data: CardData }) {
  const { theme } = useDesignSystemTheme();
  const { n, project } = data;
  const [q, setQ] = useState("");
  const [all, setAll] = useState(false);
  const cols = (n.columns ?? []).filter((c) => c.name.toLowerCase().includes(q.toLowerCase()));
  const shown = all ? cols : cols.slice(0, COLS_SHOWN);
  const { title, sub } = nodeTitle(n, project);
  return (
    <div css={{ position: "relative", width: CARD_W }}>
      <div css={{ position: "absolute", top: -20, left: 0 }}>
        <Typography.Text color="secondary" size="sm">{kindText(n)}</Typography.Text>
      </div>
      <div css={{
        background: theme.colors.backgroundPrimary, borderRadius: theme.borders.borderRadiusMd, boxShadow: theme.shadows.sm,
        border: `${data.center ? 2 : 1}px solid ${data.center ? theme.colors.actionPrimaryBackgroundDefault : theme.colors.border}`,
        padding: theme.spacing.sm,
      }}>
        <Handle type="target" position={Position.Left} css={{ opacity: 0, top: 26 }} />
        <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm, minWidth: 0 }}>
          <IconTile><ObjectIcon kind={n.kind} type={n.type} /></IconTile>
          <div css={{ minWidth: 0, flex: 1 }}>
            <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.xs }}>
              <Typography.Text bold ellipsis title={n.name}><NodeLink n={n} project={project}>{title}</NodeLink></Typography.Text>
              {data.busy && <Spinner size="small" />}
            </div>
            <Typography.Text color="secondary" size="sm" ellipsis>{sub}</Typography.Text>
          </div>
        </div>
        {n.readable === false && (
          <div css={{ marginTop: theme.spacing.sm }}>
            <Typography.Text color="secondary" size="sm">In another project the console's account can't read.</Typography.Text>
          </div>
        )}
        {n.kind === "model" && n.aliases && n.aliases.length > 0 && (
          <div css={{ marginTop: theme.spacing.sm, display: "flex", gap: theme.spacing.xs, flexWrap: "wrap" }}>
            {n.aliases.map((a) => <Tag key={a} componentId="lineage.alias">@{a}</Tag>)}
          </div>
        )}
        {n.columns && (
          <div className="nodrag nowheel" css={{ marginTop: theme.spacing.sm }}>
            <Input componentId="lineage.cols.search" size="small" placeholder="Search columns..." value={q}
                   onChange={(e) => setQ(e.target.value)} />
            <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", margin: `${theme.spacing.xs}px 0` }}>
              <Typography.Text color="secondary" size="sm">
                {shown.length < cols.length ? `Showing ${shown.length} of ${cols.length} columns` : `${cols.length} columns`}
              </Typography.Text>
              {cols.length > COLS_SHOWN && (
                <Button componentId="lineage.cols.all" size="small" onClick={() => setAll(!all)}>
                  {all ? "Show fewer columns" : "Show all columns"}
                </Button>
              )}
            </div>
            <div css={{ maxHeight: all ? 320 : undefined, overflow: "auto" }}>
              {shown.map((c) => (
                <div key={c.name} css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm, height: 26, padding: `0 ${theme.spacing.xs}px` }}>
                  <ColumnTypeIcon type={c.type} />
                  <Typography.Text ellipsis css={{ flex: 1 }}>{c.name}</Typography.Text>
                  <Typography.Text color="secondary" size="sm">{typeText(c.type)}</Typography.Text>
                </div>
              ))}
            </div>
          </div>
        )}
        <Handle type="source" position={Position.Right} css={{ opacity: 0, top: 26 }} />
      </div>
      {data.canUp && <Expand side="left" label="Show upstream" onClick={() => data.expand(n.id, "up")} />}
      {data.canDown && <Expand side="right" label="Show downstream" onClick={() => data.expand(n.id, "down")} />}
    </div>
  );
}

type EdgeData = { via?: LineageNode["via"]; onPick: (id: string) => void };

/** A curved edge; one a job ran carries the job as a circle midway, as Databricks draws processes. */
function LineageEdge(p: EdgeProps<Edge<EdgeData>>) {
  const { theme } = useDesignSystemTheme();
  const [path, x, y] = getBezierPath(p);
  return (
    <>
      <BaseEdge id={p.id} path={path} markerEnd={p.markerEnd} style={p.style} interactionWidth={16} />
      {p.data?.via && (
        <EdgeLabelRenderer>
          <Tooltip componentId="lineage.process.tip" content={`Cloud Run job ${p.data.via.name}`}>
            <button className="nodrag nopan" onClick={() => p.data?.onPick(p.id)} aria-label={`Cloud Run job ${p.data.via.name}`} css={{
              all: "unset", position: "absolute", transform: `translate(-50%, -50%) translate(${x}px, ${y}px)`, pointerEvents: "all",
              width: 32, height: 32, display: "grid", placeItems: "center", cursor: "pointer", borderRadius: "50%",
              background: theme.colors.backgroundPrimary, border: `1px solid ${theme.colors.textPlaceholder}`,
              color: theme.colors.textPrimary, "&:hover": { borderColor: theme.colors.actionPrimaryBackgroundDefault },
            }}><WorkflowsIcon /></button>
          </Tooltip>
        </EdgeLabelRenderer>
      )}
    </>
  );
}
const nodeTypes = { lineage: LineageCard };
const edgeTypes = { lineage: LineageEdge };

type State = { nodes: Record<string, { n: LineageNode; level: number }>;
  edges: Record<string, { from: string; to: string; via?: LineageNode["via"]; role?: string }>; done: Record<string, true> };

function merge(s: State, l: Lineage, level: number, dirs: ("up" | "down")[]): State {
  const nodes = { ...s.nodes }, edges = { ...s.edges }, done = { ...s.done };
  const id = l.node.id;
  nodes[id] = { n: l.node, level: nodes[id]?.level ?? level };
  for (const dir of dirs) {
    done[`${id}|${dir}`] = true;
    for (const o of dir === "up" ? l.upstream : l.downstream) {
      if (!nodes[o.id]) nodes[o.id] = { n: o, level: level + (dir === "up" ? -1 : 1) };
      const [from, to] = dir === "up" ? [o.id, id] : [id, o.id];
      edges[`${from}>${to}`] = { from, to, via: o.via, role: o.role };
    }
  }
  return { nodes, edges, done };
}

function cardHeight(n: LineageNode): number {
  if (n.columns) return 110 + 26 * Math.min(n.columns.length, COLS_SHOWN);
  return n.kind === "model" && n.aliases?.length ? 96 : 64;
}

function DetailsPanel({ state, edgeId, project, onClose }: { state: State; edgeId: string; project: string; onClose: () => void }) {
  const { theme } = useDesignSystemTheme();
  const e = state.edges[edgeId];
  const end = (id: string, label: string) => {
    const n = state.nodes[id].n;
    const { title, sub } = nodeTitle(n, project);
    return (
      <div css={{ marginBottom: theme.spacing.md }}>
        <Typography.Text bold>{label}</Typography.Text>
        <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm, marginTop: theme.spacing.xs }}>
          <IconTile><ObjectIcon kind={n.kind} type={n.type} /></IconTile>
          <div css={{ minWidth: 0 }}>
            <Typography.Text ellipsis><NodeLink n={n} project={project}>{title}</NodeLink></Typography.Text>
            <div><Typography.Text color="secondary" size="sm">{sub}</Typography.Text></div>
          </div>
        </div>
      </div>
    );
  };
  return (
    <aside css={{ position: "absolute", top: 0, right: 0, bottom: 0, width: 380, zIndex: 5, overflow: "auto",
                  background: theme.colors.backgroundPrimary, borderLeft: `1px solid ${theme.colors.border}`, padding: theme.spacing.lg }}>
      <div css={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: theme.spacing.md }}>
        <Typography.Title level={3} css={{ "&&": { margin: 0 } }}>Lineage details</Typography.Title>
        <Button componentId="lineage.details.close" icon={<CloseIcon />} aria-label="Close" onClick={onClose} />
      </div>
      {end(e.from, "Source")}
      {end(e.to, "Target")}
      {e.role && <Typography.Paragraph color="secondary">Its training reads this table as its {e.role}.</Typography.Paragraph>}
      <div css={{ borderTop: `1px solid ${theme.colors.border}`, paddingTop: theme.spacing.md }}>
        <div css={{ display: "grid", gridTemplateColumns: "1fr auto", padding: `${theme.spacing.xs}px 0`, borderBottom: `1px solid ${theme.colors.border}` }}>
          <Typography.Text bold>Name</Typography.Text><Typography.Text bold>Type</Typography.Text>
        </div>
        {e.via ? (
          <div css={{ display: "grid", gridTemplateColumns: "1fr auto", alignItems: "center", padding: `${theme.spacing.sm}px 0` }}>
            <span css={{ display: "inline-flex", alignItems: "center", gap: theme.spacing.sm }}>
              <IconTile size={28}><WorkflowsIcon /></IconTile>
              <a href={jobHref(e.via.name, project)} target="_blank" rel="noopener noreferrer">{e.via.name}</a>
            </span>
            <Typography.Text color="secondary">Cloud Run job</Typography.Text>
          </div>
        ) : (
          <Typography.Paragraph color="secondary" css={{ "&&": { marginTop: theme.spacing.sm } }}>
            {state.nodes[e.to].n.kind === "model" ? "The version's training." : "Recorded by BigQuery from its own jobs."}
          </Typography.Paragraph>
        )}
      </div>
    </aside>
  );
}

/** Databricks' lineage graph: the node in the middle, one level up and down, more on each ⊕. */
export default function LineageGraph({ center, project }: { center: string; project: string }) {
  const { theme } = useDesignSystemTheme();
  const [state, setState] = useState<State | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);

  useEffect(() => {
    setState(null); setError(null); setPicked(null);
    api.lineage(center).then((l) => setState(merge({ nodes: {}, edges: {}, done: {} }, l, 0, ["up", "down"])),
                              (e: Error) => setError(e.message));
  }, [center]);

  const expand = useCallback((id: string, dir: "up" | "down") => {
    setBusy(id);
    api.lineage(id).then(
      (l) => setState((s) => s && merge(s, l, s.nodes[id].level, [dir])),
      (e: Error) => setError(e.message),
    ).finally(() => setBusy(null));
  }, []);

  const flow = useMemo(() => {
    if (!state) return { ns: [], es: [] };
    const byLevel: Record<number, string[]> = {};
    for (const [id, { level }] of Object.entries(state.nodes)) (byLevel[level] ??= []).push(id);
    const ns: Node<CardData>[] = [];
    for (const [lvl, ids] of Object.entries(byLevel)) {
      ids.sort((a, b) => state.nodes[a].n.label.localeCompare(state.nodes[b].n.label));
      const heights = ids.map((id) => cardHeight(state.nodes[id].n) + 56);
      let y = -heights.reduce((a, b) => a + b, 0) / 2;
      ids.forEach((id, i) => {
        const { n } = state.nodes[id];
        ns.push({ id, type: "lineage", position: { x: Number(lvl) * (CARD_W + 140), y },
                  data: { n, project, center: id === center, busy: busy === id, expand,
                          canUp: n.kind !== "path" && !state.done[`${id}|up`],
                          canDown: n.kind !== "path" && !state.done[`${id}|down`] } });
        y += heights[i];
      });
    }
    const es: Edge<EdgeData>[] = Object.entries(state.edges).map(([id, e]) => ({
      id, source: e.from, target: e.to, type: "lineage", data: { via: e.via, onPick: setPicked },
      style: { stroke: picked === id ? theme.colors.actionPrimaryBackgroundDefault : theme.colors.textPlaceholder, strokeWidth: picked === id ? 2 : 1 },
      markerEnd: { type: MarkerType.ArrowClosed, color: theme.colors.textPlaceholder },
    }));
    return { ns, es };
  }, [state, busy, center, expand, project, picked, theme]);

  return (
    <div css={{ position: "relative", height: "100%", minHeight: 360 }}>
      {error && <Alert componentId="lineage.error" type="error" message="Couldn't load lineage" description={error} closable={false} />}
      {!state && !error && <div css={{ position: "absolute", inset: 0, display: "grid", placeItems: "center" }}><Spinner /></div>}
      {state && (
        <ReactFlow nodes={flow.ns} edges={flow.es} nodeTypes={nodeTypes} edgeTypes={edgeTypes} onEdgeClick={(_, e) => setPicked(e.id)}
                   colorMode={theme.isDarkMode ? "dark" : "light"} fitView fitViewOptions={{ nodes: [{ id: center }], minZoom: 0.85, maxZoom: 0.85 }} minZoom={0.2}
                   nodesDraggable={false} nodesConnectable={false} proOptions={{ hideAttribution: true }}>
          <Background variant={BackgroundVariant.Dots} gap={12} size={1} color={theme.colors.border} />
          <Controls showInteractive={false} position="bottom-left" />
        </ReactFlow>
      )}
      {state && picked && state.edges[picked] && (
        <DetailsPanel state={state} edgeId={picked} project={project} onClose={() => setPicked(null)} />
      )}
    </div>
  );
}

/** "See lineage graph": the graph over the whole window, titled as Databricks titles it. */
export function LineageGraphOverlay({ center, title, project, onClose }: { center: string; title: string; project: string; onClose: () => void }) {
  const { theme } = useDesignSystemTheme();
  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <div role="dialog" aria-label={`Data Lineage for ${title}`} css={{
      position: "fixed", inset: 16, zIndex: 1000, display: "grid", gridTemplateRows: "auto 1fr",
      background: theme.colors.backgroundPrimary, borderRadius: theme.borders.borderRadiusLg ?? 8,
      boxShadow: theme.shadows.xl, border: `1px solid ${theme.colors.border}`, overflow: "hidden",
    }}>
      <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: `${theme.spacing.md}px ${theme.spacing.lg}px` }}>
        <Typography.Title level={3} css={{ "&&": { margin: 0 } }}>Data Lineage for {title}</Typography.Title>
        <Button componentId="lineage.overlay.close" icon={<CloseIcon />} aria-label="Close" onClick={onClose} />
      </div>
      <div css={{ minHeight: 0, background: theme.colors.backgroundSecondary }}>
        <LineageGraph center={center} project={project} />
      </div>
    </div>
  );
}
