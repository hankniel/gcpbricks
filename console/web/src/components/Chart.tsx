import { Typography, useDesignSystemTheme } from "@databricks/design-system";

/** Databricks' categorical chart colors, in order. */
const PALETTE = ["#077A9D", "#FFAB00", "#00A972", "#FF3621", "#8BCAE7", "#AB4057", "#99DDB4", "#FCA4A1", "#919191", "#BF7080"];

export type Series = { name: string; points: { x: string; y: number | null }[] };

/** A widget's rows as series: x is the first column, the series the middle ones (a `by`
 *  under a bin), y the last. */
export function toSeries(columns: string[], rows: (string | number | null)[][]): { xs: string[]; series: Series[] } {
  const xs = [...new Set(rows.map((r) => String(r[0])))];
  const byName = new Map<string, Map<string, number | null>>();
  for (const r of rows) {
    const name = columns.length > 2 ? r.slice(1, -1).map(String).join(" / ") : columns[columns.length - 1];
    if (!byName.has(name)) byName.set(name, new Map());
    byName.get(name)!.set(String(r[0]), r[r.length - 1] == null ? null : Number(r[r.length - 1]));
  }
  return { xs, series: [...byName].map(([name, m]) => ({ name, points: xs.map((x) => ({ x, y: m.get(x) ?? null })) })) };
}

export function number(v: unknown): string {
  if (v == null || v === "") return "-";
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return Math.abs(n) >= 1000 ? n.toLocaleString("en-US", { maximumFractionDigits: 0 })
    : n.toLocaleString("en-US", { maximumFractionDigits: Number.isInteger(n) ? 0 : 2 });
}

/** A line or (grouped, or stacked) bar chart over categorical x, drawn as SVG like the console's other charts. */
export default function Chart({ kind, xs, series, height = 220, stacked = false }: {
  kind: "line" | "bar"; xs: string[]; series: Series[]; height?: number; stacked?: boolean;
}) {
  const { theme } = useDesignSystemTheme();
  const W = 640, H = height, L = 48, R = 12, T = 12, B = 36;
  const stack = kind === "bar" && stacked;
  // Stacked: each x's bars sit on the ones before them, so the scale is each x's sum.
  const ys = stack
    ? xs.map((_, i) => series.reduce((a, s) => a + Math.max(0, s.points[i]?.y ?? 0), 0))
    : series.flatMap((s) => s.points.map((p) => p.y)).filter((y): y is number => y != null);
  if (!xs.length || !ys.length) return <Typography.Text color="secondary">No data in this window.</Typography.Text>;
  const hi = Math.max(0, ...ys), lo = Math.min(0, ...ys), span = hi - lo || 1;
  const y = (v: number) => T + (1 - (v - lo) / span) * (H - T - B);
  const band = (W - L - R) / xs.length;
  const x = (i: number) => L + band * (i + 0.5);
  const ticks = [lo, lo + span / 2, hi];
  const every = Math.ceil(xs.length / 8);
  const grid = theme.colors.borderDecorative ?? theme.colors.border;
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" css={{ display: "block" }}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke={grid} />
            <text x={L - 6} y={y(t) + 4} textAnchor="end" fontSize={10} fill={theme.colors.textSecondary}>{number(t)}</text>
          </g>
        ))}
        {xs.map((label, i) => i % every === 0 && (
          <text key={label} x={x(i)} y={H - B + 14} textAnchor="middle" fontSize={10} fill={theme.colors.textSecondary}>
            {label.length > 12 ? `${label.slice(0, 11)}…` : label}</text>
        ))}
        {series.map((s, si) => {
          const color = PALETTE[si % PALETTE.length];
          if (stack) {
            const w = Math.max(2, band * 0.7);
            return s.points.map((p, i) => {
              if (p.y == null || p.y <= 0) return null;
              const below = series.slice(0, si).reduce((a, o) => a + Math.max(0, o.points[i]?.y ?? 0), 0);
              return (
                <rect key={`${s.name}-${p.x}`} x={x(i) - w / 2} width={w} y={y(below + p.y)} height={y(below) - y(below + p.y)} fill={color}>
                  <title>{`${s.name} · ${p.x}: ${number(p.y)}`}</title>
                </rect>);
            });
          }
          if (kind === "bar") {
            const group = Math.min(band * 0.8, 56 * series.length);
            const w = Math.max(2, group / series.length);
            return s.points.map((p, i) => p.y == null ? null : (
              <rect key={`${s.name}-${p.x}`} x={x(i) - group / 2 + si * w} width={w - 1}
                    y={Math.min(y(p.y), y(0))} height={Math.abs(y(p.y) - y(0))} fill={color}>
                <title>{`${s.name} · ${p.x}: ${number(p.y)}`}</title>
              </rect>));
          }
          const d = s.points.map((p, i) => p.y == null ? "" : `${i && s.points[i - 1].y != null ? "L" : "M"}${x(i)},${y(p.y)}`).join(" ");
          return (
            <g key={s.name}>
              <path d={d} fill="none" stroke={color} strokeWidth={2} />
              {s.points.map((p, i) => p.y == null ? null : (
                <circle key={p.x} cx={x(i)} cy={y(p.y)} r={2.5} fill={color}><title>{`${s.name} · ${p.x}: ${number(p.y)}`}</title></circle>))}
            </g>
          );
        })}
      </svg>
      {series.length > 1 && (
        <div css={{ display: "flex", flexWrap: "wrap", gap: theme.spacing.md, marginTop: theme.spacing.xs }}>
          {series.map((s, si) => (
            <span key={s.name} css={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12 }}>
              <span css={{ width: 10, height: 10, borderRadius: 2, background: PALETTE[si % PALETTE.length] }} />{s.name}
            </span>))}
        </div>
      )}
    </div>
  );
}
