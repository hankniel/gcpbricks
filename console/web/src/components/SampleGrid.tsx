import { useDesignSystemTheme } from "@databricks/design-system";
import type { Sample } from "../api";
import { ColumnTypeIcon } from "./LineageGraph";

function cellText(v: unknown): string {
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** Sample rows as Catalog Explorer shows them: sticky header and row numbers, each column as
 *  wide as its values (to a cap), scrolling both ways, so a table hundreds of columns wide reads. */
export default function SampleGrid({ sample, types = {} }: {
  sample: Sample; types?: Record<string, { type: string; mode?: string }>;
}) {
  const { theme } = useDesignSystemTheme();
  const line = theme.colors.borderDecorative ?? theme.colors.border;
  const cell = { padding: "0 12px", height: 32, borderBottom: `1px solid ${line}`, borderRight: `1px solid ${line}` };
  return (
    <div css={{ overflow: "auto", maxHeight: "65vh", border: `1px solid ${line}`, borderRadius: theme.borders.borderRadiusMd,
                background: theme.colors.backgroundPrimary }}>
      <table css={{ borderCollapse: "separate", borderSpacing: 0, fontSize: theme.typography.fontSizeBase, whiteSpace: "nowrap",
                    color: theme.colors.textPrimary }}>
        <thead>
          <tr>
            <th css={{ ...cell, position: "sticky", top: 0, left: 0, zIndex: 2, background: theme.colors.backgroundSecondary }} />
            {sample.columns.map((c) => (
              <th key={c} css={{ ...cell, textAlign: "left", position: "sticky", top: 0, zIndex: 1, fontWeight: 600,
                                 background: theme.colors.backgroundSecondary }}>
                <span css={{ display: "inline-flex", alignItems: "center", gap: theme.spacing.xs }}>
                  {types[c] && <ColumnTypeIcon type={types[c].type} mode={types[c].mode} />}{c}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sample.rows.map((r, i) => (
            <tr key={i}>
              <td css={{ ...cell, position: "sticky", left: 0, textAlign: "right", color: theme.colors.textSecondary,
                         background: theme.colors.backgroundSecondary }}>{i + 1}</td>
              {r.map((v, j) => (
                <td key={j} title={cellText(v)} css={{ ...cell, maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis",
                  color: v == null ? theme.colors.textPlaceholder : undefined, fontStyle: v == null ? "italic" : undefined }}>
                  {v == null ? "null" : cellText(v)}
                </td>))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
