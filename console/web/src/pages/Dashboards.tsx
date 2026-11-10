import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Alert, DashboardIcon, Empty, FormUI, SegmentedControlButton, SegmentedControlGroup, SimpleSelect,
  SimpleSelectOption, Spacer, TableCell, TableRow, TableSkeleton, Tag, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../components/Table";
import { api, type DashboardFilter, type WidgetSpec } from "../api";
import Chart, { number, toSeries } from "../components/Chart";
import PageTitle from "../components/PageTitle";
import { useLoad, when } from "../util";

const WINDOWS = ["24h", "7d", "30d", "90d"];

/** A text widget's text, with Markdown-style [label](https://...) links as links. */
function TextWithLinks({ text }: { text: string }) {
  const parts = text.split(/(\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g);
  return (
    <Typography.Paragraph css={{ whiteSpace: "pre-wrap", "&&": { margin: 0 } }}>
      {parts.map((part, i) => {
        const m = /^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/.exec(part);
        return m ? <a key={i} href={m[2]} target="_blank" rel="noreferrer">{m[1]}</a> : part;
      })}
    </Typography.Paragraph>
  );
}

/** A page filter: a dropdown of its field's values in the window; "All" picks nothing. */
function FilterSelect({ bundle, target, dashboard, name, filter, window, value, onChange }: {
  bundle: string; target: string; dashboard: string; name: string; filter: DashboardFilter; window: string;
  value: string; onChange: (v: string) => void;
}) {
  const { data } = useLoad(() => api.dashboardFilter(bundle, target, dashboard, name, window), [bundle, target, dashboard, name, window]);
  return (
    <div>
      <FormUI.Label htmlFor={`filter-${name}`}>{filter.label ?? name}</FormUI.Label>
      <SimpleSelect componentId="dashboard.filter" id={`filter-${name}`} value={value || "__all"} width="100%"
                    onChange={(e) => onChange(e.target.value === "__all" ? "" : e.target.value)}>
        <SimpleSelectOption value="__all">All</SimpleSelectOption>
        {(data ?? []).map((v) => <SimpleSelectOption key={v} value={v}>{v}</SimpleSelectOption>)}
      </SimpleSelect>
    </div>
  );
}

export function DashboardsPage() {
  const { data, error } = useLoad(api.dashboards, []);
  return (
    <>
      <PageTitle title="Dashboards" />
      <Spacer size="md" />
      {error && <Alert componentId="dashboards.error" type="error" message="Couldn't load dashboards" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={4} />}
      {data && (
        <Table scrollable empty={data.length ? undefined : <Empty title="No dashboards"
               description="A bundle declares one under resources.dashboards; it appears here once deployed." />}>
          <TableRow isHeader>
            <TableHeader componentId="dashboards.h.title">Name</TableHeader>
            <TableHeader componentId="dashboards.h.bundle">Bundle</TableHeader>
            <TableHeader componentId="dashboards.h.widgets">Widgets</TableHeader>
            <TableHeader componentId="dashboards.h.updated">Deployed</TableHeader>
          </TableRow>
          {data.map((d) => (
            <TableRow key={d.object}>
              <TableCell><Link to={`/dashboards/${d.bundle}/${d.target}/${d.key}`}>{d.title}</Link></TableCell>
              <TableCell><Tag componentId="dashboards.bundle">{d.bundle}</Tag> <Tag componentId="dashboards.target">{d.target}</Tag></TableCell>
              <TableCell>{d.widgets}</TableCell>
              <TableCell>{when(d.updated, true)}</TableCell>
            </TableRow>
          ))}
        </Table>
      )}
    </>
  );
}

export function DashboardPage() {
  const { bundle = "", target = "", key = "" } = useParams();
  const { theme } = useDesignSystemTheme();
  const { data, error } = useLoad(() => api.dashboard(bundle, target, key), [bundle, target, key]);
  const [picked, setPicked] = useState<string | null>(null);
  const [filters, setFilters] = useState<Record<string, string>>({});
  const window = picked ?? data?.spec.window ?? "30d";
  return (
    <>
      <PageTitle crumbs={[{ to: "/dashboards", label: "Dashboards" }]} icon={<DashboardIcon />} title={data?.title ?? key}
        actions={data && (
          <SegmentedControlGroup componentId="dashboard.window" name="window" value={window} onChange={(e) => setPicked(e.target.value)}>
            {[...new Set([...WINDOWS, data.spec.window])].map((w) => <SegmentedControlButton key={w} value={w}>{w}</SegmentedControlButton>)}
          </SegmentedControlGroup>)} />
      {data?.spec.filters && Object.keys(data.spec.filters).length > 0 && (
        <div css={{ display: "flex", flexWrap: "wrap", gap: theme.spacing.md, marginTop: theme.spacing.md }}>
          {Object.entries(data.spec.filters).map(([name, f]) => (
            <div key={name} css={{ width: 240 }}>
              <FilterSelect bundle={bundle} target={target} dashboard={key} name={name} filter={f} window={window}
                            value={filters[name] ?? ""} onChange={(v) => setFilters({ ...filters, [name]: v })} />
            </div>))}
        </div>
      )}
      <Spacer size="lg" />
      {error && <Alert componentId="dashboard.error" type="error" message="Couldn't load this dashboard" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={8} />}
      {data && (
        <div css={{ display: "grid", gridTemplateColumns: "repeat(12, minmax(0, 1fr))", gap: theme.spacing.md }}>
          {data.spec.widgets.map((w, i) => (
            <div key={i} css={{ gridColumn: `span ${w.width}`, minWidth: 0, border: `1px solid ${theme.colors.border}`,
                                borderRadius: theme.borders.borderRadiusMd, padding: theme.spacing.md,
                                background: theme.colors.backgroundPrimary }}>
              <Typography.Text bold>{w.title}</Typography.Text>
              <Spacer size="sm" />
              <Widget bundle={bundle} target={target} dashboard={key} index={i} spec={w} filters={filters}
                      window={w.window && !picked ? w.window : window} />
            </div>
          ))}
        </div>
      )}
    </>
  );
}

function Widget({ bundle, target, dashboard, index, spec, window, filters }: {
  bundle: string; target: string; dashboard: string; index: number; spec: WidgetSpec; window: string;
  filters: Record<string, string>;
}) {
  const { theme } = useDesignSystemTheme();
  const picks = JSON.stringify(filters);
  const { data, error } = useLoad(() => api.dashboardWidget(bundle, target, dashboard, index, window, filters),
                                  [bundle, target, dashboard, index, window, picks], 120000);
  if (error) return <Typography.Text color="error">{error}</Typography.Text>;
  if (!data) return <TableSkeleton lines={3} />;
  if (data.type === "text") return <TextWithLinks text={data.text ?? ""} />;
  if (data.type === "counter") {
    return (
      <div>
        <div css={{ fontSize: 36, fontWeight: 600, lineHeight: 1.2 }}>{number(data.rows?.[0]?.[data.rows[0].length - 1])}</div>
        <Typography.Text color="secondary">{spec.measure} · last {data.window}</Typography.Text>
      </div>
    );
  }
  if (data.type === "line" || data.type === "bar") {
    const { xs, series } = toSeries(data.columns ?? [], data.rows ?? []);
    return <Chart kind={data.type} xs={xs} series={series} />;
  }
  if (data.type === "table") {
    return (
      <div css={{ maxHeight: 320, overflow: "auto" }}>
        <Table empty={data.rows?.length ? undefined : <Empty description="No data in this window." />}>
          <TableRow isHeader>{(data.columns ?? []).map((c) => <TableHeader key={c} componentId="widget.h">{c}</TableHeader>)}</TableRow>
          {(data.rows ?? []).map((r, ri) => (
            <TableRow key={ri}>{r.map((v, ci) => <TableCell key={ci}>{typeof v === "number" ? number(v) : (v ?? "-")}</TableCell>)}</TableRow>
          ))}
        </Table>
      </div>
    );
  }
  if (data.type === "alerts") {
    return data.alerts?.length ? (
      <div>{data.alerts.map((a) => (
        <div key={a.name || a.policy} css={{ padding: `${theme.spacing.xs}px 0`, borderBottom: `1px solid ${theme.colors.border}` }}>
          <Tag componentId="widget.alert" color="coral">{a.severity || "OPEN"}</Tag> {a.policy}
          <Typography.Text color="secondary"> · opened {when(a.opened)}</Typography.Text>
        </div>))}</div>
    ) : <Typography.Text color="secondary">No open alerts.</Typography.Text>;
  }
  return data.lines?.length ? (
    <div css={{ maxHeight: 320, overflow: "auto", fontFamily: "monospace", fontSize: 12 }}>
      {data.lines.map((l, i) => (
        <div key={i} css={{ display: "grid", gridTemplateColumns: "150px 76px 1fr", gap: theme.spacing.sm, padding: "2px 0" }}>
          <span css={{ color: theme.colors.textSecondary }}>{when(l.time)}</span>
          <span css={{ color: l.severity === "ERROR" || l.severity === "CRITICAL" ? theme.colors.textValidationDanger
                                : theme.colors.textValidationWarning }}>{l.severity}</span>
          <span css={{ overflowWrap: "anywhere" }}>{l.text}</span>
        </div>))}
    </div>
  ) : <Typography.Text color="secondary">Nothing logged in this window.</Typography.Text>;
}
