import { useState } from "react";
import { ChevronDownIcon, ChevronRightIcon, Typography, useDesignSystemTheme } from "@databricks/design-system";

/** A collapsible section of Databricks' right-hand details panel. */
export function Section({ title, children }: { title: string; children: React.ReactNode }) {
  const { theme } = useDesignSystemTheme();
  const [open, setOpen] = useState(true);
  return (
    // Databricks' measures: a 32px head, the body 28px in under the chevron, 24px either side of the rule.
    <section css={{ borderBottom: `1px solid ${theme.colors.border}`, paddingBottom: theme.spacing.lg, marginBottom: theme.spacing.lg }}>
      <button onClick={() => setOpen(!open)} aria-expanded={open} css={{
        all: "unset", boxSizing: "border-box", cursor: "pointer", display: "flex", alignItems: "center", gap: 12, width: "100%",
        height: 32, padding: "6px 0", "& > svg, & > span[role=img]": { color: theme.colors.textSecondary } }}>
        {open ? <ChevronDownIcon /> : <ChevronRightIcon />}
        <Typography.Text bold>{title}</Typography.Text>
      </button>
      {open && <div css={{ marginTop: theme.spacing.sm, paddingLeft: 28 }}>{children}</div>}
    </section>
  );
}

/** One label / value line, as "Job ID   123" in Databricks' panel. */
export function Field({ name, children }: { name: string; children: React.ReactNode }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div css={{ display: "grid", gridTemplateColumns: "137px minmax(0, 1fr)", minHeight: 30, paddingBottom: theme.spacing.xs,
                "& > *": { paddingTop: 1 }, "& .du-bois-light-typography, & .du-bois-dark-typography": { lineHeight: "24px" } }}>
      <Typography.Text color="secondary" css={{ paddingRight: theme.spacing.sm }}>{name}</Typography.Text>
      <div css={{ minWidth: 0, overflowWrap: "anywhere" }}><Typography.Text>{children}</Typography.Text></div>
    </div>
  );
}

const MONO = "\"DM Mono\", monospace";

/** Text in the code font with no chip behind it, so a long value wraps cleanly. */
export function Mono({ children }: { children: React.ReactNode }) {
  return <span css={{ fontFamily: MONO, fontSize: 12, overflowWrap: "anywhere" }}>{children}</span>;
}

/** Parameters as Databricks lists them: one row per name, its value beneath in the code font,
 *  whole and wrapped as a block (no chip per wrapped line), rows parted by a hairline. */
export function ParamList({ rows, empty }: { rows: [string, unknown][]; empty: string }) {
  const { theme } = useDesignSystemTheme();
  if (!rows.length) return <Typography.Text color="secondary">{empty}</Typography.Text>;
  return (
    <div css={{ border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd, overflow: "hidden" }}>
      {rows.map(([k, v], i) => {
        const text = typeof v === "string" ? v : JSON.stringify(v);
        return (
          <div key={k} css={{ padding: `${theme.spacing.xs + 2}px ${theme.spacing.sm}px`,
                              borderTop: i ? `1px solid ${theme.colors.border}` : undefined }}>
            <Typography.Text color="secondary" size="sm" css={{ display: "block", overflowWrap: "anywhere" }}>{k}</Typography.Text>
            {text === "" ? <Typography.Text color="secondary" size="sm">(empty)</Typography.Text> : (
              <div css={{ marginTop: 2, fontFamily: MONO, fontSize: 12, lineHeight: "18px",
                          whiteSpace: "pre-wrap", overflowWrap: "anywhere", color: theme.colors.textPrimary }}>{text}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}
