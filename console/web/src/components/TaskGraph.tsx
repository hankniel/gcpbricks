import { useMemo, useRef } from "react";
import { Background, Controls, Edge, Handle, MarkerType, Node, NodeChange, Position, ReactFlow } from "@xyflow/react";
import {
  CodeIcon, FolderIcon, ForkIcon, Typography, WorkflowsIcon, useDesignSystemTheme,
} from "@databricks/design-system";
import { GraphNode } from "../api";
import { StatusIcon, useStateFill } from "./Status";
import { duration, KIND, label } from "../util";
import { HEAD, nestedLayout, PAD } from "./graphLayout";

type Timing = { start: string | null; end: string | null };
type Data = { node: GraphNode; status?: string | null; timing?: Timing; selected?: boolean; dim?: boolean; vertical?: boolean;
  onPick?: (k: string) => void };

function DetailIcon({ type }: { type: string }) {
  if (type === "condition") return <CodeIcon />;
  if (type === "workflow") return <WorkflowsIcon />;
  if (type === "for_each") return <ForkIcon />;
  return <FolderIcon />;
}

/** A task as Databricks draws one: name, state and duration, what it runs; a colored top edge in a run. */
function TaskNode({ data }: { data: Data }) {
  const { theme } = useDesignSystemTheme();
  const fill = useStateFill();
  const n = data.node;
  const inRun = data.status !== undefined;
  return (
    <div onClick={() => data.onPick?.(n.key)} css={{
      width: 240, padding: `${theme.spacing.sm}px ${theme.spacing.mid}px`, cursor: data.onPick ? "pointer" : "default",
      opacity: data.dim ? 0.45 : 1,
      background: theme.colors.backgroundPrimary, borderRadius: theme.borders.borderRadiusMd,
      border: `1px solid ${data.selected ? theme.colors.actionPrimaryBackgroundDefault : theme.colors.border}`,
      borderTop: inRun ? `4px solid ${fill(data.status)}` : undefined,
      boxShadow: n.for_each ? `3px 3px 0 -1px ${theme.colors.backgroundPrimary}, 3px 3px 0 0 ${theme.colors.border}` : theme.shadows.xs,
    }}>
      <Handle type="target" position={data.vertical ? Position.Top : Position.Left} css={{ opacity: 0 }} />
      <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: theme.spacing.xs }}>
        <Typography.Text size="lg" ellipsis>{n.label ?? n.key}</Typography.Text>
        {inRun && <StatusIcon state={data.status} />}
      </div>
      {inRun && (
        <Typography.Text color="secondary" size="sm">
          {label[data.status ?? ""] ?? "Pending"}{data.timing?.start ? ` · ${duration(data.timing.start, data.timing.end)}` : ""}
        </Typography.Text>
      )}
      <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.xs, color: theme.colors.textSecondary, marginTop: 2, minWidth: 0 }}>
        <DetailIcon type={n.type} />
        <Typography.Text color="secondary" size="sm" ellipsis title={n.detail}>
          {inRun ? n.detail || (KIND[n.kind] ?? n.kind) : `${KIND[n.kind] ?? n.kind}${n.detail ? `: ${n.detail}` : ""}`}
        </Typography.Text>
      </div>
      <Handle type="source" position={data.vertical ? Position.Bottom : Position.Right} css={{ opacity: 0 }} />
    </div>
  );
}
/** A pipeline condition (dsl.If / Else) as GCP draws one: a frame around the steps it runs, its name and
 *  state in the header. Sized by nestedLayout(). */
function ConditionNode({ data }: { data: Data & { width: number; height: number } }) {
  const { theme } = useDesignSystemTheme();
  const fill = useStateFill();
  const n = data.node;
  const inRun = data.status !== undefined;
  return (
    <div css={{
      width: data.width, height: data.height, opacity: data.dim ? 0.45 : 1, borderRadius: theme.borders.borderRadiusMd,
      border: `1px dashed ${data.selected ? theme.colors.actionPrimaryBackgroundDefault : theme.colors.border}`,
      borderTop: inRun ? `3px solid ${fill(data.status)}` : undefined, background: "transparent",
    }}>
      <Handle type="target" position={Position.Top} css={{ opacity: 0 }} />
      <div onClick={() => data.onPick?.(n.key)} css={{
        display: "flex", alignItems: "center", gap: theme.spacing.xs, padding: `${theme.spacing.xs}px ${theme.spacing.sm}px`,
        cursor: data.onPick ? "pointer" : "default", color: theme.colors.textSecondary, minWidth: 0,
      }}>
        <CodeIcon />
        <Typography.Text ellipsis title={n.label}>{n.label ?? n.key}</Typography.Text>
        {inRun && <StatusIcon state={data.status} />}
        {inRun && (
          <Typography.Text color="secondary" size="sm">
            {label[data.status ?? ""] ?? "Pending"}{data.timing?.start ? ` · ${duration(data.timing.start, data.timing.end)}` : ""}
          </Typography.Text>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} css={{ opacity: 0 }} />
    </div>
  );
}
const nodeTypes = { task: TaskNode, condition: ConditionNode };

export default function TaskGraph({ nodes, edges, status, timing, onPick, selected, height = 380, vertical = false }: {
  nodes: GraphNode[]; edges: { from: string; to: string; outcome: string }[];
  status?: Record<string, string | null>; timing?: Record<string, Timing>;
  onPick?: (key: string) => void; selected?: string | string[] | null; height?: number;
  /** Levels top to bottom, as GCP draws a pipeline; else left to right, as Databricks draws a job. */
  vertical?: boolean;
}) {
  const { theme } = useDesignSystemTheme();
  // A node object without `measured` is hidden until React Flow measures it again, which a page that
  // re-renders every second can leave undone: hand each node back the size it was last measured at.
  const measured = useRef(new Map<string, { width: number; height: number }>());
  const onNodesChange = (changes: NodeChange[]) => changes.forEach((c) => {
    if (c.type === "dimensions" && c.dimensions) measured.current.set(c.id, c.dimensions);
  });
  const flow = useMemo(() => {
    const dataOf = (n: GraphNode): Data => ({
      node: n, status: status ? status[n.key] ?? null : undefined, timing: timing?.[n.key],
      selected: Array.isArray(selected) ? selected.includes(n.key) : selected === n.key,
      dim: Array.isArray(selected) && !selected.includes(n.key), vertical, onPick });
    const edgeOf = (e: { from: string; to: string; outcome: string }): Edge => ({
      id: `${e.from}-${e.to}`, source: e.from, target: e.to, type: "default",
      label: e.outcome ? (e.outcome === "true" ? "True" : "False") : undefined,
      labelStyle: { fill: theme.colors.textSecondary, fontSize: 12 },
      style: { stroke: theme.colors.textPlaceholder },
      markerEnd: { type: MarkerType.ArrowClosed, color: theme.colors.textPlaceholder },
    });
    const withSize = (n: Node): Node => (measured.current.has(n.id) ? { ...n, measured: measured.current.get(n.id) } : n);
    // A pipeline's steps sit in conditions: draw each condition as a frame around its own steps, so a
    // branch's steps stay together, and leave out the edge from a frame to what it holds.
    if (nodes.some((n) => n.parent)) {
      const lay = nestedLayout(nodes, edges);
      const ancestors = (k: string) => {
        const out: string[] = [];
        for (let p = lay.parentOf(k); p; p = lay.parentOf(p)) out.push(p);
        return out;
      };
      const ns: Node[] = lay.order.map((n) => {
        const frame = nodes.some((m) => m.parent === n.key);
        const { w, h } = lay.size.get(n.key)!;
        const { x, y } = lay.at.get(n.key)!;
        const inside = !!n.parent && lay.at.has(n.parent);
        return { id: n.key, type: frame ? "condition" : "task", position: inside ? { x: x + PAD, y: y + HEAD } : { x, y },
                 ...(inside ? { parentId: n.parent!, extent: "parent" as const } : {}),
                 ...(frame ? { style: { width: w, height: h }, zIndex: -1 } : {}),
                 data: frame ? { ...dataOf(n), width: w, height: h } : dataOf(n) };
      });
      const es = edges.filter((e) => !ancestors(e.to).includes(e.from)).map(edgeOf);
      return { ns: ns.map(withSize), es };
    }
    // A level of many parallel tasks wraps into columns of PER_COLUMN, so it stays readable.
    const PER_COLUMN = 4;
    const count: Record<number, number> = {};
    nodes.forEach((n) => { count[n.level] = (count[n.level] ?? 0) + 1; });
    const levels = Object.keys(count).map(Number).sort((a, b) => a - b);
    const startX: Record<number, number> = {};
    levels.reduce((x, l) => { startX[l] = x; return x + Math.ceil(count[l] / PER_COLUMN) * 280 + 40; }, 0);
    const row: Record<number, number> = {};
    const ns: Node[] = nodes.map((n) => {
      const y = (row[n.level] = (row[n.level] ?? -1) + 1);
      const position = vertical ? { x: y * 280, y: n.level * 100 }
        : { x: startX[n.level] + Math.floor(y / PER_COLUMN) * 280, y: (y % PER_COLUMN) * 90 };
      return { id: n.key, type: "task", position, data: dataOf(n) };
    });
    const es: Edge[] = edges.map(edgeOf);
    return { ns: ns.map(withSize), es };
  }, [nodes, edges, status, timing, onPick, selected, theme, vertical]);
  return (
    // Databricks draws the graph straight on the page: no fill, only the dot grid.
    <div css={{ height, "& .react-flow": { "--xy-background-color": "transparent" },
                // Databricks' controls: separate 32px square buttons, 4px apart, 8px in from the corner, fit first.
                "& .react-flow__controls": { display: "flex", flexDirection: "column", gap: 4, margin: 8, boxShadow: "none" },
                "& .react-flow__controls-fitview": { order: -1 },
                "& .react-flow__controls-button": { width: 32, height: 32, padding: 0, borderRadius: 4,
                  background: theme.colors.backgroundPrimary, border: `1px solid ${theme.colors.border}`, color: theme.colors.textSecondary,
                  "&:hover": { background: theme.colors.actionDefaultBackgroundHover, color: theme.colors.actionDefaultTextHover },
                  "& svg": { fill: "currentColor", maxWidth: 12, maxHeight: 12 } } }}>
      <ReactFlow nodes={flow.ns} edges={flow.es} onNodesChange={onNodesChange} nodeTypes={nodeTypes} colorMode={theme.isDarkMode ? "dark" : "light"} fitView fitViewOptions={{ maxZoom: 1, padding: 0.25 }}
                 nodesDraggable={false} nodesConnectable={false} proOptions={{ hideAttribution: true }}>
        <Background gap={8} size={1} color={theme.colors.border} />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
    </div>
  );
}
