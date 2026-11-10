import { useMemo } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { Typography, useDesignSystemTheme } from "@databricks/design-system";
import type { Cell, CellOutput } from "../api";

export const text = (s: string | string[] | undefined) => (Array.isArray(s) ? s.join("") : s ?? "");
// eslint-disable-next-line no-control-regex
const ANSI = /\u001b\[[0-9;]*m/g;

/** Markdown as a notebook renders it, sanitized: notebooks are shared, their HTML untrusted. */
export function Markdown({ source }: { source: string }) {
  const { theme } = useDesignSystemTheme();
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(source, { async: false }) as string), [source]);
  return <div css={{ color: theme.colors.textPrimary, lineHeight: 1.6, "& img": { maxWidth: "100%" },
                     "& pre, & code": { fontFamily: "var(--db-font-family-mono, monospace)", fontSize: 12 },
                     "& table": { borderCollapse: "collapse" }, "& th, & td": { border: `1px solid ${theme.colors.border}`, padding: "2px 8px" } }}
              dangerouslySetInnerHTML={{ __html: html }} />;
}

function Rich({ data }: { data: Record<string, unknown> }) {
  const { theme } = useDesignSystemTheme();
  const get = (k: string) => text(data[k] as string | string[] | undefined);
  for (const img of ["image/png", "image/jpeg", "image/gif"]) {
    if (data[img]) return <img alt="output" src={`data:${img};base64,${get(img).replace(/\n/g, "")}`} css={{ maxWidth: "100%" }} />;
  }
  if (data["image/svg+xml"]) return <div dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(get("image/svg+xml")) }} />;
  if (data["text/html"]) {
    return <div css={{ overflowX: "auto", color: theme.colors.textPrimary, "& table": { borderCollapse: "collapse", fontSize: 12 },
                       "& th, & td": { border: `1px solid ${theme.colors.border}`, padding: "2px 8px", textAlign: "right" },
                       "& thead th": { background: theme.colors.backgroundSecondary } }}
                dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(get("text/html")) }} />;
  }
  if (data["text/markdown"]) return <Markdown source={get("text/markdown")} />;
  if (data["application/json"]) return <pre css={{ margin: 0 }}>{JSON.stringify(data["application/json"], null, 2)}</pre>;
  return <pre css={{ margin: 0, whiteSpace: "pre-wrap" }}>{get("text/plain")}</pre>;
}

/** A code cell's outputs, as Jupyter renders them. */
export default function NotebookOutputs({ outputs }: { outputs: CellOutput[] }) {
  const { theme } = useDesignSystemTheme();
  if (!outputs.length) return null;
  const mono = { fontFamily: "var(--db-font-family-mono, monospace)", fontSize: 12 };
  return (
    <div css={{ ...mono, padding: `${theme.spacing.xs}px ${theme.spacing.sm}px`, display: "flex", flexDirection: "column", gap: theme.spacing.xs }}>
      {outputs.map((o, i) => {
        if (o.output_type === "stream") {
          return <pre key={i} css={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word",
                                     color: o.name === "stderr" ? theme.colors.textValidationWarning : theme.colors.textPrimary }}>{text(o.text)}</pre>;
        }
        if (o.output_type === "error") {
          return <pre key={i} css={{ margin: 0, whiteSpace: "pre-wrap", color: theme.colors.textValidationDanger }}>
            {(o.traceback.length ? o.traceback.join("\n") : `${o.ename}: ${o.evalue}`).replace(ANSI, "")}</pre>;
        }
        return <div key={i}><Rich data={o.data} /></div>;
      })}
    </div>
  );
}

/** A notebook read-only: an executed notebook_task's output, its cells and what they printed. */
export function NotebookView({ cells }: { cells: Cell[] }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div css={{ display: "flex", flexDirection: "column", gap: theme.spacing.sm }}>
      {cells.map((c, i) => (
        <div key={i} css={{ border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd, overflow: "hidden" }}>
          {c.cell_type === "markdown" ? <div css={{ padding: theme.spacing.sm }}><Markdown source={text(c.source)} /></div> : (
            <>
              <div css={{ display: "flex", gap: theme.spacing.sm, padding: theme.spacing.sm, background: theme.colors.backgroundSecondary }}>
                <Typography.Text color="secondary" css={{ fontFamily: "var(--db-font-family-mono, monospace)", fontSize: 12, minWidth: 36 }}>
                  [{c.execution_count ?? " "}]</Typography.Text>
                <pre css={{ margin: 0, whiteSpace: "pre-wrap", fontFamily: "var(--db-font-family-mono, monospace)", fontSize: 12, flex: 1 }}>
                  {text(c.source)}</pre>
                {c.metadata?.tags?.length ? <Typography.Text color="secondary" size="sm">{c.metadata.tags.join(", ")}</Typography.Text> : null}
              </div>
              <NotebookOutputs outputs={c.outputs ?? []} />
            </>
          )}
        </div>
      ))}
    </div>
  );
}
