import { useEffect, useRef } from "react";
import {
  acceptCompletion, autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap, completionStatus, startCompletion,
  type Completion, type CompletionContext, type CompletionResult,
} from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap, indentLess, indentMore } from "@codemirror/commands";
import {
  HighlightStyle, bracketMatching, ensureSyntaxTree, foldGutter, foldKeymap, indentOnInput, indentUnit, syntaxHighlighting, syntaxTree,
} from "@codemirror/language";
import { python } from "@codemirror/lang-python";
import { markdown } from "@codemirror/lang-markdown";
import { type Diagnostic, linter, setDiagnostics } from "@codemirror/lint";
import { highlightSelectionMatches, selectNextOccurrence, selectSelectionMatches } from "@codemirror/search";
import { Compartment, EditorSelection, EditorState, Prec, RangeSetBuilder, StateEffect, StateField } from "@codemirror/state";
import {
  Decoration, type DecorationSet, EditorView, MatchDecorator, ViewPlugin, type ViewUpdate, crosshairCursor, drawSelection,
  dropCursor, highlightActiveLine, highlightActiveLineGutter, highlightSpecialChars, hoverTooltip, keymap, lineNumbers,
  rectangularSelection, showTooltip, type Tooltip,
} from "@codemirror/view";
import { tags as t } from "@lezer/highlight";
import { useDesignSystemTheme } from "@databricks/design-system";
import type { Completions } from "../api";
import type { CellDiagnostic } from "./notebook/ruff";

const MONO = 'Menlo, Monaco, Consolas, "Ubuntu Mono", "Source Code Pro", monospace';

/** Monaco's Dark+ and Light+, the colours Databricks' notebook editor paints Python in. */
export const PALETTE = {
  dark: { control: "#C586C0", keyword: "#569CD6", fn: "#DCDCAA", string: "#CE9178", number: "#B5CEA8", comment: "#6A9955",
          type: "#4EC9B0", text: "#CCCCCC", param: "#9CDCFE", brackets: ["#FFD700", "#DA70D6", "#179FFF"],
          selection: "#264F78", gutter: "#858585", activeLine: "rgba(255, 255, 255, 0.04)", match: "rgba(234, 92, 0, 0.33)" },
  light: { control: "#AF00DB", keyword: "#0000FF", fn: "#795E26", string: "#A31515", number: "#098658", comment: "#008000",
           type: "#267F99", text: "#1F272D", param: "#001080", brackets: ["#0431FA", "#319331", "#7B3814"],
           selection: "#ADD6FF", gutter: "#6E7681", activeLine: "rgba(0, 0, 0, 0.04)", match: "rgba(234, 92, 0, 0.25)" },
};

function look(dark: boolean) {
  const p = dark ? PALETTE.dark : PALETTE.light;
  const style = HighlightStyle.define([
    { tag: [t.controlKeyword, t.moduleKeyword], color: p.control },
    { tag: [t.keyword, t.definitionKeyword, t.operatorKeyword, t.bool, t.null, t.self, t.operator], color: p.keyword },
    { tag: [t.function(t.variableName), t.function(t.propertyName), t.function(t.definition(t.variableName))], color: p.fn },
    { tag: [t.string, t.special(t.string)], color: p.string },
    { tag: [t.number], color: p.number },
    { tag: [t.comment, t.lineComment], color: p.comment, fontStyle: "italic" },
    { tag: [t.className, t.typeName, t.definition(t.className)], color: p.type },
    { tag: [t.attributeName, t.labelName], color: p.param },
    { tag: t.heading, color: p.keyword, fontWeight: "bold" },
    { tag: t.emphasis, fontStyle: "italic" },
    { tag: t.strong, fontWeight: "bold" },
    { tag: t.link, color: p.string, textDecoration: "underline" },
  ]);
  const view = EditorView.theme({
    "&": { fontSize: "13px", color: p.text, backgroundColor: "transparent" },
    ".cm-scroller": { fontFamily: MONO, lineHeight: "20px" },
    ".cm-content": { caretColor: dark ? "#AEAFAD" : "#000", padding: "0" },
    "&.cm-focused": { outline: "none" },
    ".cm-gutters": { backgroundColor: "transparent", border: "none", color: p.gutter },
    ".cm-lineNumbers .cm-gutterElement": { padding: "0 12px 0 4px", minWidth: "28px" },
    ".cm-foldGutter .cm-gutterElement": { width: "16px", opacity: 0, transition: "opacity .1s" },
    "&:hover .cm-foldGutter .cm-gutterElement, .cm-foldGutter .cm-gutterElement:hover": { opacity: 1 },
    ".cm-activeLine, .cm-activeLineGutter": { backgroundColor: "transparent" },
    "&.cm-focused .cm-activeLine": { backgroundColor: p.activeLine },
    "&.cm-focused .cm-activeLineGutter": { color: dark ? "#C6C6C6" : "#0B216F" },
    "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, ::selection":
      { backgroundColor: `${p.selection} !important` },
    ".cm-selectionMatch": { backgroundColor: dark ? "rgba(173, 214, 255, 0.15)" : "rgba(173, 214, 255, 0.5)" },
    ".cm-matchingBracket": { outline: `1px solid ${p.gutter}`, backgroundColor: "transparent" },
    ".cm-nb-find": { backgroundColor: p.match, borderRadius: "2px" },
    ".cm-nb-symbol": { backgroundColor: dark ? "rgba(87, 87, 87, 0.72)" : "rgba(87, 87, 87, 0.18)", borderRadius: "2px" },
    ".cm-nb-guide": { boxShadow: `inset 1px 0 0 ${dark ? "rgba(255, 255, 255, 0.12)" : "rgba(0, 0, 0, 0.12)"}` },
    ".cm-lintRange-error": { backgroundImage: "none", textDecoration: `underline wavy ${dark ? "#F14C4C" : "#E51400"}`,
                             textDecorationSkipInk: "none", textUnderlineOffset: "3px" },
    ".cm-diagnostic": { fontFamily: "var(--db-font-family)" },
    ".cm-lintRange-warning": { backgroundImage: "none", textDecoration: `underline wavy ${dark ? "#CCA700" : "#BF8803"}`,
                               textDecorationSkipInk: "none", textUnderlineOffset: "3px" },
    ".cm-lintRange-hint": { backgroundImage: "none" },
    // Monaco fades what's unused instead of squiggling it.
    ".cm-nb-unused": { opacity: 0.55 },
    ".cm-tooltip": { fontFamily: MONO, fontSize: "12px" },
    ".cm-tooltip-autocomplete > ul > li": { padding: "2px 8px", lineHeight: "20px" },
    ".cm-nb-doc": { maxWidth: "640px", maxHeight: "320px", overflow: "auto", margin: 0, padding: "8px 12px", whiteSpace: "pre-wrap" },
  }, { dark });
  return [view, syntaxHighlighting(style)];
}

/** Brackets coloured by depth, as Monaco's bracket-pair colourization does. */
function bracketColours(colours: string[]) {
  const marks = colours.map((c) => Decoration.mark({ attributes: { style: `color: ${c}` } }));
  const build = (view: EditorView) => {
    const b = new RangeSetBuilder<Decoration>();
    let depth = 0;
    const doc = view.state.doc.toString();
    // Brackets inside strings and comments still count: cheap, and what the eye expects of most cells.
    for (let i = 0; i < doc.length; i++) {
      const ch = doc[i];
      if (ch === "(" || ch === "[" || ch === "{") { b.add(i, i + 1, marks[depth % marks.length]); depth++; }
      else if (ch === ")" || ch === "]" || ch === "}") { depth = Math.max(0, depth - 1); b.add(i, i + 1, marks[depth % marks.length]); }
    }
    return b.finish();
  };
  return ViewPlugin.fromClass(class {
    decorations: DecorationSet;
    constructor(view: EditorView) { this.decorations = build(view); }
    update(u: ViewUpdate) { if (u.docChanged) this.decorations = build(u.view); }
  }, { decorations: (v) => v.decorations });
}

function findMarks(re: RegExp | null) {
  if (!re) return [];
  const deco = new MatchDecorator({ regexp: re, decoration: Decoration.mark({ class: "cm-nb-find" }) });
  return ViewPlugin.fromClass(class {
    decorations: DecorationSet;
    constructor(view: EditorView) { this.decorations = deco.createDeco(view); }
    update(u: ViewUpdate) { this.decorations = deco.updateDeco(u, this.decorations); }
  }, { decorations: (v) => v.decorations });
}

const PY_KEYWORDS = new Set(("False None True and as assert async await break class continue def del elif else except finally for "
  + "from global if import in is lambda nonlocal not or pass raise return try while with yield self").split(" "));
const inText = (view: EditorView, pos: number) => /String|Comment/.test(syntaxTree(view.state).resolveInner(pos, 1).name);

/** The identifier around `pos`, or null on a keyword, a literal or inside a string or comment. */
export function wordAt(view: EditorView, pos: number): { word: string; from: number; to: number } | null {
  const line = view.state.doc.lineAt(pos);
  const at = pos - line.from;
  let a = at, b = at;
  while (a > 0 && /\w/.test(line.text[a - 1])) a--;
  while (b < line.text.length && /\w/.test(line.text[b])) b++;
  const word = line.text.slice(a, b);
  if (!/^[A-Za-z_]\w*$/.test(word) || PY_KEYWORDS.has(word) || inText(view, line.from + a)) return null;
  return { word, from: line.from + a, to: line.from + b };
}

/** Every use of the symbol under the cursor, in this cell and the notebook's others: code only, not strings or comments. */
function symbolMarks(word: string | null | undefined) {
  if (!word) return [];
  const mark = Decoration.mark({ class: "cm-nb-symbol" });
  const deco = new MatchDecorator({
    regexp: new RegExp(`(?<![\\w.])${word}(?!\\w)`, "g"),
    decoration: (_m, view, pos) => (inText(view, pos) ? null : mark),
  });
  return ViewPlugin.fromClass(class {
    decorations: DecorationSet;
    constructor(view: EditorView) { this.decorations = deco.createDeco(view); }
    update(u: ViewUpdate) { this.decorations = deco.updateDeco(u, this.decorations); }
  }, { decorations: (v) => v.decorations });
}

/** Monaco's indent guides: a faint rule at each four-space step of a line's indentation. */
const indentGuides = ViewPlugin.fromClass(class {
  decorations: DecorationSet;
  constructor(view: EditorView) { this.decorations = this.build(view); }
  update(u: ViewUpdate) { if (u.docChanged || u.viewportChanged) this.decorations = this.build(u.view); }
  build(view: EditorView) {
    const b = new RangeSetBuilder<Decoration>();
    const mark = Decoration.mark({ class: "cm-nb-guide" });
    for (const { from, to } of view.visibleRanges) {
      for (let pos = from; pos <= to;) {
        const line = view.state.doc.lineAt(pos);
        const lead = /^ */.exec(line.text)![0].length;
        for (let k = 0; k < lead; k += 4) b.add(line.from + k, line.from + k + 1, mark);
        pos = line.to + 1;
      }
    }
    return b.finish();
  }
}, { decorations: (v) => v.decorations });

/** Syntax errors as Monaco squiggles them; IPython's %magic and !shell lines aren't Python, so they're skipped. */
const syntaxErrors = linter((view) => {
  const tree = ensureSyntaxTree(view.state, view.state.doc.length, 200) ?? syntaxTree(view.state);
  const out: Diagnostic[] = [];
  tree.iterate({ enter: (n) => {
    if (!n.type.isError || out.length > 20) return;
    const line = view.state.doc.lineAt(n.from);
    if (/^\s*[%!?]/.test(line.text) || /^\s*[%!?]/.test(view.state.doc.lineAt(Math.max(0, n.from - 1)).text)) return;
    const to = n.to > n.from ? n.to : Math.min(line.to, n.from + 1);
    out.push({ from: n.from === to && n.from > line.from ? n.from - 1 : n.from, to: Math.max(to, n.from), severity: "error",
               message: "Syntax error" });
  } });
  return out;
}, { delay: 600 });

// The docstring Shift+Tab shows, as a tooltip at the cursor until the next edit or move.
const showDoc = StateEffect.define<{ pos: number; text: string } | null>();
const docField = StateField.define<Tooltip | null>({
  create: () => null,
  update(tip, tr) {
    for (const e of tr.effects) if (e.is(showDoc)) return e.value && { pos: e.value.pos, above: true, create: () => {
      const dom = document.createElement("pre");
      dom.className = "cm-nb-doc";
      dom.textContent = e.value!.text;
      return { dom };
    } };
    return tr.docChanged || tr.selection ? null : tip;
  },
  provide: (f) => showTooltip.from(f),
});

const PY_WORDS = ("False None True and as assert async await break class continue def del elif else except finally for from global if "
  + "import in is lambda nonlocal not or pass raise return try while with yield print len range enumerate zip list dict set tuple str "
  + "int float bool type isinstance sorted reversed sum min max abs open map filter any all super property staticmethod classmethod")
  .split(" ");
const KIND: Record<string, string> = { function: "function", class: "class", module: "namespace", keyword: "keyword",
                                       instance: "variable", statement: "keyword", param: "variable", path: "text" };

/** A notebook cell's source in CodeMirror, as Databricks' editor behaves: Shift+Enter runs and moves on,
 *  Mod+Enter runs in place, Tab completes from the kernel, Shift+Tab shows a name's docstring, Esc leaves
 *  for command mode and the arrow keys walk off the first or last line into the next cell. */
export default function CodeEditor({ value, onChange, language, onRun, onRunNext, onFocus, onEscape, onEdge, autoFocus,
  readOnly = false, numbers = false, complete, inspect, words, find, select, focus, symbol, onSymbol, onDefinition, onRename,
  diagnostics }: {
  value: string; onChange: (v: string) => void; language: "python" | "markdown";
  onRun?: () => void; onRunNext?: () => void; onFocus?: () => void; onEscape?: () => void; onEdge?: (dir: "up" | "down") => void;
  autoFocus?: boolean; readOnly?: boolean; numbers?: boolean;
  complete?: (code: string, pos: number) => Promise<Completions | null>;
  inspect?: (code: string, pos: number) => Promise<string | null>;
  words?: () => string[];
  find?: RegExp | null;
  select?: { from: number; to: number; n: number; focus?: boolean } | null;
  focus?: { at: "start" | "end"; n: number } | null;
  symbol?: string | null;
  onSymbol?: (word: string | null) => void;
  onDefinition?: (word: string) => void;
  onRename?: (word: string) => void;
  // Ruff's findings for this cell; until ruff has loaded (undefined), the editor marks syntax errors itself.
  diagnostics?: CellDiagnostic[];
}) {
  const { theme } = useDesignSystemTheme();
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const slots = useRef({ look: new Compartment(), ro: new Compartment(), nums: new Compartment(), find: new Compartment(),
                         brackets: new Compartment(), symbol: new Compartment(), lint: new Compartment() });
  // The latest props, read by the extensions the editor was built with.
  const props = useRef({ onChange, onRun, onRunNext, onFocus, onEscape, onEdge, complete, inspect, words, onSymbol, onDefinition, onRename });
  props.current = { onChange, onRun, onRunNext, onFocus, onEscape, onEdge, complete, inspect, words, onSymbol, onDefinition, onRename };
  const symbolTimer = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    const source = async (ctx: CompletionContext): Promise<CompletionResult | null> => {
      const word = ctx.matchBefore(/\w*/);
      const dot = /\.\w*$/.test(ctx.state.sliceDoc(Math.max(0, ctx.pos - 80), ctx.pos));
      if (!ctx.explicit && !dot && (!word || word.to - word.from < 2)) return null;
      const from = word?.from ?? ctx.pos;
      let options: Completion[] = [];
      let start = from;
      const kernel = props.current.complete;
      if (kernel) {
        try {
          const r = await kernel(ctx.state.doc.toString(), ctx.pos);
          if (ctx.aborted) return null;
          if (r && r.matches.length) {
            start = r.cursor_start;
            options = r.matches.map((m, i) => ({ label: m, type: KIND[r.types[m]] ?? "variable", boost: 99 - Math.min(i, 98) }));
          }
        } catch { /* the notebook's own words stand in */ }
      }
      if (!dot && start === from) {
        const seen = new Set(options.map((o) => o.label));
        for (const w of [...(props.current.words?.() ?? []), ...PY_WORDS]) {
          if (!seen.has(w)) { seen.add(w); options.push({ label: w, type: PY_WORDS.includes(w) ? "keyword" : "variable" }); }
        }
      }
      return options.length ? { from: start, options, validFor: /^\w*$/ } : null;
    };
    const tab = (v: EditorView) => {
      if (completionStatus(v.state) === "active") return acceptCompletion(v);
      const { from, empty } = v.state.selection.main;
      const before = v.state.sliceDoc(Math.max(0, from - 1), from);
      if (empty && language === "python" && /[\w.]/.test(before)) return startCompletion(v);
      return indentMore(v);
    };
    const shiftTab = (v: EditorView) => {
      const { from, empty } = v.state.selection.main;
      const inspectFn = props.current.inspect;
      if (!empty || !inspectFn || language !== "python" || !/[\w(]/.test(v.state.sliceDoc(Math.max(0, from - 1), from + 1))) {
        return indentLess(v);
      }
      inspectFn(v.state.doc.toString(), from).then((text) => {
        if (text && v.state.selection.main.from === from) v.dispatch({ effects: showDoc.of({ pos: from, text }) });
      }, () => { /* no docstring, no tooltip */ });
      return true;
    };
    const edge = (dir: "up" | "down") => (v: EditorView) => {
      const sel = v.state.selection.main;
      if (!sel.empty || !props.current.onEdge || completionStatus(v.state) === "active") return false;
      const line = v.state.doc.lineAt(sel.head).number;
      if (dir === "up" ? line !== 1 : line !== v.state.doc.lines) return false;
      props.current.onEdge(dir);
      return true;
    };
    const atCursor = (fn?: (w: string) => void) => (v: EditorView) => {
      const w = fn && language === "python" ? wordAt(v, v.state.selection.main.head) : null;
      if (!w || !fn) return false;
      fn(w.word);
      return true;
    };
    // The docstring of the call being opened, as Monaco's signature help shows it on "(".
    const signature = (v: EditorView, pos: number) => props.current.inspect?.(v.state.doc.toString(), pos).then((text) => {
      if (text && v.state.selection.main.head === pos) v.dispatch({ effects: showDoc.of({ pos, text }) });
    }, () => { /* no signature, no tooltip */ });
    const hoverDocs = hoverTooltip(async (v, pos) => {
      const inspectFn = props.current.inspect;
      const w = inspectFn ? wordAt(v, pos) : null;
      if (!w || !inspectFn) return null;
      const text = await inspectFn(v.state.doc.toString(), w.to).catch(() => null);
      if (!text) return null;
      return { pos: w.from, end: w.to, above: true, create: () => {
        const dom = document.createElement("pre");
        dom.className = "cm-nb-doc";
        dom.textContent = text;
        return { dom };
      } };
    }, { hoverTime: 500 });
    const v = new EditorView({
      parent: host.current!,
      state: EditorState.create({
        doc: value,
        extensions: [
          Prec.highest(keymap.of([
            { key: "Shift-Enter", run: () => { props.current.onRunNext?.(); return true; } },
            { key: "Mod-Enter", run: () => { props.current.onRun?.(); return true; } },
            { key: "Escape", run: (ev) => {
              if (completionStatus(ev.state) || ev.state.field(docField)) { ev.dispatch({ effects: showDoc.of(null) }); return false; }
              props.current.onEscape?.(); return true;
            } },
            { key: "Tab", run: tab },
            { key: "Shift-Tab", run: shiftTab },
            { key: "ArrowUp", run: edge("up") },
            { key: "ArrowDown", run: edge("down") },
            { key: "F12", run: atCursor((w) => props.current.onDefinition?.(w)) },
            { key: "F2", run: atCursor((w) => props.current.onRename?.(w)) },
            { key: "Mod-d", run: selectNextOccurrence, preventDefault: true },
            { key: "Mod-Shift-l", run: selectSelectionMatches, preventDefault: true },
          ])),
          slots.current.nums.of(numbers ? [lineNumbers(), highlightActiveLineGutter()] : []),
          foldGutter({ markerDOM: (open) => {
            const s = document.createElement("span");
            s.textContent = open ? "⌄" : "›";
            s.style.cssText = "cursor:pointer;display:inline-block;width:16px;text-align:center";
            return s;
          } }),
          highlightSpecialChars(), history(), drawSelection(), dropCursor(),
          EditorState.allowMultipleSelections.of(true), indentUnit.of("    "), EditorState.tabSize.of(4),
          indentOnInput(), bracketMatching(), closeBrackets(), rectangularSelection(), crosshairCursor(),
          highlightActiveLine(), highlightSelectionMatches(),
          autocompletion({ override: language === "python" ? [source] : [], icons: true, defaultKeymap: true }),
          // Shift+Alt+Up/Down are the notebook's run above/below in Databricks, not copy-line.
          keymap.of([...closeBracketsKeymap, ...defaultKeymap.filter((b) => !/^Shift-Alt-Arrow(Up|Down)$/.test(b.key ?? "")),
                     ...historyKeymap, ...foldKeymap, ...completionKeymap]),
          language === "python" ? python() : markdown(),
          docField,
          slots.current.look.of(look(theme.isDarkMode)),
          slots.current.brackets.of(language === "python" ? bracketColours((theme.isDarkMode ? PALETTE.dark : PALETTE.light).brackets) : []),
          slots.current.ro.of(EditorState.readOnly.of(readOnly)),
          slots.current.find.of(findMarks(find ?? null)),
          slots.current.symbol.of(language === "python" ? symbolMarks(symbol) : []),
          language === "python" ? [indentGuides, hoverDocs] : [],
          slots.current.lint.of(language === "python" && diagnostics === undefined ? syntaxErrors : []),
          EditorView.domEventHandlers({
            mousedown: (e, view) => {
              if (!(e.metaKey || e.ctrlKey) || e.altKey || language !== "python" || !props.current.onDefinition) return false;
              const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
              const w = pos == null ? null : wordAt(view, pos);
              if (!w) return false;
              e.preventDefault();
              props.current.onDefinition(w.word);
              return true;
            },
          }),
          EditorView.lineWrapping,
          // Code isn't prose: no browser spellcheck, no Grammarly underlines on names.
          EditorView.contentAttributes.of({ spellcheck: "false", autocorrect: "off", autocapitalize: "off",
                                            "data-gramm": "false", "data-gramm_editor": "false", "data-enable-grammarly": "false" }),
          // Room for the notebook's sticky bar when a jump scrolls a line into view.
          EditorView.scrollMargins.of(() => ({ top: 120, bottom: 40 })),
          EditorView.updateListener.of((u) => {
            if (u.docChanged) props.current.onChange(u.state.doc.toString());
            if (u.focusChanged && u.view.hasFocus) props.current.onFocus?.();
            if (language === "python" && (u.selectionSet || u.docChanged || u.focusChanged) && props.current.onSymbol) {
              clearTimeout(symbolTimer.current);
              const view = u.view;
              symbolTimer.current = setTimeout(() => {
                if (!view.hasFocus) return;
                const sel = view.state.selection.main;
                const w = wordAt(view, sel.head);
                // A selection that is exactly one name (as go-to-definition leaves) keeps that name highlighted.
                props.current.onSymbol?.(w && (sel.empty || (sel.from === w.from && sel.to === w.to)) ? w.word : null);
              }, 120);
            }
            if (language === "python" && props.current.inspect) {
              for (const tr of u.transactions) {
                if (!tr.isUserEvent("input.type")) continue;
                tr.changes.iterChanges((_a, _b, _fromB, toB, ins) => {
                  if (ins.toString().startsWith("(")) signature(u.view, Math.min(toB, u.state.doc.length) - (ins.toString() === "()" ? 1 : 0));
                });
              }
            }
          }),
        ],
      }),
    });
    view.current = v;
    if (autoFocus) v.focus();
    return () => v.destroy();
    // The editor is built once per cell; the props below are applied as they change.
  }, [language]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const v = view.current;
    if (v && v.state.doc.toString() !== value) v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: value } });
  }, [value]);

  useEffect(() => {
    const p = theme.isDarkMode ? PALETTE.dark : PALETTE.light;
    view.current?.dispatch({ effects: [slots.current.look.reconfigure(look(theme.isDarkMode)),
      slots.current.brackets.reconfigure(language === "python" ? bracketColours(p.brackets) : [])] });
  }, [theme.isDarkMode]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    view.current?.dispatch({ effects: slots.current.ro.reconfigure(EditorState.readOnly.of(readOnly)) });
  }, [readOnly]);

  useEffect(() => {
    view.current?.dispatch({ effects: slots.current.nums.reconfigure(numbers ? [lineNumbers(), highlightActiveLineGutter()] : []) });
  }, [numbers]);

  useEffect(() => {
    view.current?.dispatch({ effects: slots.current.find.reconfigure(findMarks(find ?? null)) });
  }, [find]);

  useEffect(() => {
    if (language === "python") view.current?.dispatch({ effects: slots.current.symbol.reconfigure(symbolMarks(symbol)) });
  }, [symbol]); // eslint-disable-line react-hooks/exhaustive-deps

  const ruffOn = diagnostics !== undefined;
  useEffect(() => {
    view.current?.dispatch({ effects: slots.current.lint.reconfigure(language === "python" && !ruffOn ? syntaxErrors : []) });
  }, [ruffOn]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const v = view.current;
    if (!v || language !== "python" || !diagnostics) return;
    // Findings are for the source ruff saw; anything past the end of what's here now is dropped until the next lint.
    const len = v.state.doc.length;
    const shown: Diagnostic[] = diagnostics.filter((d) => d.to <= len).map((d) => ({
      from: d.from, to: d.to, severity: d.severity, message: d.message, source: "ruff",
      markClass: d.unused ? "cm-nb-unused" : undefined,
      actions: d.fix ? [{ name: d.fix.label, apply: (view: EditorView) => {
        if (view.state.doc.length !== len) return;
        view.dispatch({ changes: d.fix!.edits.map((e) => ({ from: e.from, to: e.to, insert: e.insert })) });
      } }] : undefined,
    }));
    v.dispatch(setDiagnostics(v.state, shown));
  }, [diagnostics]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const v = view.current;
    if (!v || !select) return;
    const to = Math.min(select.to, v.state.doc.length);
    v.dispatch({ selection: EditorSelection.single(Math.min(select.from, to), to), scrollIntoView: true });
    if (select.focus) v.focus();
  }, [select?.n]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const v = view.current;
    if (!v || !focus) return;
    v.focus();
    v.dispatch({ selection: EditorSelection.cursor(focus.at === "start" ? 0 : v.state.doc.length) });
  }, [focus?.n]); // eslint-disable-line react-hooks/exhaustive-deps

  return <div ref={host} css={{ "& .cm-editor": { minHeight: 20 } }} />;
}
