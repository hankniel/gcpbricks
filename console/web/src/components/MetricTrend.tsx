import { Link } from "react-router-dom";
import { Tooltip, Typography, useDesignSystemTheme } from "@databricks/design-system";
import { ModelVersion } from "../api";

const fmt = (val: number) => {
  const a = Math.abs(val);
  if (a >= 1e6) return `${(val / 1e6).toPrecision(3)}M`;
  if (a >= 1e4) return `${(val / 1e3).toPrecision(3)}k`;
  return a >= 100 ? val.toFixed(0) : val.toPrecision(3);
};

/** One metric across versions, oldest left; each point opens its version, an aliased one filled. */
function Trend({ id, metric, versions }: { id: string; metric: string; versions: ModelVersion[] }) {
  const { theme } = useDesignSystemTheme();
  const pts = versions.filter((v) => typeof v.source_run?.metrics[metric] === "number")
    .sort((a, b) => Number(a.version) - Number(b.version))
    .map((v) => ({ v, y: v.source_run!.metrics[metric] }));
  const W = 360, H = 150, PAD = 40;
  const lo = Math.min(...pts.map((p) => p.y)), hi = Math.max(...pts.map((p) => p.y));
  const span = hi - lo || Math.abs(hi) || 1;
  const x = (i: number) => PAD + (pts.length > 1 ? (i / (pts.length - 1)) * (W - 2 * PAD) : (W - 2 * PAD) / 2);
  const y = (val: number) => H - PAD / 2 - ((val - lo) / span) * (H - PAD);
  const stroke = theme.colors.actionPrimaryBackgroundDefault;
  const every = Math.ceil(pts.length / 8);
  return (
    <div css={{ border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd, padding: theme.spacing.sm,
                background: theme.colors.backgroundPrimary, minWidth: 0 }}>
      <Typography.Text bold ellipsis title={metric}>{metric}</Typography.Text>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${metric} by version`} css={{ display: "block" }}>
        {[lo, hi].map((t) => (
          <g key={t}>
            <line x1={PAD} x2={W - PAD} y1={y(t)} y2={y(t)} stroke={theme.colors.border} strokeDasharray="3 3" />
            <text x={PAD - 4} y={y(t)} textAnchor="end" dominantBaseline="middle" fontSize={11} fill={theme.colors.textSecondary}>{fmt(t)}</text>
          </g>
        ))}
        <polyline fill="none" stroke={stroke} strokeWidth={2} points={pts.map((p, i) => `${x(i)},${y(p.y)}`).join(" ")} />
        {pts.map((p, i) => (
          <Tooltip key={p.v.version} componentId="model.trend.point"
                   content={`Version ${p.v.version}${p.v.aliases.length ? ` @${p.v.aliases.join(" @")}` : ""}: ${metric} ${fmt(p.y)}`}>
            <Link to={`/models/${id}/versions/${p.v.version}`}>
              <circle cx={x(i)} cy={y(p.y)} r={p.v.aliases.length ? 5 : 3.5} stroke={stroke} strokeWidth={2}
                      fill={p.v.aliases.length ? stroke : theme.colors.backgroundPrimary} />
              {(i % every === 0 || i === pts.length - 1) &&
                <text x={x(i)} y={H - 2} textAnchor="middle" fontSize={11} fill={theme.colors.textSecondary}>v{p.v.version}</text>}
            </Link>
          </Tooltip>
        ))}
      </svg>
    </div>
  );
}

/** Every source-run metric across a model's versions, one chart each, as MLflow's chart view
 *  lays out a run's metrics: does a retrain hold up? */
export default function MetricTrend({ id, versions }: { id: string; versions: ModelVersion[] }) {
  const { theme } = useDesignSystemTheme();
  const names = [...new Set(versions.flatMap((v) => Object.keys(v.source_run?.metrics ?? {})))].sort();
  if (!names.length) return null;
  return (
    <div>
      <Typography.Title level={4}>Metrics across versions <Typography.Text color="secondary">({names.length})</Typography.Text></Typography.Title>
      <div css={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: theme.spacing.md }}>
        {names.map((n) => <Trend key={n} id={id} metric={n} versions={versions} />)}
      </div>
    </div>
  );
}
