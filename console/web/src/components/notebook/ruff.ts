import type { Diagnostic as RuffDiagnostic, Workspace } from "@astral-sh/ruff-wasm-web";
import wasmUrl from "@astral-sh/ruff-wasm-web/ruff_wasm_bg.wasm?url";

/** A ruff finding placed in one cell, by offset into that cell's source. */
export type CellDiagnostic = {
  from: number; to: number; severity: "error" | "warning" | "hint"; message: string; unused: boolean;
  fix?: { label: string; edits: { from: number; to: number; insert: string }[] };
};

// The pyflakes rules and syntax errors: what a notebook editor squiggles, without style nags. F704 is
// await outside a function, which IPython allows at the top level.
const SETTINGS = { "line-length": 120, "indent-width": 4, builtins: ["display", "get_ipython", "dbutils", "spark"],
                   lint: { select: ["E9", "F"], ignore: ["F704"] } };
// Errors stop a run or fail a cell; the rest (unused imports, f-strings without fields...) are warnings.
const ERROR = /^(invalid-syntax|E9|F821|F822|F823|F63|F7)/;

let loading: Promise<typeof import("@astral-sh/ruff-wasm-web") | null> | null = null;
/** Ruff's WebAssembly build, fetched once the first notebook opens; null if the browser can't load it. */
function load() {
  loading ??= import("@astral-sh/ruff-wasm-web")
    .then(async (m) => { await m.default({ module_or_path: wasmUrl }); return m; })
    .catch((e) => { console.warn("ruff didn't load; the editor falls back to syntax errors only", e); return null; });
  return loading;
}

const workspaces = new Map<string, Workspace>();
async function workspace(extraBuiltins: string[] = []): Promise<Workspace | null> {
  const m = await load();
  if (!m) return null;
  const key = extraBuiltins.join(",");
  let ws = workspaces.get(key);
  if (!ws) {
    if (workspaces.size > 8) { for (const w of workspaces.values()) w.free(); workspaces.clear(); }
    ws = new m.Workspace({ ...SETTINGS, builtins: [...SETTINGS.builtins, ...extraBuiltins] }, m.PositionEncoding.Utf16);
    workspaces.set(key, ws);
  }
  return ws;
}

/** Python ruff can read: IPython's %magic, !shell and `x = !cmd` lines become blank lines or `x = None`, line for line. */
function pythonOnly(src: string): string | null {
  if (/^\s*%%/.test(src.split("\n").find((l) => l.trim()) ?? "")) return null;
  return src.split("\n").map((l) => {
    const assigned = /^(\s*)([A-Za-z_]\w*)\s*=\s*[!%]/.exec(l);
    if (assigned) return `${assigned[1]}${assigned[2]} = None`;
    return /^\s*[%!]/.test(l) || /^\s*[\w.]+\?\??\s*$/.test(l) ? "" : l;
  }).join("\n");
}

const lineStarts = (src: string) => {
  const out = [0];
  for (let i = 0; i < src.length; i++) if (src[i] === "\n") out.push(i + 1);
  return out;
};
const offset = (starts: number[], len: number, row: number, col: number) =>
  Math.min(len, (starts[row - 1] ?? len) + col - 1);

const NAMES = /^\s*(?:(?:async\s+)?def|class)\s+(\w+)|^\s*(\w+)\s*(?::[^=\n]+)?=(?!=)|^\s*(?:from\s+\S+\s+)?import\s+(.+)$|^\s*for\s+(\w+)/gm;
/** The names a cell ruff can't parse would have bound, so later cells using them aren't flagged undefined. */
function boundNames(src: string): string[] {
  const out = new Set<string>();
  for (const m of src.matchAll(NAMES)) {
    if (m[1] || m[2] || m[4]) out.add((m[1] || m[2] || m[4])!);
    if (m[3]) for (const part of m[3].replace(/[()]/g, "").split(",")) {
      const name = part.trim().split(/\s+as\s+/).pop()?.split(".")[0]?.trim();
      if (name && /^\w+$/.test(name)) out.add(name);
    }
  }
  return [...out];
}

/** Lints a notebook's code cells as one module, in cell order, as ruff lints an .ipynb; each finding lands in its cell. */
export async function lintNotebook(cells: { key: string; source: string }[]): Promise<Record<string, CellDiagnostic[]> | null> {
  const base = await workspace();
  if (!base) return null;
  const out: Record<string, CellDiagnostic[]> = Object.fromEntries(cells.map((c) => [c.key, []]));
  const parts: { key: string; source: string; text: string; row: number }[] = [];
  const extra = new Set<string>();
  let row = 1;
  for (const c of cells) {
    const text = pythonOnly(c.source);
    if (text === null) continue;
    // A cell that doesn't parse would hide every other cell's findings: it reports its syntax errors alone.
    const own = (base.check(text) as RuffDiagnostic[]).filter((d) => d.code === "invalid-syntax" || d.code?.startsWith("E9"));
    if (own.length) {
      out[c.key] = own.map((d) => place(d, text, c.source, 1));
      boundNames(text).forEach((n) => extra.add(n));
      continue;
    }
    parts.push({ key: c.key, source: c.source, text, row });
    row += text.split("\n").length;
  }
  const ws = extra.size ? await workspace([...extra].sort()) : base;
  if (!ws || !parts.length) return out;
  const found = ws.check(parts.map((p) => p.text).join("\n")) as RuffDiagnostic[];
  for (const d of found) {
    const at = d.start_location.row;
    const part = [...parts].reverse().find((p) => p.row <= at);
    if (!part) continue;
    out[part.key].push(place(d, part.text, part.source, part.row));
  }
  return out;
}

function place(d: RuffDiagnostic, text: string, source: string, firstRow: number): CellDiagnostic {
  const starts = lineStarts(source);
  const rows = text.split("\n").length;
  // A whole-line fix ends at column 1 of the line after; past the cell's last line, that's the cell's end.
  const pos = (r: number, c: number) => (r - firstRow + 1 > rows ? source.length
    : offset(starts, source.length, Math.max(1, r - firstRow + 1), c));
  const from = pos(d.start_location.row, d.start_location.column);
  let to = pos(d.end_location.row, d.end_location.column);
  if (to <= from) to = Math.min(source.length, from + 1);
  const code = d.code ?? "";
  const unused = d.tags?.includes("unnecessary") ?? false;
  const edits = d.fix?.edits.filter((e) => e.location.row >= firstRow && e.location.row < firstRow + rows
    && (e.end_location.row < firstRow + rows || (e.end_location.row === firstRow + rows && e.end_location.column === 1))) ?? [];
  return {
    from, to, unused,
    severity: ERROR.test(code) ? "error" : unused ? "hint" : "warning",
    message: code === "invalid-syntax" ? d.message : `${d.message} (${code})`,
    fix: d.fix && edits.length === d.fix.edits.length && edits.length ? {
      label: d.fix.message ?? "Fix",
      edits: edits.map((e) => ({ from: pos(e.location.row, e.location.column), to: pos(e.end_location.row, e.end_location.column), insert: e.content ?? "" })),
    } : undefined,
  };
}

/** A cell formatted as `ruff format` would, its IPython lines kept as they were; throws when it doesn't parse. */
export async function formatCell(source: string): Promise<string | null> {
  const ws = await workspace();
  if (!ws) return null;
  if (/^\s*%%/.test(source.split("\n").find((l) => l.trim()) ?? "")) return source;
  const kept: string[] = [];
  const masked = source.split("\n").map((l) => {
    if (!/^\s*[%!]/.test(l) && !/^\s*[A-Za-z_]\w*\s*=\s*[!%]/.test(l)) return l;
    kept.push(l);
    return `${/^\s*/.exec(l)![0]}# __mlops_ipython_${kept.length - 1}__`;
  }).join("\n");
  const formatted = ws.format(masked).replace(/\n$/, "");
  return formatted.replace(/^[ \t]*# __mlops_ipython_(\d+)__$/gm, (_m, i: string) => kept[Number(i)]);
}
