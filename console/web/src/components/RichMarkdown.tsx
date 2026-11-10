import { useEffect, useRef } from "react";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/common";
import { Marked } from "marked";
import markedAlert from "marked-alert";
import { getHeadingList, gfmHeadingId } from "marked-gfm-heading-id";
import { markedHighlight } from "marked-highlight";
import markedKatex from "marked-katex-extension";
import "katex/dist/katex.min.css";
import type { Mermaid } from "mermaid";
import mermaidUrl from "mermaid/dist/mermaid.min.js?url";
import { useDesignSystemTheme } from "@databricks/design-system";
import { PALETTE } from "./CodeEditor";

export type Heading = { id: string; level: number; text: string };

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const md = new Marked(
  markedHighlight({
    langPrefix: "hljs language-", emptyLangClass: "hljs",
    highlight: (code, lang) => (hljs.getLanguage(lang) ? hljs.highlight(code, { language: lang }).value : code),
  }),
  markedKatex({ throwOnError: false, nonStandard: true }),
  markedAlert(),
  gfmHeadingId(),
  // A mermaid block keeps its source, drawn once the page is in the DOM (and again when the theme turns);
  // URI-encoded, since DOMPurify drops an attribute holding "-->".
  { renderer: { code: ({ text, lang }) => (lang === "mermaid"
    ? `<div class="mermaid-block" data-src="${encodeURIComponent(text)}"></div>` : false) } },
);

/** A post's Markdown as sanitized HTML, and its headings for a table of contents. */
export function renderMarkdown(source: string): { html: string; headings: Heading[] } {
  const html = DOMPurify.sanitize(md.parse(source, { async: false }) as string);
  return { html, headings: getHeadingList().map((h) => ({ id: h.id, level: h.level, text: DOMPurify.sanitize(h.text) })) };
}

let drawn = 0;

// Mermaid's own single-file build, shipped as an asset and loaded as a script: bundling its thousands of
// modules took the console's build past the CI runner's memory.
let mermaid: Promise<Mermaid> | undefined;
const loadMermaid = () => (mermaid ??= new Promise((resolve, reject) => {
  const script = Object.assign(document.createElement("script"), { src: mermaidUrl });
  script.onload = () => resolve((globalThis as unknown as { mermaid: Mermaid }).mermaid);
  script.onerror = () => { mermaid = undefined; reject(new Error("mermaid didn't load")); };
  document.head.append(script);
}));

/** HTML from renderMarkdown: GitHub's Markdown look, code coloured as the notebook editor colours it,
 *  KaTeX maths, GitHub's alerts and mermaid diagrams. */
export default function RichMarkdown({ html }: { html: string }) {
  const { theme } = useDesignSystemTheme();
  const host = useRef<HTMLDivElement>(null);
  const dark = theme.isDarkMode;

  useEffect(() => {
    const blocks = [...(host.current?.querySelectorAll<HTMLDivElement>(".mermaid-block") ?? [])];
    if (!blocks.length) return;
    let stale = false;
    loadMermaid().then(async (mermaid) => {
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "default" });
      for (const b of blocks) {
        try {
          const { svg } = await mermaid.render(`mermaid-${++drawn}`, decodeURIComponent(b.dataset.src ?? ""));
          if (!stale) b.innerHTML = svg;
        } catch (e) {
          if (!stale) b.innerHTML = `<pre class="mermaid-error">${esc(String((e as Error).message ?? e))}</pre>`;
        }
      }
    });
    return () => { stale = true; };
  }, [html, dark]);

  const p = dark ? PALETTE.dark : PALETTE.light;
  const alert = (color: string) => ({ borderLeft: `4px solid ${color}`, "& .markdown-alert-title": { color, "& svg": { fill: color } } });
  return (
    <div ref={host} dangerouslySetInnerHTML={{ __html: html }} css={{
      color: theme.colors.textPrimary, fontSize: 15, lineHeight: 1.7, overflowWrap: "break-word",
      "& h1, & h2, & h3, & h4": { fontWeight: 600, lineHeight: 1.3, margin: "1.6em 0 0.6em", scrollMarginTop: 16 },
      "& h1": { fontSize: 28, paddingBottom: 6, borderBottom: `1px solid ${theme.colors.border}` },
      "& h2": { fontSize: 22, paddingBottom: 4, borderBottom: `1px solid ${theme.colors.border}` },
      "& h3": { fontSize: 18 }, "& h4": { fontSize: 16 },
      "& > :first-child": { marginTop: 0 },
      "& p, & ul, & ol, & blockquote, & table, & pre": { margin: "0 0 1em" },
      "& img": { maxWidth: "100%", borderRadius: 4 },
      "& hr": { border: 0, borderTop: `1px solid ${theme.colors.border}`, margin: "1.5em 0" },
      "& blockquote": { padding: "0 1em", color: theme.colors.textSecondary, borderLeft: `4px solid ${theme.colors.border}` },
      "& table": { borderCollapse: "collapse", display: "block", overflowX: "auto" },
      "& th, & td": { border: `1px solid ${theme.colors.border}`, padding: "6px 12px" },
      "& th": { background: theme.colors.backgroundSecondary, fontWeight: 600 },
      "& code": { fontFamily: "var(--db-font-family-mono, monospace)", fontSize: "0.88em", padding: "0.15em 0.35em",
                  borderRadius: 4, background: theme.colors.backgroundSecondary },
      "& pre": { padding: 16, borderRadius: 6, overflowX: "auto", background: theme.colors.backgroundSecondary,
                 border: `1px solid ${theme.colors.border}`, lineHeight: 1.5 },
      "& pre code": { padding: 0, background: "none", fontSize: 13, color: p.text },
      "& .hljs-keyword, & .hljs-literal, & .hljs-selector-tag, & .hljs-section, & .hljs-meta .hljs-keyword": { color: p.keyword },
      "& .hljs-built_in, & .hljs-title.function_, & .hljs-title": { color: p.fn },
      "& .hljs-title.class_, & .hljs-type": { color: p.type },
      "& .hljs-string, & .hljs-regexp, & .hljs-meta .hljs-string": { color: p.string },
      "& .hljs-number, & .hljs-symbol": { color: p.number },
      "& .hljs-comment, & .hljs-quote": { color: p.comment, fontStyle: "italic" },
      "& .hljs-attr, & .hljs-attribute, & .hljs-params, & .hljs-variable, & .hljs-property": { color: p.param },
      "& .hljs-meta": { color: p.control },
      "& .hljs-addition": { color: theme.colors.textValidationSuccess }, "& .hljs-deletion": { color: theme.colors.textValidationDanger },
      "& .markdown-alert": { padding: "8px 16px", margin: "0 0 1em", borderRadius: 4, background: theme.colors.backgroundSecondary,
                             "& > :last-child": { marginBottom: 0 } },
      "& .markdown-alert-title": { display: "flex", alignItems: "center", gap: 8, fontWeight: 600, margin: "0 0 4px" },
      "& .markdown-alert-note": alert(theme.colors.actionPrimaryBackgroundDefault),
      "& .markdown-alert-tip": alert(theme.colors.textValidationSuccess),
      "& .markdown-alert-important": alert(dark ? "#AB7DF8" : "#8250DF"),
      "& .markdown-alert-warning": alert(theme.colors.textValidationWarning),
      "& .markdown-alert-caution": alert(theme.colors.textValidationDanger),
      "& .mermaid-block": { margin: "0 0 1em", display: "flex", justifyContent: "center", overflowX: "auto", minHeight: 24 },
      "& .mermaid-error": { color: theme.colors.textValidationDanger, whiteSpace: "pre-wrap" },
      "& .katex-display": { overflowX: "auto", overflowY: "hidden" },
    }} />
  );
}
