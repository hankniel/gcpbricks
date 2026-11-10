import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { keyframes } from "@emotion/react";
import {
  Alert, BracketsCurlyIcon, Breadcrumb, Button, CheckCircleFillIcon, ChevronDownIcon, ChevronRightIcon, ClockIcon, DangerFillIcon, DragIcon,
  DropdownMenu, FullscreenIcon, KeyboardIcon, ListBorderIcon, LockIcon, Modal, OverflowIcon, PlayIcon, PlusIcon, SparkleDoubleIcon,
  Spinner, StopIcon, TableSkeleton, Tag, Tooltip, TrashIcon, Typography, WarningFillIcon, XCircleFillIcon, useDesignSystemTheme,
} from "@databricks/design-system";
import {
  api, notebooksApi, runCell, type Cell, type CellOutput, type KernelStatus, type Notebook, type NotebookAccess, type NotebookRun,
  type NotebookRuntime, type RunCell,
} from "../api";
import ClaudeSetup from "../components/ClaudeSetup";
import CodeEditor from "../components/CodeEditor";
import RuntimeDrawer from "../components/RuntimeDrawer";
import ShareModal from "../components/ShareModal";
import NotebookOutputs, { Markdown, text } from "../components/NotebookOutputs";
import { ALT, MAC, MOD, type Action } from "../components/notebook/actions";
import CommandPalette from "../components/notebook/CommandPalette";
import FindBar, { NO_FIND, findRegExp, type FindState } from "../components/notebook/FindBar";
import Shortcuts from "../components/notebook/Shortcuts";
import { type CellDiagnostic, formatCell, lintNotebook } from "../components/notebook/ruff";
import { Contents, Variables, type Heading } from "../components/notebook/SidePanels";
import { useToast } from "../components/Toast";
import { pythonLanguage } from "@codemirror/lang-python";
import { secondsText, useLoad } from "../util";

type Live = Cell & { key: string };
// How a cell's last run in this page went; what's waiting to run is the queue.
type Stat = { state: "running"; start: number } | { state: "ok" | "error"; ms: number };
type Prefs = { numbers: boolean; full: boolean; left: "toc" | null; right: "vars" | null };
const newKey = () => Math.random().toString(36).slice(2);
// nbformat 4.5's cell id: a run record names its cells by it, and it stays put when cells move.
const newId = () => Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) => b.toString(16).padStart(2, "0")).join("");
const blank = (cell_type: "code" | "markdown"): Live =>
  ({ key: newKey(), id: newId(), cell_type, source: "", metadata: {}, ...(cell_type === "code" ? { outputs: [], execution_count: null } : {}) });
// How a run that isn't this page's own reads under its cell: what is happening, and how sure we are of it.
const FINISHED = new Set(["done", "failed", "cancelled"]);
const RUNTIME_KEY = "mlops-console.notebook-runtime";
const PREFS_KEY = "mlops-console.notebook-prefs";
const slide = keyframes({ from: { transform: "translateX(-100%)" }, to: { transform: "translateX(250%)" } });
// eslint-disable-next-line no-control-regex
const ANSI = /\u001b\[[0-9;]*m/g;

function loadPrefs(): Prefs {
  const base: Prefs = { numbers: false, full: false, left: null, right: null };
  try { return { ...base, ...JSON.parse(localStorage.getItem(PREFS_KEY) ?? "{}") }; } catch { return base; }
}
// nbformat's own flags for a hidden input or output, so another Jupyter front end keeps them.
const hidden = (c: Cell, what: "source_hidden" | "outputs_hidden") =>
  !!(c.metadata?.jupyter as Record<string, unknown> | undefined)?.[what];
const withJupyter = (c: Cell, what: "source_hidden" | "outputs_hidden", on: boolean): Cell["metadata"] => {
  const j = { ...((c.metadata?.jupyter as Record<string, unknown>) ?? {}) };
  if (on) j[what] = true; else delete j[what];
  const { jupyter: _, ...rest } = c.metadata ?? {};
  return Object.keys(j).length ? { ...rest, jupyter: j } : rest;
};
const titleOf = (c: Cell) => (typeof c.metadata?.title === "string" ? c.metadata.title : "");

/** A workspace notebook in the console, as Databricks' notebook editor works it: a menu bar and command
 *  palette over cells that are selected and moved in command mode and edited in an IDE-grade editor,
 *  queued and run one by one on the user's own Colab runtime, and saved back to the workspace. */
export default function NotebookEditorPage() {
  const { theme } = useDesignSystemTheme();
  const [params] = useSearchParams();
  const path = params.get("path") ?? "";
  const me = useLoad(api.me, []);
  const access = useLoad(notebooksApi.access, []);
  // A runtime being made or started shows up within seconds; otherwise a slower refresh does.
  const [waiting, setWaiting] = useState<string | null>(null);
  const runtimes = useLoad(notebooksApi.runtimes, [], waiting ? 5000 : 20000);
  const templates = useLoad(notebooksApi.templates, []);
  const [connecting, setConnecting] = useState(false);
  const [cells, setCells] = useState<Live[] | null>(null);
  const [generation, setGeneration] = useState<string | null>(null);
  const [meta, setMeta] = useState<Notebook["metadata"]>({});
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<{ text: string; conflict?: boolean; access?: boolean } | null>(null);
  const [active, setActive] = useState(0);
  // The other end of a Shift-extended selection; the selection is every cell between it and `active`.
  const [anchor, setAnchor] = useState(0);
  const [editingMd, setEditingMd] = useState<string | null>(null);
  const [runtimeId, setRuntimeId] = useState<string>(() => { try { return localStorage.getItem(RUNTIME_KEY) ?? ""; } catch { return ""; } });
  const [kernel, setKernel] = useState<string | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [queue, setQueue] = useState<string[]>([]);
  const [stats, setStats] = useState<Record<string, Stat>>({});
  const [lastRun, setLastRun] = useState<string | null>(null);
  // Every run of this notebook in the last day, whoever made it (another tab, Claude), with how it stands now,
  // and the kernel's own word on itself: what tells a running cell from a hanging runtime or a dead one.
  const [runs, setRuns] = useState<NotebookRun[]>([]);
  const [kstat, setKstat] = useState<KernelStatus | null>(null);
  const [claude, setClaude] = useState(false);
  const [can, setCan] = useState<NotebookAccess>("view");
  const [sharing, setSharing] = useState(false);
  const [prefs, setPrefsState] = useState<Prefs>(loadPrefs);
  const [palette, setPalette] = useState(false);
  const [shortcuts, setShortcuts] = useState(false);
  const [find, setFind] = useState<FindState | null>(null);
  const [findAt, setFindAt] = useState(0);
  const [select, setSelect] = useState<{ key: string; from: number; to: number; n: number; focus?: boolean } | null>(null);
  // The symbol under the cursor: every code cell highlights its uses, as one editor would.
  const [symbol, setSymbol] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<{ from: string; to: string } | null>(null);
  const notify = useToast();
  // Ruff's findings per cell; null until ruff has loaded, when editors fall back to syntax errors only.
  const [lint, setLint] = useState<Record<string, CellDiagnostic[]> | null>(null);
  const [focusReq, setFocusReq] = useState<{ key: string; at: "start" | "end"; n: number } | null>(null);
  const [clip, setClip] = useState<Live[]>([]);
  const [trash, setTrash] = useState<{ at: number; cells: Live[] }[]>([]);
  const [titling, setTitling] = useState<string | null>(null);
  const [full, setFull] = useState<string | null>(null);
  const [drag, setDrag] = useState<{ key: string; over: number } | null>(null);
  const [varsTick, setVarsTick] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const chord = useRef<{ key: string; at: number } | null>(null);
  // A notebook only shared with you to view opens read-only: no edits, no runs, nothing saved.
  const canEdit = can !== "view";
  // The queue runs a cell's source as it is when its turn comes, not when it was queued.
  const cellsRef = useRef(cells);
  cellsRef.current = cells;
  const busy = !!running || queue.length > 0;
  const setPrefs = (p: Partial<Prefs>) => setPrefsState((cur) => {
    const next = { ...cur, ...p };
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(next)); } catch { /* remembered this visit only */ }
    return next;
  });

  const load = useCallback(() => {
    setCells(null); setError(null);
    notebooksApi.read(path).then((r) => {
      setCells(r.notebook.cells.map((c) => ({ ...c, id: c.id ?? newId(), key: newKey() })));
      setMeta(r.notebook.metadata ?? {}); setGeneration(r.generation); setDirty(false); setCan(r.access);
    }, (e: Error) => setError({ text: e.message }));
  }, [path]);
  useEffect(load, [load]);

  // A save made elsewhere (another tab, Claude through mlops-notebooks-mcp) shows here: taken
  // in place while you have nothing unsaved, else offered as a reload.
  useEffect(() => {
    if (!cells || !generation || dirty || saving || busy || error?.conflict) return;
    const t = setInterval(() => {
      notebooksApi.generation(path).then((g) => {
        if (!g.generation || g.generation === generation) return;
        notebooksApi.read(path).then((r) => {
          setCells((cs) => r.notebook.cells.map((c, i) => ({ ...c, id: c.id ?? newId(), key: cs?.[i]?.key ?? newKey() })));
          setMeta(r.notebook.metadata ?? {}); setGeneration(r.generation); setCan(r.access);
        }, () => { /* the next poll tries again */ });
      }, () => { /* the next poll tries again */ });
    }, 4000);
    return () => clearInterval(t);
  }, [cells, generation, dirty, saving, busy, error?.conflict, path]);

  const mine = (runtimes.data ?? []).filter((r) => r.user === me.data?.email);
  const rt: NotebookRuntime | undefined = mine.find((r) => r.id === runtimeId) ?? mine.find((r) => r.state === "RUNNING") ?? mine[0];
  useEffect(() => { setKernel(null); }, [rt?.id]);
  // A kernel already up on the runtime (another tab, Claude) is the one this page shares.
  useEffect(() => {
    if (!rt || rt.state !== "RUNNING" || kernel || !access.data?.allowed) return;
    notebooksApi.kernels(rt.id).then((ks) => {
      const k = ks.find((x) => x.name.startsWith("python"));
      if (k) setKernel((cur) => cur ?? k.id);
    }, () => { /* the first run connects instead */ });
  }, [rt?.id, rt?.state, kernel, access.data?.allowed]); // eslint-disable-line react-hooks/exhaustive-deps
  // A runtime made from the connect panel is picked once it's listed, and polled until it runs.
  useEffect(() => {
    const made = waiting ? mine.find((r) => r.name === waiting || r.id === waiting) : undefined;
    if (!made) return;
    if (made.id !== runtimeId) pickRuntime(made.id);
    if (made.state === "RUNNING") setWaiting(null);
  }, [waiting, runtimes.data]); // eslint-disable-line react-hooks/exhaustive-deps

  // The notebook's runs, polled while any is live (every 3 s) and slowly otherwise: a cell Claude or another
  // tab runs shows here with its outputs so far, and one whose runner, kernel or runtime is gone says so.
  const liveRuns = runs.some((r) => !FINISHED.has(r.live));
  useEffect(() => {
    if (!path) return;
    let stop = false;
    const tick = () => notebooksApi.runs(path).then((r) => { if (!stop) setRuns(r.runs); }, () => { /* the next poll tries again */ });
    tick();
    const t = setInterval(tick, liveRuns || busy ? 3000 : 15000);
    return () => { stop = true; clearInterval(t); };
  }, [path, liveRuns, busy]);
  // The kernel's own execution_state: busy with nothing this page runs is still busy, and dead is dead.
  useEffect(() => {
    if (!rt || rt.state !== "RUNNING" || !kernel || !access.data?.allowed) { setKstat(null); return; }
    let stop = false;
    const tick = () => notebooksApi.kernelStatus(rt.id, kernel).then((s) => { if (!stop) setKstat(s); },
                                                                    () => { if (!stop) setKstat(null); });
    tick();
    const t = setInterval(tick, 5000);
    return () => { stop = true; clearInterval(t); };
  }, [rt?.id, rt?.state, kernel, access.data?.allowed]); // eslint-disable-line react-hooks/exhaustive-deps
  // The newest run naming each cell (runs come newest first).
  const lastRunOf = useMemo(() => {
    const m: Record<string, { run: NotebookRun; cell: RunCell }> = {};
    for (const r of runs) for (const c of r.cells) if (!m[c.id]) m[c.id] = { run: r, cell: c };
    return m;
  }, [runs]);

  const save = useCallback(async () => {
    if (!cells || !canEdit) return;
    setSaving(true);
    try {
      const nb: Notebook = { nbformat: 4, nbformat_minor: 5, metadata: meta, cells: cells.map(({ key: _, ...c }) => c) };
      const r = await notebooksApi.save(path, nb, generation);
      setGeneration(r.generation); setDirty(false);
    } catch (e) {
      const m = (e as Error).message;
      setError({ text: m, conflict: m.includes("changed since you opened it") });
    } finally { setSaving(false); }
  }, [cells, meta, path, generation, canEdit]);

  useEffect(() => {
    if (!dirty || saving || error?.conflict) return;
    const t = setTimeout(save, 2000);
    return () => clearTimeout(t);
  }, [dirty, cells, saving, save, error?.conflict]);

  // Lints the code cells as one module a moment after the last edit, as Databricks' editor does.
  useEffect(() => {
    if (!cells) return;
    let stale = false;
    const t = setTimeout(() => {
      lintNotebook(cells.filter((c) => c.cell_type === "code").map((c) => ({ key: c.key, source: text(c.source) })))
        .then((r) => { if (!stale && r) setLint(r); }, () => { /* the next edit lints again */ });
    }, 300);
    return () => { stale = true; clearTimeout(t); };
  }, [cells]);

  const edit = (key: string, change: Partial<Cell>) => {
    if (!canEdit) return;
    setCells((cs) => cs!.map((c) => (c.key === key ? { ...c, ...change } : c)));
    setDirty(true);
  };
  const structure = (f: (cs: Live[]) => Live[]) => { if (canEdit) { setCells((cs) => f(cs!)); setDirty(true); } };

  // --- selection and focus ----------------------------------------------------------------------
  const lo = Math.min(active, anchor), hi = Math.max(active, anchor);
  const selected = (cells ?? []).slice(lo, hi + 1).map((c) => c.key);
  const reveal = (key: string) => requestAnimationFrame(() =>
    document.getElementById(`nb-cell-${key}`)?.scrollIntoView({ block: "nearest", behavior: "smooth" }));
  /** Selects cell `i` in command mode; with `extend`, keeps the anchor so the range grows. */
  const pick = (i: number, extend = false) => {
    if (!cells?.length) return;
    const at = Math.max(0, Math.min(cells.length - 1, i));
    setActive(at);
    if (!extend) setAnchor(at);
    reveal(cells[at].key);
  };
  const commandMode = () => {
    (document.activeElement as HTMLElement | null)?.blur();
    setEditingMd(null);
    root.current?.focus({ preventScroll: true });
  };
  const editCell = (i: number, at: "start" | "end" = "end") => {
    const c = cells?.[i];
    if (!c || !canEdit) return;
    setActive(i); setAnchor(i);
    if (hidden(c, "source_hidden")) edit(c.key, { metadata: withJupyter(c, "source_hidden", false) });
    if (c.cell_type === "markdown") setEditingMd(c.key);
    setFocusReq({ key: c.key, at, n: Date.now() });
  };

  const insert = (at: number, type: "code" | "markdown", focus = true) => {
    if (!canEdit) return;
    const c = blank(type);
    structure((cs) => [...cs.slice(0, at), c, ...cs.slice(at)]);
    setActive(at); setAnchor(at);
    if (type === "markdown") setEditingMd(c.key);
    if (focus) setFocusReq({ key: c.key, at: "start", n: Date.now() });
    reveal(c.key);
  };

  // --- running ----------------------------------------------------------------------------------
  const connect = async (): Promise<string> => {
    if (!rt) { setConnecting(true); throw new Error("Connect the notebook to a runtime first."); }
    if (rt.state !== "RUNNING") throw new Error(`${rt.name} is ${rt.state.toLowerCase() || "not running"}: start it first.`);
    if (kernel) return kernel;
    const ks = await notebooksApi.kernels(rt.id);
    const k = ks.find((x) => x.name.startsWith("python")) ?? await notebooksApi.startKernel(rt.id);
    setKernel(k.id);
    return k.id;
  };

  const fail = (e: unknown) => {
    const err = e as Error & { status?: number };
    setError({ text: err.message, access: err.status === 401 || /notebook access/.test(err.message) });
  };

  /** Queues code cells to run after what's already queued, as VS Code does. */
  const enqueue = (keys: string[]) => {
    if (!canEdit) return;
    const code = new Set((cellsRef.current ?? []).filter((c) => c.cell_type === "code").map((c) => c.key));
    setQueue((q) => [...q, ...keys.filter((k) => code.has(k) && k !== running && !q.includes(k))]);
  };

  const execute = async (key: string): Promise<boolean> => {
    const cell = cellsRef.current?.find((c) => c.key === key);
    if (!cell) return true;
    setRunning(key); setError(null); setLastRun(key);
    let ok = false;
    let start = 0;
    try {
      const k = await connect();
      start = Date.now();
      setStats((s) => ({ ...s, [key]: { state: "running", start } }));
      edit(key, { outputs: [], execution_count: null });
      const index = cellsRef.current?.findIndex((c) => c.key === key) ?? -1;
      await runCell(rt!.id, k, text(cell.source), (m) => {
        if (m.output_type === "done") {
          ok = m.status === "ok";
          setCells((cs) => cs!.map((c) => (c.key === key ? { ...c, execution_count: m.execution_count } : c)));
        } else if (m.output_type === "clear_output") {
          setCells((cs) => cs!.map((c) => (c.key === key ? { ...c, outputs: [] } : c)));
        } else if (m.output_type !== "heartbeat") {
          setCells((cs) => cs!.map((c) => (c.key === key ? { ...c, outputs: merge(c.outputs ?? [], m) } : c)));
        }
      }, cell.id && index >= 0 ? { path, cell_id: cell.id, index } : undefined);
    } catch (e) { fail(e); ok = false; } finally {
      if (start) setStats((s) => ({ ...s, [key]: { state: ok ? "ok" : "error", ms: Date.now() - start } }));
      setRunning(null); setDirty(true); setVarsTick((n) => n + 1);
    }
    return ok;
  };

  // One cell at a time; a failed cell drops the rest of the queue, as Jupyter does.
  useEffect(() => {
    if (running || !queue.length) return;
    const [key, ...rest] = queue;
    setQueue(rest);
    execute(key).then((ok) => { if (!ok) setQueue([]); });
  }, [queue, running]); // eslint-disable-line react-hooks/exhaustive-deps

  const kernelAction = async (action: "interrupt" | "restart") => {
    setQueue([]);
    if (!rt || !kernel) return;
    try { await notebooksApi.kernelAction(rt.id, kernel, action); } catch (e) { fail(e); }
    if (action === "restart") { setStats({}); setVarsTick((n) => n + 1); }
  };

  const pickRuntime = (id: string) => {
    setRuntimeId(id);
    try { localStorage.setItem(RUNTIME_KEY, id); } catch { /* remembered this visit only */ }
  };

  // The kernel's completions and docstrings, once a kernel is up; the editor falls back to the notebook's words.
  const live = !!(rt && kernel && rt.state === "RUNNING" && access.data?.allowed);
  const complete = useMemo(() => (live ? (code: string, pos: number) => notebooksApi.complete(rt!.id, kernel!, code, pos) : undefined),
    [live, rt?.id, kernel]); // eslint-disable-line react-hooks/exhaustive-deps
  const inspect = useMemo(() => (live ? (code: string, pos: number) => notebooksApi.inspect(rt!.id, kernel!, code, pos)
    .then((r) => (r.found ? r.text.replace(ANSI, "") : null)) : undefined), [live, rt?.id, kernel]); // eslint-disable-line react-hooks/exhaustive-deps
  const words = useCallback(() => {
    const seen = new Set<string>();
    for (const c of cellsRef.current ?? []) {
      if (c.cell_type !== "code") continue;
      for (const m of text(c.source).matchAll(/\b[A-Za-z_]\w{2,}\b/g)) { seen.add(m[0]); if (seen.size > 3000) return [...seen]; }
    }
    return [...seen];
  }, []);

  // --- cell commands ------------------------------------------------------------------------------
  const keys = (cells ?? []).map((c) => c.key);
  const remove = (ks: string[]) => {
    if (!cells || !canEdit || !ks.length) return;
    const at = cells.findIndex((c) => ks.includes(c.key));
    setTrash((t) => [...t.slice(-19), { at, cells: cells.filter((c) => ks.includes(c.key)) }]);
    const rest = cells.filter((c) => !ks.includes(c.key));
    structure(() => (rest.length ? rest : [blank("code")]));
    setQueue((q) => q.filter((k) => !ks.includes(k)));
    const next = Math.max(0, Math.min(at, rest.length - 1));
    setActive(next); setAnchor(next);
  };
  const undoDelete = () => {
    const last = trash[trash.length - 1];
    if (!last || !canEdit) return;
    setTrash((t) => t.slice(0, -1));
    structure((cs) => [...cs.slice(0, last.at), ...last.cells, ...cs.slice(last.at)]);
    setActive(last.at); setAnchor(last.at + last.cells.length - 1);
  };
  const copy = (cut = false) => {
    if (!cells) return;
    const picked = cells.filter((c) => selected.includes(c.key));
    setClip(picked);
    navigator.clipboard?.writeText(picked.map((c) => text(c.source)).join("\n\n")).catch(() => { /* the cell clipboard still has them */ });
    if (cut) remove(picked.map((c) => c.key));
  };
  const paste = () => {
    if (!clip.length || !canEdit || !cells) return;
    const fresh = clip.map((c) => ({ ...c, key: newKey(), id: newId() }));
    const at = hi + 1;
    structure((cs) => [...cs.slice(0, at), ...fresh, ...cs.slice(at)]);
    setActive(at + fresh.length - 1); setAnchor(at);
  };
  const move = (dir: -1 | 1) => {
    if (!cells || !canEdit) return;
    const from = dir < 0 ? lo : hi;
    const to = from + dir;
    if (to < 0 || to >= cells.length) return;
    structure((cs) => {
      const block = cs.slice(lo, hi + 1);
      const rest = [...cs.slice(0, lo), ...cs.slice(hi + 1)];
      const at = lo + dir;
      return [...rest.slice(0, at), ...block, ...rest.slice(at)];
    });
    setActive(active + dir); setAnchor(anchor + dir);
    reveal(cells[to].key);
  };
  const toType = (type: "code" | "markdown") => {
    if (!canEdit) return;
    structure((cs) => cs.map((c) => (!selected.includes(c.key) || c.cell_type === type ? c
      : type === "markdown" ? { ...c, cell_type: "markdown", outputs: undefined, execution_count: undefined }
        : { ...c, cell_type: "code", outputs: [], execution_count: null })));
  };
  const clearOutputs = (ks: string[]) => {
    structure((cs) => cs.map((c) => (ks.includes(c.key) && c.cell_type === "code" ? { ...c, outputs: [], execution_count: null } : c)));
    setStats((s) => Object.fromEntries(Object.entries(s).filter(([k]) => !ks.includes(k))));
  };
  const setHidden = (ks: string[], what: "source_hidden" | "outputs_hidden", on: boolean) =>
    structure((cs) => cs.map((c) => (ks.includes(c.key) ? { ...c, metadata: withJupyter(c, what, on) } : c)));
  const goto = (key: string | undefined | null) => {
    const i = keys.indexOf(key ?? "");
    if (i >= 0) pick(i);
  };
  const failed = (cells ?? []).find((c) => stats[c.key]?.state === "error" || (c.outputs ?? []).some((o) => o.output_type === "error"));
  const restartThen = async (andRun: boolean, andClear: boolean) => {
    await kernelAction("restart");
    if (andClear) clearOutputs(keys);
    if (andRun) enqueue(keys);
  };
  const download = () => {
    if (!cells) return;
    const nb: Notebook = { nbformat: 4, nbformat_minor: 5, metadata: meta, cells: cells.map(({ key: _, ...c }) => c) };
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(nb, null, 1)], { type: "application/x-ipynb+json" }));
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  // --- find and replace ---------------------------------------------------------------------------
  const findRe = useMemo(() => (find ? findRegExp(find) : null), [find]);
  const matches = useMemo(() => {
    if (!findRe || !cells) return [] as { key: string; from: number; to: number }[];
    const out: { key: string; from: number; to: number }[] = [];
    for (const c of cells) {
      if (c.cell_type === "raw") continue;
      for (const m of text(c.source).matchAll(findRe)) {
        if (!m[0].length) break;
        out.push({ key: c.key, from: m.index!, to: m.index! + m[0].length });
        if (out.length > 5000) return out;
      }
    }
    return out;
  }, [findRe, cells]);
  const showMatch = (i: number) => {
    const m = matches[i];
    if (!m) return;
    const c = cells!.find((x) => x.key === m.key)!;
    if (hidden(c, "source_hidden")) edit(c.key, { metadata: withJupyter(c, "source_hidden", false) });
    if (c.cell_type === "markdown") setEditingMd(c.key);
    setFindAt(i);
    setActive(keys.indexOf(m.key)); setAnchor(keys.indexOf(m.key));
    setSelect({ ...m, n: Date.now() });
    reveal(m.key);
  };
  useEffect(() => { if (findAt >= matches.length) setFindAt(0); }, [matches.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const step = (dir: 1 | -1) => matches.length && showMatch((findAt + dir + matches.length) % matches.length);
  const replaceOne = () => {
    const m = matches[findAt];
    if (!m || !find || !findRe || !canEdit) return;
    const c = cells!.find((x) => x.key === m.key)!;
    const src = text(c.source);
    const one = new RegExp(findRe.source, findRe.flags.replace("g", ""));
    edit(c.key, { source: src.slice(0, m.from) + src.slice(m.from, m.to).replace(one, find.replace) + src.slice(m.to) });
  };
  const replaceAll = () => {
    if (!find || !findRe || !canEdit) return;
    const touched = new Set(matches.map((m) => m.key));
    structure((cs) => cs.map((c) => (touched.has(c.key) ? { ...c, source: text(c.source).replace(findRe, find.replace) } : c)));
  };
  // --- format -------------------------------------------------------------------------------------
  /** Formats code cells as `ruff format` would; a cell that doesn't parse is left as it is and named in a toast. */
  const format = async (ks: string[]) => {
    if (!cells || !canEdit) return;
    const targets = cells.filter((c) => ks.includes(c.key) && c.cell_type === "code");
    const done: Record<string, string> = {};
    const broken: number[] = [];
    for (const c of targets) {
      try {
        const out = await formatCell(text(c.source));
        if (out === null) { notify({ title: "The formatter didn't load", severity: "error" }); return; }
        if (out !== text(c.source)) done[c.key] = out;
      } catch { broken.push(keys.indexOf(c.key) + 1); }
    }
    if (Object.keys(done).length) structure((cs) => cs.map((c) => (done[c.key] !== undefined ? { ...c, source: done[c.key] } : c)));
    if (broken.length) notify({ title: `Cell${broken.length > 1 ? "s" : ""} ${broken.join(", ")} didn't format`, description: "Fix the syntax error first.", severity: "warning" });
  };

  // --- go to definition and rename ------------------------------------------------------------------
  /** Where `word` is bound: a def or class first, then an assignment, import, loop or with target, in cell order. */
  const definition = (word: string) => {
    if (!cells) return;
    const w = word.replace(/[^\w]/g, "");
    const kinds = [
      new RegExp(`^[ \\t]*(?:async[ \\t]+)?(?:def|class)[ \\t]+(${w})\\b`, "m"),
      new RegExp(`^[ \\t]*(?:[\\w, \\t*]*,[ \\t]*)?(${w})(?:[ \\t]*,[\\w, \\t*]*)?[ \\t]*(?::[^=\\n]+)?=(?!=)`, "m"),
      new RegExp(`^[ \\t]*(?:from[ \\t]+\\S+[ \\t]+)?import[ \\t]+(?:.*[ \\t,(])?(?:\\S+[ \\t]+as[ \\t]+)?(${w})\\b`, "m"),
      new RegExp(`^[ \\t]*(?:for|with)[ \\t]+(?:.*\\b(?:as[ \\t]+))?(${w})\\b`, "m"),
    ];
    for (const re of kinds) {
      for (const c of cells) {
        if (c.cell_type !== "code") continue;
        const m = re.exec(text(c.source));
        if (!m) continue;
        // The name's first whole-word use on the matched line is the binding itself in each of these forms.
        const start = m.index + m[0].search(new RegExp(`(?<![\\w.])${w}(?!\\w)`));
        if (hidden(c, "source_hidden")) edit(c.key, { metadata: withJupyter(c, "source_hidden", false) });
        const i = keys.indexOf(c.key);
        setActive(i); setAnchor(i);
        setSelect({ key: c.key, from: start, to: start + w.length, n: Date.now(), focus: true });
        reveal(c.key);
        return;
      }
    }
    notify({ title: `No definition of ${w} in this notebook`, description: "It may come from an import or the runtime.", severity: "info" });
  };
  /** Renames `from` in every code cell: whole names only, not attributes (x.from), strings or comments. */
  const rename = (from: string, to: string) => {
    if (!cells || !canEdit || !/^[A-Za-z_]\w*$/.test(to) || from === to) return;
    let n = 0;
    structure((cs) => cs.map((c) => {
      if (c.cell_type !== "code") return c;
      const src = text(c.source);
      const tree = pythonLanguage.parser.parse(src);
      const out = src.replace(new RegExp(`(?<![\\w.])${from}(?!\\w)`, "g"), (m, at: number) => {
        if (/String|Comment/.test(tree.resolveInner(at, 1).name)) return m;
        n++;
        return to;
      });
      return out === src ? c : { ...c, source: out };
    }));
    setSymbol(to);
    setTimeout(() => notify({ title: `Renamed ${n} use${n === 1 ? "" : "s"} of ${from} to ${to}`, severity: "success" }), 0);
  };

  const openFind = (withReplace: boolean) => setFind((f) => ({ ...(f ?? NO_FIND), withReplace: withReplace && canEdit, n: Date.now() }));

  // --- the action list: menus, palette and keys all run these ---------------------------------------
  const folder = path.split("/").slice(0, -1).join("/");
  const name = path.split("/").pop() ?? path;
  const one = selected.length > 1 ? "cells" : "cell";
  const actions: Action[] = [
    { id: "save", group: "File", label: "Save now", keys: `${MOD} + S`, run: save, disabled: !canEdit },
    { id: "share", group: "File", label: "Share...", run: () => setSharing(true), disabled: !(can === "manage" && path.startsWith("Users/")) },
    { id: "download", group: "File", label: "Download as .ipynb", run: download, disabled: !cells, section: "export" },
    { id: "copypath", group: "File", label: "Copy path", run: () => navigator.clipboard?.writeText(`/Workspace/${path}`), section: "export" },
    { id: "claude", group: "File", label: "Use with Claude...", run: () => setClaude(true), section: "claude" },
    { id: "undo", group: "Edit", label: "Undo delete cells", keys: "Z", run: undoDelete, disabled: !trash.length || !canEdit },
    { id: "cut", group: "Edit", label: `Cut ${one}`, keys: `${MOD} + X`, run: () => copy(true), disabled: !canEdit, section: "clip" },
    { id: "copy", group: "Edit", label: `Copy ${one}`, keys: `${MOD} + C`, run: () => copy(), section: "clip" },
    { id: "paste", group: "Edit", label: "Paste cells below", keys: `${MOD} + V`, run: paste, disabled: !clip.length || !canEdit, section: "clip" },
    { id: "delete", group: "Edit", label: `Delete ${one}`, keys: "D , D", run: () => remove(selected), disabled: !canEdit, section: "clip" },
    { id: "above", group: "Edit", label: "Insert cell above", keys: "A", run: () => insert(lo, "code", false), disabled: !canEdit, section: "insert" },
    { id: "below", group: "Edit", label: "Insert cell below", keys: "B", run: () => insert(hi + 1, "code", false), disabled: !canEdit, section: "insert" },
    { id: "up", group: "Edit", label: "Move cell up", keys: `Ctrl + ${ALT} + ↑`, run: () => move(-1), disabled: !canEdit || lo === 0, section: "insert" },
    { id: "down", group: "Edit", label: "Move cell down", keys: `Ctrl + ${ALT} + ↓`, run: () => move(1),
      disabled: !canEdit || hi >= (cells?.length ?? 1) - 1, section: "insert" },
    { id: "all", group: "Edit", label: "Select all cells", keys: `${MOD} + A`, run: () => { setAnchor(0); setActive((cells?.length ?? 1) - 1); }, section: "insert" },
    { id: "tocode", group: "Edit", label: "Change to code", keys: "Y", run: () => toType("code"), disabled: !canEdit, section: "type" },
    { id: "tomd", group: "Edit", label: "Change to markdown", keys: "M", run: () => toType("markdown"), disabled: !canEdit, section: "type" },
    { id: "title", group: "Edit", label: "Add title", keys: "T", run: () => cells?.[active] && setTitling(cells[active].key), disabled: !canEdit, section: "type" },
    { id: "definition", group: "Edit", label: symbol ? `Go to definition of ${symbol}` : "Go to definition", keys: "F12",
      run: () => symbol && definition(symbol), disabled: !symbol, section: "symbol" },
    { id: "rename", group: "Edit", label: symbol ? `Rename ${symbol}...` : "Rename symbol...", keys: "F2",
      run: () => symbol && setRenaming({ from: symbol, to: symbol }), disabled: !symbol || !canEdit, section: "symbol" },
    { id: "format", group: "Edit", label: `Format ${one}`, keys: `${MOD} + Shift + F`, run: () => format(selected), disabled: !canEdit, section: "format" },
    { id: "formatall", group: "Edit", label: "Format notebook", run: () => format(keys), disabled: !canEdit, section: "format" },
    { id: "find", group: "Edit", label: "Find...", keys: `${MOD} + F`, run: () => openFind(false), section: "find" },
    { id: "replace", group: "Edit", label: "Replace...", keys: `Ctrl + ${ALT} + F`, run: () => openFind(true), disabled: !canEdit, section: "find" },
    { id: "numbers", group: "View", label: "Line numbers", keys: "L", checked: prefs.numbers, run: () => setPrefs({ numbers: !prefs.numbers }) },
    { id: "layout", group: "View", label: "Full width", checked: prefs.full, run: () => setPrefs({ full: !prefs.full }) },
    { id: "collapse", group: "View", label: "Collapse all cells", run: () => setHidden(keys.filter((k) => cells?.find((c) => c.key === k)?.cell_type === "code"), "source_hidden", true),
      disabled: !canEdit, section: "fold" },
    { id: "expand", group: "View", label: "Expand all cells", run: () => setHidden(keys, "source_hidden", false), disabled: !canEdit, section: "fold" },
    { id: "hideout", group: "View", label: "Collapse all results", run: () => setHidden(keys, "outputs_hidden", true), disabled: !canEdit, section: "fold" },
    { id: "showout", group: "View", label: "Expand all results", run: () => setHidden(keys, "outputs_hidden", false), disabled: !canEdit, section: "fold" },
    { id: "toc", group: "View", label: "Table of contents", checked: prefs.left === "toc", run: () => setPrefs({ left: prefs.left ? null : "toc" }), section: "panel" },
    { id: "vars", group: "View", label: "Variables", checked: prefs.right === "vars", run: () => setPrefs({ right: prefs.right ? null : "vars" }), section: "panel" },
    { id: "runall", group: "Run", label: "Run all", keys: `${ALT} + Shift + Enter`, run: () => enqueue(keys), disabled: !canEdit || !cells },
    { id: "runabove", group: "Run", label: "Run all above", keys: `${ALT} + Shift + ↑`, run: () => enqueue(keys.slice(0, lo)), disabled: !canEdit || lo === 0 },
    { id: "runbelow", group: "Run", label: "Run all below", keys: `${ALT} + Shift + ↓`, run: () => enqueue(keys.slice(lo)), disabled: !canEdit },
    { id: "runsel", group: "Run", label: `Run selected ${one}`, keys: `${MOD} + Enter`, run: () => enqueue(selected), disabled: !canEdit },
    { id: "interrupt", group: "Run", label: "Interrupt execution", keys: "I , I", run: () => kernelAction("interrupt"), disabled: !busy, section: "kernel" },
    { id: "clearsel", group: "Run", label: "Clear selected outputs", run: () => clearOutputs(selected), disabled: !canEdit, section: "clear" },
    { id: "clearall", group: "Run", label: "Clear all outputs", run: () => clearOutputs(keys), disabled: !canEdit, section: "clear" },
    { id: "state", group: "Run", label: "Clear state", keys: "0 , 0", run: () => restartThen(false, false), disabled: !kernel, section: "clear" },
    { id: "stateout", group: "Run", label: "Clear state and outputs", run: () => restartThen(false, true), disabled: !kernel || !canEdit, section: "clear" },
    { id: "staterun", group: "Run", label: "Clear state and run all", run: () => restartThen(true, false), disabled: !kernel || !canEdit, section: "clear" },
    { id: "last", group: "Run", label: "Go to last run cell", run: () => goto(lastRun), disabled: !lastRun, section: "go" },
    { id: "failed", group: "Run", label: "Go to first failed cell", run: () => goto(failed?.key), disabled: !failed, section: "go" },
    { id: "shortcuts", group: "Help", label: "Keyboard shortcuts", keys: "H", run: () => setShortcuts(true) },
    { id: "palette", group: "Help", label: "Command palette", keys: `${MOD} + Shift + P`, run: () => setPalette(true) },
  ];
  const act = (id: string) => { const a = actions.find((x) => x.id === id); if (a && !a.disabled) a.run(); };

  // --- keys --------------------------------------------------------------------------------------
  const keyRef = useRef<(e: KeyboardEvent) => void>(() => {});
  keyRef.current = (e: KeyboardEvent) => {
    if (!cells) return;
    const mod = MAC ? e.metaKey : e.ctrlKey;
    const target = e.target as HTMLElement;
    const inEditor = !!target.closest?.(".cm-editor");
    const inField = !inEditor && (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) || target.isContentEditable);
    const dialog = !!document.querySelector("[role=dialog]");
    const stop = () => { e.preventDefault(); e.stopPropagation(); };
    if (mod && e.code === "KeyS") { stop(); save(); return; }
    if (mod && e.shiftKey && e.code === "KeyP") { stop(); setPalette((p) => !p); return; }
    if (dialog) return;
    if (mod && e.shiftKey && e.code === "KeyF") { stop(); act("format"); return; }
    if (mod && !e.shiftKey && e.code === "KeyF") { stop(); openFind(false); return; }
    if (e.ctrlKey && e.altKey && e.code === "KeyF") { stop(); openFind(true); return; }
    if (e.altKey && e.shiftKey && e.key === "Enter") { stop(); act("runall"); return; }
    if (e.altKey && e.shiftKey && e.key === "ArrowUp") { stop(); act("runabove"); return; }
    if (e.altKey && e.shiftKey && e.key === "ArrowDown") { stop(); act("runbelow"); return; }
    if (e.ctrlKey && e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) { stop(); move(e.key === "ArrowUp" ? -1 : 1); return; }
    if (inEditor || inField) return;
    // Command mode: the notebook has focus, no editor does.
    const k = e.key.toLowerCase();
    const twice = (key: string) => {
      const now = Date.now();
      const hit = chord.current?.key === key && now - chord.current.at < 600;
      chord.current = hit ? null : { key, at: now };
      return hit;
    };
    if (mod && e.key === "Enter") { stop(); act("runsel"); return; }
    if (e.shiftKey && e.key === "Enter") {
      stop(); enqueue([cells[active].key]);
      if (active === cells.length - 1) insert(cells.length, "code", false); else pick(active + 1);
      return;
    }
    if (mod && e.code === "KeyA") { stop(); act("all"); return; }
    if (mod && e.code === "KeyC") { stop(); act("copy"); return; }
    if (mod && e.code === "KeyX") { stop(); act("cut"); return; }
    if (mod && e.code === "KeyV") { stop(); act("paste"); return; }
    if (mod && e.code === "KeyZ") { stop(); act("undo"); return; }
    if (mod || e.altKey) return;
    if (e.key === "Enter") { stop(); editCell(active); return; }
    if (e.key === "ArrowUp" || k === "k") { stop(); pick(active - 1, e.shiftKey); return; }
    if (e.key === "ArrowDown" || k === "j") { stop(); pick(active + 1, e.shiftKey); return; }
    if (e.key === "Escape") { if (find) { stop(); setFind(null); } return; }
    const single: Record<string, string> = { a: "above", b: "below", y: "tocode", m: "tomd", t: "title", l: "numbers", h: "shortcuts", z: "undo" };
    if (single[k]) { stop(); act(single[k]); return; }
    if (k === "d") { stop(); if (twice("d")) act("delete"); return; }
    if (k === "i") { stop(); if (twice("i")) act("interrupt"); return; }
    if (e.key === "0") { stop(); if (twice("0")) act("state"); }
  };
  useEffect(() => {
    const on = (e: KeyboardEvent) => keyRef.current(e);
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  // --- drag to reorder -------------------------------------------------------------------------
  const dropAt = (over: number) => {
    if (!drag || !cells) return;
    const from = keys.indexOf(drag.key);
    setDrag(null);
    if (from < 0 || over === from || over === from + 1) return;
    structure((cs) => {
      const c = cs[from];
      const rest = cs.filter((_, i) => i !== from);
      const at = over > from ? over - 1 : over;
      return [...rest.slice(0, at), c, ...rest.slice(at)];
    });
    const at = over > from ? over - 1 : over;
    setActive(at); setAnchor(at);
  };

  const headings: Heading[] = useMemo(() => (cells ?? []).flatMap((c) => c.cell_type !== "markdown" ? []
    : text(c.source).split("\n").flatMap((l) => {
      const m = /^(#{1,6})\s+(.+?)\s*#*$/.exec(l);
      return m ? [{ key: c.key, level: m[1].length, text: m[2].replace(/[*_`]/g, "") }] : [];
    })), [cells]);

  // --- render ------------------------------------------------------------------------------------
  const line = `1px solid ${theme.colors.border}`;
  const blue = theme.colors.actionDefaultBorderFocus;
  const menu = (group: Action["group"]) => {
    const items = actions.filter((a) => a.group === group);
    return (
      <DropdownMenu.Root key={group} modal={false}>
        <DropdownMenu.Trigger asChild>
          <button type="button" css={{ all: "unset", cursor: "pointer", height: 28, padding: "0 8px", borderRadius: 4, color: theme.colors.textPrimary,
                                       "&:hover, &[data-state=open]": { background: theme.colors.actionDefaultBackgroundHover } }}>{group}</button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Content align="start" minWidth={260}>
          {items.map((a, i) => (
            <span key={a.id}>
              {i > 0 && a.section !== items[i - 1].section && <DropdownMenu.Separator />}
              {a.checked !== undefined ? (
                <DropdownMenu.CheckboxItem componentId={`nb.menu.${a.id}`} checked={a.checked} disabled={a.disabled} onCheckedChange={() => a.run()}>
                  <DropdownMenu.ItemIndicator />{a.label}{a.keys && <DropdownMenu.HintColumn>{a.keys}</DropdownMenu.HintColumn>}
                </DropdownMenu.CheckboxItem>
              ) : (
                <DropdownMenu.Item componentId={`nb.menu.${a.id}`} disabled={a.disabled} onClick={() => a.run()}>
                  {a.label}{a.keys && <DropdownMenu.HintColumn>{a.keys}</DropdownMenu.HintColumn>}
                </DropdownMenu.Item>
              )}
            </span>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Root>
    );
  };
  const rail = (side: "left" | "right") => (
    <div css={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 4, paddingTop: 8, width: 36, flex: "none",
                position: "sticky", top: 52, alignSelf: "flex-start" }}>
      {side === "left" ? (
        <Tooltip componentId="nb.rail.toc.tip" content="Table of contents" side="right">
          <Button componentId="nb.rail.toc" size="small" icon={<ListBorderIcon />} aria-label="Table of contents" aria-pressed={prefs.left === "toc"}
                  onClick={() => setPrefs({ left: prefs.left ? null : "toc" })}
                  css={prefs.left === "toc" ? { "&&": { background: theme.colors.actionDefaultBackgroundPress } } : undefined} />
        </Tooltip>
      ) : (
        <>
          <Tooltip componentId="nb.rail.vars.tip" content="Variables" side="left">
            <Button componentId="nb.rail.vars" size="small" icon={<BracketsCurlyIcon />} aria-label="Variables" aria-pressed={prefs.right === "vars"}
                    onClick={() => setPrefs({ right: prefs.right ? null : "vars" })}
                    css={prefs.right === "vars" ? { "&&": { background: theme.colors.actionDefaultBackgroundPress } } : undefined} />
          </Tooltip>
          <Tooltip componentId="nb.rail.keys.tip" content="Keyboard shortcuts" side="left">
            <Button componentId="nb.rail.keys" size="small" icon={<KeyboardIcon />} aria-label="Keyboard shortcuts" onClick={() => setShortcuts(true)} />
          </Tooltip>
        </>
      )}
    </div>
  );
  const panel = (body: React.ReactNode) => (
    <div css={{ width: 280, flex: "none", position: "sticky", top: 52, alignSelf: "flex-start", height: "calc(100vh - 170px)",
                border: line, borderRadius: 8, overflow: "hidden", background: theme.colors.backgroundPrimary }}>{body}</div>
  );

  const renderCell = (c: Live, i: number) => {
    const isActive = i === active;
    const isSel = i >= lo && i <= hi;
    const isCode = c.cell_type === "code";
    const isRunning = running === c.key;
    const isQueued = queue.includes(c.key);
    const stat = stats[c.key];
    // A run of this cell that isn't this page's: Claude's, another tab's, or one whose page is gone.
    const ext = c.id && !isRunning && !isQueued ? lastRunOf[c.id] : undefined;
    const extShown = ext && ((!FINISHED.has(ext.run.live) && ["running", "queued"].includes(ext.cell.state))
                             || (ext.run.live === "failed" && ext.cell.state === "failed")) ? ext : undefined;
    const extRunning = extShown?.run.live === "running" && extShown.cell.state === "running";
    const tags = c.metadata?.tags ?? [];
    const isParams = tags.includes("parameters");
    const srcHidden = hidden(c, "source_hidden");
    const outHidden = hidden(c, "outputs_hidden");
    const title = titleOf(c);
    // Another runner's outputs so far, until its run saves them into the notebook.
    const outputs = (extShown && extShown.cell.state === "running" && extShown.cell.outputs.length ? extShown.cell.outputs : c.outputs) ?? [];
    const editing = c.cell_type !== "markdown" || editingMd === c.key;
    const show = isSel || isRunning || isQueued;
    const tool = { size: "small" } as const;
    const runMenu = (
      <DropdownMenu.Content align="start">
        <DropdownMenu.Item componentId="nb.cell.run.one" onClick={() => enqueue([c.key])}>Run cell<DropdownMenu.HintColumn>{MOD} + Enter</DropdownMenu.HintColumn></DropdownMenu.Item>
        <DropdownMenu.Item componentId="nb.cell.run.above" disabled={i === 0} onClick={() => enqueue(keys.slice(0, i))}>Run all above</DropdownMenu.Item>
        <DropdownMenu.Item componentId="nb.cell.run.below" onClick={() => enqueue(keys.slice(i))}>Run cell and below</DropdownMenu.Item>
      </DropdownMenu.Content>
    );
    return (
      <div key={c.key} id={`nb-cell-${c.key}`}
           onDragOver={(e) => { if (drag) { e.preventDefault(); const r = e.currentTarget.getBoundingClientRect(); setDrag({ ...drag, over: e.clientY < r.top + r.height / 2 ? i : i + 1 }); } }}
           onDrop={(e) => { e.preventDefault(); if (drag) dropAt(drag.over); }}
           css={{ position: "relative", display: "grid", gridTemplateColumns: "24px minmax(0, 1fr)", gap: 4,
                  "&:hover .nb-hover": { visibility: "visible" } }}>
        {drag && drag.over === i && <div css={{ position: "absolute", left: 28, right: 0, top: -6, height: 2, background: blue }} />}
        {drag && drag.over === i + 1 && i === keys.length - 1 && <div css={{ position: "absolute", left: 28, right: 0, bottom: -6, height: 2, background: blue }} />}
        {/* Databricks' gutter: a handle to drag the cell, a chevron to fold its code. */}
        <div css={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 2, paddingTop: 8 }}>
          {canEdit && <span className="nb-hover" draggable title="Drag to move" aria-label="Drag to move the cell"
                onDragStart={(e) => { e.dataTransfer.effectAllowed = "move"; setDrag({ key: c.key, over: i }); }} onDragEnd={() => setDrag(null)}
                css={{ cursor: "grab", color: theme.colors.textSecondary, visibility: show ? "visible" : "hidden", display: "flex" }}><DragIcon /></span>}
          {isCode && <button type="button" className="nb-hover" aria-label={srcHidden ? "Show code" : "Hide code"} aria-expanded={!srcHidden}
                  onClick={() => setHidden([c.key], "source_hidden", !srcHidden)}
                  css={{ all: "unset", cursor: "pointer", display: "flex", color: theme.colors.textSecondary,
                         visibility: show || srcHidden ? "visible" : "hidden" }}>{srcHidden ? <ChevronRightIcon /> : <ChevronDownIcon />}</button>}
        </div>
        <div onMouseDown={(e) => { if (e.shiftKey) { e.preventDefault(); pick(i, true); } else if (!isActive || lo !== hi) { setActive(i); setAnchor(i); } }}
             css={{ position: "relative", minWidth: 0, borderRadius: 8,
                    background: isCode || editing ? theme.colors.backgroundSecondary : "transparent",
                    border: `1px solid ${isSel ? blue : isCode ? theme.colors.border : "transparent"}`,
                    boxShadow: isSel && lo !== hi ? `0 0 0 1px ${blue}` : undefined,
                    "&:hover": { borderColor: isSel ? blue : theme.colors.border } }}>
          {(isRunning || extRunning) && (
            <div css={{ position: "absolute", top: 0, left: 8, right: 8, height: 2, overflow: "hidden" }}>
              <div css={{ width: "40%", height: "100%", background: theme.colors.actionPrimaryBackgroundDefault, animation: `${slide} 1.2s ease-in-out infinite` }} />
            </div>
          )}
          {/* The cell's head: run on the left, its number and title in the middle, its tools on the right. */}
          <div css={{ position: "relative", display: "flex", alignItems: "center", justifyContent: "space-between", height: 36, padding: "0 8px" }}>
            <div className={show ? undefined : "nb-hover"} css={{ display: "flex", alignItems: "center", visibility: show ? "visible" : "hidden", zIndex: 1 }}>
              {isCode && canEdit && (isRunning
                ? <Button componentId="nb.cell.stop" {...tool} danger icon={<StopIcon />} aria-label="Interrupt" onClick={() => kernelAction("interrupt")} />
                : isQueued
                  ? <Tooltip componentId="nb.cell.queued.tip" content="Queued: click to take it off the queue">
                      <Button componentId="nb.cell.dequeue" {...tool} icon={<ClockIcon />} aria-label="Remove from the queue"
                              onClick={() => setQueue((q) => q.filter((k) => k !== c.key))} /></Tooltip>
                  : <span css={{ display: "inline-flex", "& > button, & > span > button": { all: "unset", boxSizing: "border-box", cursor: "pointer", height: 24,
                                                                display: "inline-flex", alignItems: "center", justifyContent: "center",
                                                                background: theme.colors.actionPrimaryBackgroundDefault, color: theme.colors.actionPrimaryTextDefault,
                                                                "&:hover": { background: theme.colors.actionPrimaryBackgroundHover } } }}>
                      {/* Plain buttons: the design system paints an icon-only primary button transparent; Databricks' is solid. */}
                      <button type="button" aria-label="Run cell" title={`Run cell (${MOD} + Enter)`} onClick={() => enqueue([c.key])}
                              css={{ width: 28, borderRadius: "4px 0 0 4px" }}><PlayIcon /></button>
                      <DropdownMenu.Root modal={false}>
                        <DropdownMenu.Trigger asChild>
                          <button type="button" aria-label="More run options" css={{ width: 18, borderRadius: "0 4px 4px 0", borderLeft: "1px solid rgba(0,0,0,0.25)" }}>
                            <ChevronDownIcon css={{ fontSize: 12 }} /></button>
                        </DropdownMenu.Trigger>
                        {runMenu}
                      </DropdownMenu.Root>
                    </span>)}
            </div>
            <div css={{ position: "absolute", left: 120, right: 200, display: "flex", justifyContent: "center", alignItems: "center", gap: 8, minWidth: 0 }}>
              {titling === c.key ? (
                <input autoFocus defaultValue={title} aria-label="Cell title" placeholder="Cell title"
                       onBlur={(e) => { edit(c.key, { metadata: { ...c.metadata, title: e.target.value.trim() || undefined } }); setTitling(null); }}
                       onKeyDown={(e) => { if (e.key === "Enter" || e.key === "Escape") { (e.target as HTMLInputElement).blur(); commandMode(); } }}
                       css={{ font: "inherit", fontWeight: 600, width: "100%", maxWidth: 360, textAlign: "center", padding: "2px 6px", borderRadius: 4,
                              color: theme.colors.textPrimary, background: theme.colors.backgroundPrimary, border: `1px solid ${blue}`, outline: "none" }} />
              ) : (
                <span onDoubleClick={() => canEdit && setTitling(c.key)} css={{ display: "inline-flex", gap: 8, minWidth: 0, whiteSpace: "nowrap" }}>
                  <Typography.Text color="secondary">{i + 1}</Typography.Text>
                  {title && <Typography.Text bold ellipsis>{title}</Typography.Text>}
                </span>
              )}
            </div>
            <div className={isSel ? undefined : "nb-hover"} css={{ display: "flex", alignItems: "center", gap: 2, visibility: isSel ? "visible" : "hidden", zIndex: 1 }}>
              {isParams && <Tag componentId="nb.cell.params" color="indigo">parameters</Tag>}
              <DropdownMenu.Root modal={false}>
                <DropdownMenu.Trigger asChild disabled={!canEdit}>
                  <button type="button" aria-label="Cell language" css={{ all: "unset", cursor: canEdit ? "pointer" : "default", fontSize: 12, lineHeight: "20px",
                    padding: "0 8px", borderRadius: 4, background: theme.colors.actionDefaultBackgroundPress, color: theme.colors.textPrimary }}>
                    {isCode ? "Python" : "Markdown"}</button>
                </DropdownMenu.Trigger>
                <DropdownMenu.Content align="end">
                  <DropdownMenu.Item componentId="nb.cell.lang.py" onClick={() => !isCode && edit(c.key, { cell_type: "code", outputs: [], execution_count: null })}>Python</DropdownMenu.Item>
                  <DropdownMenu.Item componentId="nb.cell.lang.md" onClick={() => isCode && edit(c.key, { cell_type: "markdown", outputs: undefined, execution_count: undefined })}>Markdown</DropdownMenu.Item>
                </DropdownMenu.Content>
              </DropdownMenu.Root>
              {canEdit && <Button componentId="nb.cell.delete" {...tool} icon={<TrashIcon />} aria-label="Delete cell" disabled={isRunning} onClick={() => remove([c.key])} />}
              <Button componentId="nb.cell.full" {...tool} icon={<FullscreenIcon />} aria-label="Open the cell full screen" onClick={() => setFull(c.key)} />
              <DropdownMenu.Root modal={false}>
                <DropdownMenu.Trigger asChild><Button componentId="nb.cell.more" {...tool} icon={<OverflowIcon />} aria-label="Cell actions" /></DropdownMenu.Trigger>
                <DropdownMenu.Content align="end" minWidth={240}>
                  <DropdownMenu.Item componentId="nb.cell.cut" disabled={!canEdit} onClick={() => { setAnchor(i); setActive(i); setClip([c]); remove([c.key]); }}>Cut cell<DropdownMenu.HintColumn>{MOD} + X</DropdownMenu.HintColumn></DropdownMenu.Item>
                  <DropdownMenu.Item componentId="nb.cell.copy" onClick={() => setClip([c])}>Copy cell<DropdownMenu.HintColumn>{MOD} + C</DropdownMenu.HintColumn></DropdownMenu.Item>
                  <DropdownMenu.Separator />
                  <DropdownMenu.Item componentId="nb.cell.title" disabled={!canEdit} onClick={() => setTitling(c.key)}>{title ? "Edit title" : "Add title"}<DropdownMenu.HintColumn>T</DropdownMenu.HintColumn></DropdownMenu.Item>
                  <DropdownMenu.Item componentId="nb.cell.up" disabled={!canEdit || i === 0} onClick={() => { setActive(i); setAnchor(i); structure((cs) => swap(cs, i, i - 1)); setActive(i - 1); setAnchor(i - 1); }}>Move up</DropdownMenu.Item>
                  <DropdownMenu.Item componentId="nb.cell.down" disabled={!canEdit || i === keys.length - 1} onClick={() => { structure((cs) => swap(cs, i, i + 1)); setActive(i + 1); setAnchor(i + 1); }}>Move down</DropdownMenu.Item>
                  {isCode && <DropdownMenu.Item componentId="nb.cell.fold" disabled={!canEdit} onClick={() => setHidden([c.key], "source_hidden", !srcHidden)}>{srcHidden ? "Show code" : "Hide code"}</DropdownMenu.Item>}
                  {isCode && <DropdownMenu.Item componentId="nb.cell.foldout" disabled={!canEdit} onClick={() => setHidden([c.key], "outputs_hidden", !outHidden)}>{outHidden ? "Show result" : "Hide result"}</DropdownMenu.Item>}
                  {isCode && <DropdownMenu.Item componentId="nb.cell.clear" disabled={!canEdit} onClick={() => clearOutputs([c.key])}>Clear output</DropdownMenu.Item>}
                  {isCode && <DropdownMenu.Item componentId="nb.cell.params" disabled={!canEdit} onClick={() => edit(c.key, { metadata: { ...c.metadata, tags: isParams ? tags.filter((x) => x !== "parameters") : [...tags, "parameters"] } })}>
                    {isParams ? "Remove the parameters tag" : "Tag as parameters"}</DropdownMenu.Item>}
                  <DropdownMenu.Separator />
                  <DropdownMenu.Item componentId="nb.cell.del" danger disabled={!canEdit || isRunning} onClick={() => remove([c.key])}>Delete<DropdownMenu.HintColumn>D , D</DropdownMenu.HintColumn></DropdownMenu.Item>
                </DropdownMenu.Content>
              </DropdownMenu.Root>
            </div>
          </div>
          <div css={{ padding: "0 8px 8px 4px" }}>
            {srcHidden ? (
              <button type="button" onClick={() => setHidden([c.key], "source_hidden", false)}
                      css={{ all: "unset", cursor: "pointer", padding: "0 8px", color: theme.colors.textSecondary, fontSize: 12 }}>
                {text(c.source).split("\n").length} hidden lines · Show code</button>
            ) : !editing ? (
              <div onDoubleClick={() => editCell(i)} css={{ padding: "0 12px", minHeight: 24, cursor: "text" }}>
                {text(c.source).trim() ? <Markdown source={text(c.source)} />
                  : <Typography.Text color="secondary">{canEdit ? "Double-click or press Enter to write markdown" : "Empty markdown cell"}</Typography.Text>}
              </div>
            ) : (
              <CodeEditor value={text(c.source)} language={isCode ? "python" : "markdown"} numbers={prefs.numbers} readOnly={!canEdit}
                          onChange={(v) => { if (v !== text(c.source)) edit(c.key, { source: v }); }}
                          onFocus={() => { setActive(i); setAnchor(i); }} autoFocus={editingMd === c.key && !isCode}
                          onEscape={commandMode}
                          onEdge={(dir) => {
                            const j = dir === "up" ? i - 1 : i + 1;
                            if (j < 0 || j >= keys.length) return;
                            if (cells![j].cell_type === "markdown" && editingMd !== cells![j].key) { commandMode(); pick(j); }
                            else { setActive(j); setAnchor(j); setFocusReq({ key: cells![j].key, at: dir === "up" ? "end" : "start", n: Date.now() }); }
                          }}
                          complete={isCode ? complete : undefined} inspect={isCode ? inspect : undefined} words={words}
                          find={findRe}
                          select={select?.key === c.key ? select : null}
                          symbol={isCode ? symbol : null} onSymbol={setSymbol} onDefinition={definition}
                          diagnostics={isCode && lint ? lint[c.key] ?? [] : undefined}
                          onRename={(w) => canEdit && setRenaming({ from: w, to: w })}
                          focus={focusReq?.key === c.key ? focusReq : null}
                          onRun={() => (isCode ? enqueue([c.key]) : commandMode())}
                          onRunNext={() => {
                            if (isCode) enqueue([c.key]); else setEditingMd(null);
                            if (i === keys.length - 1) {
                              const n = blank("code");
                              structure((cs) => [...cs, n]);
                              setActive(i + 1); setAnchor(i + 1); setFocusReq({ key: n.key, at: "start", n: Date.now() });
                            } else if (cells![i + 1].cell_type === "code") { setActive(i + 1); setAnchor(i + 1); setFocusReq({ key: cells![i + 1].key, at: "start", n: Date.now() }); }
                            else { commandMode(); pick(i + 1); }
                          }} />
            )}
          </div>
          {isCode && (stat || isQueued || c.execution_count != null || extShown) && (
            <div css={{ display: "flex", alignItems: "center", gap: 8, padding: "0 12px 6px", minHeight: 20, flexWrap: "wrap" }}>
              {extShown
                ? <OtherRun run={extShown.run} cell={extShown.cell} me={me.data?.email}
                            onInterrupt={canEdit && rt?.id === extShown.run.runtime && kernel === extShown.run.kernel ? () => kernelAction("interrupt") : undefined} />
                : <RunStatus queued={isQueued} stat={stat} />}
              {c.execution_count != null && !isRunning && !isQueued && !extShown && <Typography.Text color="secondary" size="sm">[{c.execution_count}]</Typography.Text>}
            </div>
          )}
          {isCode && outputs.length > 0 && (
            <div css={{ borderTop: line, padding: "8px 12px", background: theme.colors.backgroundPrimary, borderRadius: "0 0 8px 8px" }}>
              {outHidden
                ? <Typography.Link componentId="nb.cell.showout" onClick={() => setHidden([c.key], "outputs_hidden", false)}>Show result</Typography.Link>
                : <NotebookOutputs outputs={outputs} />}
            </div>
          )}
        </div>
      </div>
    );
  };

  const fullCell = cells?.find((c) => c.key === full);
  // What the compute is doing, as the bar says it: the runtime's state, then the kernel's own word, so a busy
  // kernel nothing here runs, a dead one and a stopped runtime each read as what they are.
  const compute = ((): { text: string; tone: "ok" | "warn" | "bad" | "ring" | "off"; detail: string } => {
    if (!rt) return { text: "", tone: "off", detail: "No runtime selected" };
    const idle = rt.idle ? ` It stops after ${rt.idle} with no kernel activity and no notebook connected; a detached process doesn't count.` : "";
    if (rt.state !== "RUNNING") {
      return { text: rt.state.toLowerCase().replace(/_/g, " ") || "starting", tone: rt.state.startsWith("BEING_") || waiting ? "warn" : "off",
               detail: `Runtime ${rt.state || "starting"}.${idle}` };
    }
    const k = kstat?.execution_state;
    const since = kstat?.last_activity ? `, last activity ${new Date(kstat.last_activity).toLocaleTimeString()}` : "";
    const detail = `Runtime RUNNING.${idle} Kernel: ${kernel ? (k ?? (kstat ? "gone" : "unknown")) : "none yet"}${since}.`;
    if (!kernel) return { text: "no kernel yet", tone: "ring", detail };
    if (k === "dead") return { text: "kernel dead", tone: "bad", detail: `${detail} Restart the kernel: variables are lost.` };
    if (kstat && k === null) return { text: "kernel gone", tone: "bad", detail };
    if (running || k === "busy") {
      return { text: running || liveRuns ? "busy" : "busy, not from this notebook", tone: "warn",
               detail: running || liveRuns ? detail : `${detail} Nothing in this notebook runs: Claude's run_code, another notebook, or a cell whose page is gone.` };
    }
    return { text: k === "idle" ? "idle" : "connected", tone: "ok", detail };
  })();
  return (
    <div ref={root} tabIndex={-1} css={{ outline: "none", minHeight: "100%" }}
         onMouseDown={(e) => { if (e.target === e.currentTarget) commandMode(); }}>
      <Breadcrumb>
        <Breadcrumb.Item><Link to="/workspace">Workspace</Link></Breadcrumb.Item>
        <Breadcrumb.Item><Link to={`/workspace?path=${encodeURIComponent(folder)}`}>{folder}</Link></Breadcrumb.Item>
        <Breadcrumb.Item>{""}</Breadcrumb.Item>
      </Breadcrumb>
      <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, marginTop: 6, minHeight: 32 }}>
        <div css={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <Typography.Title level={2} css={{ "&&": { margin: 0 } }}>{name.replace(/\.ipynb$/, "")}</Typography.Title>
          <Typography.Text color="secondary" size="sm">{saving ? "Saving..." : dirty ? "Unsaved changes" : generation ? "Saved" : ""}</Typography.Text>
          {cells && !canEdit && <Tag componentId="nb.viewonly" color="charcoal" icon={<LockIcon />}>View only</Tag>}
        </div>
        <div css={{ display: "flex", gap: 8 }}>
          <Button componentId="nb.claude" icon={<SparkleDoubleIcon />} onClick={() => setClaude(true)}>Claude</Button>
          {can === "manage" && path.startsWith("Users/") && <Button componentId="nb.share" type="primary" onClick={() => setSharing(true)}>Share</Button>}
        </div>
      </div>
      {/* Databricks' notebook bar: the menus and the default language left, run all and the compute right. */}
      <div css={{ position: "sticky", top: -16, zIndex: 5, margin: "8px -16px 0", padding: "6px 16px", background: theme.colors.backgroundPrimary,
                  borderBottom: line, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
        <div css={{ display: "flex", alignItems: "center", gap: 2 }}>
          {(["File", "Edit", "View", "Run", "Help"] as const).map(menu)}
          <span css={{ marginLeft: 8, fontSize: 12, lineHeight: "20px", padding: "0 8px", borderRadius: 4, background: theme.colors.actionDefaultBackgroundPress }}>Python</span>
        </div>
        <div css={{ display: "flex", alignItems: "center", gap: 8 }}>
          {queue.length > 0 && <Typography.Text color="secondary" size="sm">{queue.length} queued</Typography.Text>}
          {busy || kstat?.execution_state === "busy"
            ? <Button componentId="nb.interrupt" icon={<StopIcon />} onClick={() => kernelAction("interrupt")}>Interrupt</Button>
            : <Button componentId="nb.runall" icon={<PlayIcon />} onClick={() => enqueue(keys)} disabled={!cells || !canEdit}>Run all</Button>}
          <Tooltip componentId="nb.compute.tip" content={compute.detail}>
            <Button componentId="nb.connect.open" onClick={() => setConnecting(true)} css={{ minWidth: 200, justifyContent: "flex-start" }}
                    endIcon={<ChevronDownIcon />}>
              {rt ? <span css={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <span css={{ width: 8, height: 8, borderRadius: "50%", flex: "none", boxSizing: "border-box",
                                   ...(compute.tone === "ring" || compute.tone === "off"
                                     ? { border: `1.5px solid ${compute.tone === "ring" ? theme.colors.textValidationSuccess : theme.colors.textPlaceholder}` }
                                     : { background: { ok: theme.colors.textValidationSuccess, warn: theme.colors.textValidationWarning,
                                                       bad: theme.colors.textValidationDanger }[compute.tone] }) }} />
                      {rt.name}<Typography.Text color="secondary" size="sm">· {compute.text}</Typography.Text>
                    </span>
                : waiting ? `Creating ${waiting}...` : "Select runtime"}
            </Button>
          </Tooltip>
        </div>
      </div>
      <ClaudeSetup open={claude} onClose={() => setClaude(false)} notebook={path} />
      <ShareModal path={path} open={sharing} onClose={() => setSharing(false)} />
      <RuntimeDrawer open={connecting} onClose={() => setConnecting(false)} user={me.data?.email ?? ""} runtimes={mine}
                     templates={templates.data ?? []} current={rt?.id}
                     onConnect={(r) => { if (r.id) pickRuntime(r.id); setWaiting(r.id ?? r.name); runtimes.reload(); }} />
      <CommandPalette open={palette} onClose={() => { setPalette(false); commandMode(); }} actions={actions} />
      <Shortcuts open={shortcuts} onClose={() => setShortcuts(false)} />
      {access.data && !access.data.allowed && (
        <div css={{ marginTop: 8 }}><Alert componentId="nb.access" type="info" closable={false}
          message={access.data.configured ? "Allow notebook access to run cells" : "Running cells isn't set up yet"}
          description={access.data.configured
            ? "Your Colab runtime runs code as you, so the console needs your Google sign-in once. Editing and saving work without it."
            : "An admin adds the console's Google OAuth client first (this repo's NOTEBOOK_OAUTH_CLIENT_ID / _SECRET CI variables). Editing and saving work meanwhile."}
          action={access.data.configured ? <Button componentId="nb.allow" type="primary" onClick={() => { window.location.href = notebooksApi.allowUrl(`/workspace/notebook?path=${encodeURIComponent(path)}`); }}>
            Allow notebook access</Button> : undefined} /></div>)}
      {error && (
        <div css={{ marginTop: 8 }}><Alert componentId="nb.error" type="error" message={error.text} onClose={() => setError(null)}
          action={error.conflict ? <Button componentId="nb.reload" onClick={load}>Reload</Button>
            : error.access && access.data?.configured ? <Button componentId="nb.reallow" onClick={() => { window.location.href = notebooksApi.allowUrl(`/workspace/notebook?path=${encodeURIComponent(path)}`); }}>Allow again</Button> : undefined} /></div>)}
      {!cells && !error && <div css={{ marginTop: 16 }}><TableSkeleton lines={8} /></div>}
      {cells && (
        <div css={{ display: "flex", gap: 8, marginTop: 8, alignItems: "flex-start" }}>
          {rail("left")}
          {prefs.left === "toc" && panel(<Contents headings={headings} current={cells[active]?.key} onClose={() => setPrefs({ left: null })}
                                                   onPick={(k) => { commandMode(); goto(k); }} />)}
          <div css={{ flex: 1, minWidth: 0 }}>
            {find && <FindBar state={find} set={setFind} total={matches.length} at={findAt} onStep={step} canEdit={canEdit}
                              onReplace={replaceOne} onReplaceAll={replaceAll} onClose={() => { setFind(null); commandMode(); }} />}
            <div css={{ maxWidth: prefs.full ? "none" : 1100, margin: "0 auto", paddingBottom: 160 }}>
              {canEdit && <Inserter onAdd={(t) => insert(0, t)} />}
              {cells.map((c, i) => (
                <div key={c.key}>
                  {renderCell(c, i)}
                  {canEdit && <Inserter onAdd={(t) => insert(i + 1, t)} />}
                </div>
              ))}
            </div>
          </div>
          {prefs.right === "vars" && panel(<Variables ready={live} tick={varsTick} onClose={() => setPrefs({ right: null })}
                                                      load={() => notebooksApi.variables(rt!.id, kernel!)} />)}
          {rail("right")}
        </div>
      )}
      <Modal componentId="nb.rename" title={`Rename ${renaming?.from ?? ""}`} visible={!!renaming} okText="Rename" cancelText="Cancel"
             okButtonProps={{ disabled: !renaming || !/^[A-Za-z_]\w*$/.test(renaming.to) || renaming.to === renaming.from }}
             onCancel={() => setRenaming(null)} onOk={() => { if (renaming) rename(renaming.from, renaming.to); setRenaming(null); }}>
        <Typography.Paragraph color="secondary">Every use in this notebook's code cells, except attributes, strings and comments.</Typography.Paragraph>
        <input autoFocus aria-label="New name" value={renaming?.to ?? ""} spellCheck={false}
               onFocus={(e) => e.target.select()}
               onChange={(e) => setRenaming((r) => r && { ...r, to: e.target.value })}
               onKeyDown={(e) => { if (e.key === "Enter" && renaming && /^[A-Za-z_]\w*$/.test(renaming.to)) { rename(renaming.from, renaming.to); setRenaming(null); } }}
               css={{ width: "100%", boxSizing: "border-box", font: "13px Menlo, monospace", padding: "6px 8px", borderRadius: 4, outline: "none",
                      color: theme.colors.textPrimary, background: theme.colors.backgroundPrimary, border: `1px solid ${theme.colors.actionDefaultBorderFocus}` }} />
      </Modal>
      {fullCell && (
        <Modal componentId="nb.cell.fullscreen" visible onCancel={() => setFull(null)} footer={null} size="wide"
               title={`Cell ${keys.indexOf(fullCell.key) + 1}${titleOf(fullCell) ? ` · ${titleOf(fullCell)}` : ""}`}>
          <div css={{ minHeight: "60vh", display: "flex", flexDirection: "column", gap: 12 }}>
            <div css={{ border: line, borderRadius: 8, padding: 8, background: theme.colors.backgroundSecondary }}>
              <CodeEditor value={text(fullCell.source)} language={fullCell.cell_type === "code" ? "python" : "markdown"} numbers readOnly={!canEdit} autoFocus
                          onChange={(v) => { if (v !== text(fullCell.source)) edit(fullCell.key, { source: v }); }}
                          complete={fullCell.cell_type === "code" ? complete : undefined} inspect={fullCell.cell_type === "code" ? inspect : undefined}
                          words={words} onEscape={() => setFull(null)}
                          onRun={() => enqueue([fullCell.key])} onRunNext={() => enqueue([fullCell.key])} />
            </div>
            {fullCell.cell_type === "code" && <RunStatus queued={queue.includes(fullCell.key)} stat={stats[fullCell.key]} />}
            {fullCell.cell_type === "code" && <NotebookOutputs outputs={fullCell.outputs ?? []} />}
          </div>
        </Modal>
      )}
    </div>
  );
}

/** Databricks' "Code  Text" between cells, shown when the gap is hovered. */
function Inserter({ onAdd }: { onAdd: (type: "code" | "markdown") => void }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div css={{ position: "relative", display: "flex", justifyContent: "center", alignItems: "center", gap: theme.spacing.sm, height: 24,
                marginLeft: 28, opacity: 0, transition: "opacity 0.1s", "&:hover, &:focus-within": { opacity: 1 },
                "&::before": { content: '""', position: "absolute", left: 0, right: 0, top: "50%", borderTop: `1px solid ${theme.colors.border}`, zIndex: 0 } }}>
      <span css={{ display: "inline-flex", gap: 4, zIndex: 1, background: theme.colors.backgroundPrimary, padding: "0 8px" }}>
        <Button componentId="nb.insert.code" size="small" icon={<PlusIcon />} onClick={() => onAdd("code")}>Code</Button>
        <Button componentId="nb.insert.md" size="small" icon={<PlusIcon />} onClick={() => onAdd("markdown")}>Text</Button>
      </span>
    </div>
  );
}

/** A code cell's run as Databricks shows it under the cell: pending, a running clock, then ✓ or ✗ and how long it took. */
function RunStatus({ queued, stat }: { queued: boolean; stat?: Stat }) {
  const { theme } = useDesignSystemTheme();
  const [, setNow] = useState(0);
  const live = stat?.state === "running";
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(t);
  }, [live]);
  const small = { fontSize: theme.typography.fontSizeSm, color: theme.colors.textSecondary } as const;
  if (queued) return <span css={{ ...small, display: "inline-flex", gap: 4, alignItems: "center" }}><ClockIcon /> Pending</span>;
  if (!stat) return null;
  if (stat.state === "running") {
    return <span css={{ ...small, display: "inline-flex", gap: 6, alignItems: "center" }}><Spinner size="small" inheritColor />Running · {took(Date.now() - stat.start)}</span>;
  }
  return (
    <span css={{ ...small, display: "inline-flex", gap: 4, alignItems: "center" }}>
      {stat.state === "ok"
        ? <CheckCircleFillIcon css={{ color: theme.colors.textValidationSuccess }} />
        : <XCircleFillIcon css={{ color: theme.colors.textValidationDanger }} />}
      {took(stat.ms)}
    </span>
  );
}

const took = (ms: number) => (ms < 60000 ? `${(ms / 1000).toFixed(1)}s` : secondsText(ms / 1000));

// A run's liveness (console/runs.py) as the cell shows it: how it reads, and whether it's fine, doubtful or over.
const LIVE: Record<string, { label: string; tone: "ok" | "warn" | "bad" }> = {
  running: { label: "Running", tone: "ok" },
  runner_gone: { label: "Still running on the kernel, not captured", tone: "warn" },
  stale: { label: "No word from its runner", tone: "warn" },
  lost: { label: "Run lost", tone: "bad" },
  kernel_dead: { label: "Kernel died", tone: "bad" },
  kernel_gone: { label: "Kernel gone", tone: "bad" },
  runtime_stopped: { label: "Runtime stopped", tone: "bad" },
  failed: { label: "Run broke off", tone: "bad" },
};

/** A run of this cell that isn't this page's: Claude's, another tab's, or one whose page went away. It says
 *  who runs it and for how long, and when it can't be running as it claims, which of the ways it ended. */
function OtherRun({ run, cell, me, onInterrupt }: { run: NotebookRun; cell: RunCell; me?: string; onInterrupt?: () => void }) {
  const { theme } = useDesignSystemTheme();
  const [now, setNow] = useState(() => Date.now() / 1000);
  const ticking = run.live === "running" || run.live === "runner_gone";
  useEffect(() => {
    if (!ticking) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [ticking]);
  const small = { fontSize: theme.typography.fontSizeSm, color: theme.colors.textSecondary } as const;
  const who = run.by === "claude" ? "Claude" : run.user === me ? "another tab" : run.user;
  if (run.live === "running" && cell.state === "queued") {
    return <span css={{ ...small, display: "inline-flex", gap: 4, alignItems: "center" }}><ClockIcon /> Queued by {who}</span>;
  }
  if (run.live === "running") {
    return (
      <span css={{ ...small, display: "inline-flex", gap: 6, alignItems: "center" }}>
        <Spinner size="small" inheritColor />Running · by {who} · {took(((now - (cell.started ?? run.started)) * 1000))}
        <span title="The runner writes at least every 10 s, output or not">· heard from {took(Math.max(0, now - run.heartbeat) * 1000)} ago</span>
      </span>
    );
  }
  const look = LIVE[run.live] ?? { label: run.live, tone: "bad" as const };
  const color = look.tone === "warn" ? theme.colors.textValidationWarning : theme.colors.textValidationDanger;
  return (
    <span css={{ ...small, display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
      {look.tone === "warn" ? <WarningFillIcon css={{ color }} /> : <DangerFillIcon css={{ color }} />}
      <span css={{ color, fontWeight: 600 }}>{look.label}</span>
      <span>· by {who}{cell.started ? `, started ${new Date(cell.started * 1000).toLocaleTimeString()}` : ""}
        {run.live === "runner_gone" ? `, last heard ${took(Math.max(0, now - run.heartbeat) * 1000)} ago` : ""}</span>
      {(run.why || run.error) && <span css={{ flexBasis: "100%" }}>{run.why || run.error}</span>}
      {run.live === "runner_gone" && onInterrupt &&
        <Button componentId="nb.cell.other.interrupt" size="small" danger icon={<StopIcon />} onClick={onInterrupt}>Interrupt</Button>}
    </span>
  );
}

function swap<T>(xs: T[], a: number, b: number): T[] {
  const out = [...xs];
  [out[a], out[b]] = [out[b], out[a]];
  return out;
}

/** Appends an output as Jupyter does: a stream continues the stream before it of the same name. */
function merge(outputs: CellOutput[], m: CellOutput): CellOutput[] {
  const last = outputs[outputs.length - 1];
  if (m.output_type === "stream" && last?.output_type === "stream" && last.name === m.name) {
    return [...outputs.slice(0, -1), { ...last, text: text(last.text) + text(m.text) }];
  }
  return [...outputs, m];
}
