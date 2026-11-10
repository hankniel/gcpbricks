import { useEffect, useState } from "react";
import {
  Button, CloseIcon, Empty, Input, RefreshIcon, SearchIcon, Spinner, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import type { KernelVariable } from "../../api";

/** A side panel's frame: its title and close button over its body, as Databricks' panels are. */
export function PanelFrame({ title, onClose, actions, children }: {
  title: string; onClose: () => void; actions?: React.ReactNode; children: React.ReactNode;
}) {
  const { theme } = useDesignSystemTheme();
  return (
    <div css={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
      <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 4, height: 40, padding: "0 8px 0 12px",
                  borderBottom: `1px solid ${theme.colors.border}`, flex: "none" }}>
        <Typography.Text bold>{title}</Typography.Text>
        <span css={{ display: "flex", gap: 2 }}>{actions}
          <Button componentId="nb.panel.close" size="small" icon={<CloseIcon />} aria-label={`Close ${title}`} onClick={onClose} /></span>
      </div>
      <div css={{ flex: 1, minHeight: 0, overflow: "auto" }}>{children}</div>
    </div>
  );
}

export type Heading = { key: string; level: number; text: string };

/** Markdown headings, as Databricks' table of contents lists them; a click selects and reveals the cell. */
export function Contents({ headings, current, onPick, onClose }: {
  headings: Heading[]; current?: string; onPick: (key: string) => void; onClose: () => void;
}) {
  const { theme } = useDesignSystemTheme();
  const top = Math.min(...headings.map((h) => h.level), 6);
  return (
    <PanelFrame title="Table of contents" onClose={onClose}>
      {headings.length === 0
        ? <div css={{ padding: 12 }}><Typography.Text color="secondary">Markdown headings (# Title) appear here.</Typography.Text></div>
        : <div css={{ padding: "4px 0" }}>{headings.map((h, i) => (
          <div key={`${h.key}-${i}`} role="button" tabIndex={0} onClick={() => onPick(h.key)} onKeyDown={(e) => e.key === "Enter" && onPick(h.key)}
               title={h.text}
               css={{ padding: `4px 12px 4px ${12 + (h.level - top) * 14}px`, cursor: "pointer", overflow: "hidden", textOverflow: "ellipsis",
                      whiteSpace: "nowrap", fontWeight: h.level === top ? 600 : 400,
                      color: h.key === current ? theme.colors.actionDefaultTextHover : theme.colors.textPrimary,
                      "&:hover": { background: theme.colors.actionDefaultBackgroundHover } }}>{h.text}</div>))}
        </div>}
    </PanelFrame>
  );
}

/** The kernel's variables, read silently: refreshed by hand and after every cell that finishes. */
export function Variables({ load, ready, tick, onClose }: {
  load: () => Promise<KernelVariable[]>; ready: boolean; tick: number; onClose: () => void;
}) {
  const { theme } = useDesignSystemTheme();
  const [rows, setRows] = useState<KernelVariable[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [q, setQ] = useState("");
  const refresh = () => {
    if (!ready) return;
    setBusy(true);
    load().then((r) => { setRows(r); setError(null); }, (e: Error) => setError(e.message)).finally(() => setBusy(false));
  };
  useEffect(refresh, [ready, tick]); // eslint-disable-line react-hooks/exhaustive-deps
  const shown = (rows ?? []).filter((r) => r.name.toLowerCase().includes(q.toLowerCase()));
  const cell = { padding: "6px 8px", borderBottom: `1px solid ${theme.colors.border}`, verticalAlign: "top" } as const;
  return (
    <PanelFrame title="Variables" onClose={onClose}
      actions={<Button componentId="nb.vars.refresh" size="small" icon={busy ? <Spinner size="small" /> : <RefreshIcon />}
                       aria-label="Refresh variables" disabled={!ready || busy} onClick={refresh} />}>
      {!ready ? <div css={{ padding: 12 }}><Typography.Text color="secondary">Connect to a runtime and run a cell to see its variables.</Typography.Text></div> : (
        <div css={{ padding: 8 }}>
          <Input componentId="nb.vars.filter" prefix={<SearchIcon />} placeholder="Filter variables" value={q} onChange={(e) => setQ(e.target.value)} allowClear />
          {error && <Typography.Paragraph color="error" css={{ "&&": { margin: "8px 0 0" } }}>{error}</Typography.Paragraph>}
          {rows && shown.length === 0 && <Empty title="No variables" description={q ? "None match the filter." : "Define one in a cell and run it."} />}
          {shown.length > 0 && (
            <table css={{ width: "100%", borderCollapse: "collapse", marginTop: 8, tableLayout: "fixed", fontSize: 12 }}>
              <thead><tr css={{ textAlign: "left", color: theme.colors.textSecondary }}>
                <th css={{ ...cell, width: "34%" }}>Name</th><th css={{ ...cell, width: "26%" }}>Type</th><th css={cell}>Value</th></tr></thead>
              <tbody>{shown.map((r) => (
                <tr key={r.name}>
                  <td css={{ ...cell, fontFamily: "Menlo, monospace", overflowWrap: "anywhere" }}>{r.name}</td>
                  <td css={{ ...cell, color: theme.colors.textSecondary }}>{r.type}{r.shape ? <div>{r.shape}</div> : null}</td>
                  <td css={{ ...cell, fontFamily: "Menlo, monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      title={r.value}>{r.value}</td>
                </tr>))}</tbody>
            </table>
          )}
        </div>
      )}
    </PanelFrame>
  );
}
