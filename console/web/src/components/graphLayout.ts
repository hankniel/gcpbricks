import { GraphNode } from "../api";

const STEP_W = 240, STEP_H = 74, GAP_X = 24, GAP_Y = 40;
/** A frame's inner padding, and its header's height (the condition's name and state). */
export const PAD = 16, HEAD = 40;

/** A pipeline's steps laid out inside their conditions, top to bottom: each condition a frame holding its
 *  own steps, ordered by the dependencies among them; a frame's size follows what it holds. Positions are
 *  relative to the enclosing frame (React Flow's parentId); parents come before their children. */
export function nestedLayout(nodes: GraphNode[], edges: { from: string; to: string }[]) {
  const keys = new Set(nodes.map((n) => n.key));
  const kids = new Map<string, GraphNode[]>();
  nodes.forEach((n) => {
    const p = n.parent && keys.has(n.parent) ? n.parent : "";
    kids.set(p, [...(kids.get(p) ?? []), n]);
  });
  const into = new Map<string, string[]>();
  edges.forEach((e) => into.set(e.to, [...(into.get(e.to) ?? []), e.from]));
  const size = new Map<string, { w: number; h: number }>();
  const at = new Map<string, { x: number; y: number }>();
  const order: GraphNode[] = [];

  function lay(container: string): { w: number; h: number } {
    const own = kids.get(container) ?? [];
    const siblings = new Set(own.map((n) => n.key));
    const level = new Map<string, number>();
    const depth = (k: string): number => {
      if (!level.has(k)) {
        level.set(k, 0);
        const before = (into.get(k) ?? []).filter((f) => siblings.has(f) && f !== k);
        level.set(k, before.length ? 1 + Math.max(...before.map(depth)) : 0);
      }
      return level.get(k)!;
    };
    own.forEach((n) => {
      if (kids.has(n.key)) {
        const inner = lay(n.key);
        size.set(n.key, { w: Math.max(STEP_W, inner.w + 2 * PAD), h: HEAD + inner.h + PAD });
      } else {
        size.set(n.key, { w: STEP_W, h: STEP_H });
      }
    });
    const rows: GraphNode[][] = [];
    own.forEach((n) => { (rows[depth(n.key)] ??= []).push(n); });
    const packed = rows.filter(Boolean);
    const rowW = packed.map((r) => r.reduce((w, n) => w + size.get(n.key)!.w, 0) + GAP_X * (r.length - 1));
    const width = Math.max(0, ...rowW);
    let y = 0;
    packed.forEach((r, i) => {
      let x = (width - rowW[i]) / 2;
      r.forEach((n) => {
        at.set(n.key, { x, y });
        x += size.get(n.key)!.w + GAP_X;
      });
      y += Math.max(...r.map((n) => size.get(n.key)!.h)) + GAP_Y;
    });
    return { w: width, h: Math.max(0, y - GAP_Y) };
  }
  lay("");
  const visit = (container: string) => (kids.get(container) ?? []).forEach((n) => { order.push(n); visit(n.key); });
  visit("");
  return { size, at, order, parentOf: (k: string) => nodes.find((n) => n.key === k)?.parent ?? null };
}
