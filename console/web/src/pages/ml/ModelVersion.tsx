import { Fragment, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Alert, Button, CopyIcon, Empty, Input, ModelsIcon, Spacer, TableCell, TableRow, TableSkeleton, Tooltip,
  Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../../components/Table";
import { api, Model } from "../../api";
import { Field, Section } from "../../components/Panel";
import PageTitle from "../../components/PageTitle";
import SidePanel from "../../components/SidePanel";
import { Status } from "../../components/Status";
import LineageTab from "../../components/LineageTab";
import { Tile, usd } from "../../components/TaskMetrics";
import { duration, fmtValue, secondsText, useLoad, when } from "../../util";
import { AliasTags, useAliases } from "./Model";

/** What the version trained on and where its batch scoring lands, as Databricks' version Lineage
 * tab shows; with its predict job's latest runs. */
function VersionLineageSection({ id, version }: { id: string; version: string }) {
  const { theme } = useDesignSystemTheme();
  const me = useLoad(api.me, []);
  const { data, error } = useLoad(() => api.versionLineage(id, version), [id, version]);
  const f = data?.flow;
  return (
    <>
      <Typography.Title level={4}>Lineage</Typography.Title>
      {error && <Alert componentId="version.lineage.error" type="error" message="Couldn't load lineage" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={3} />}
      {data && !f && <Typography.Text color="secondary">This version's training recorded no manifest (train_window.json), so its inputs and outputs aren't known.</Typography.Text>}
      {f && (
        <>
          <Typography.Text color="secondary">
            Trained on install dates {f.train_start ?? "?"} to {f.train_cutoff ?? "?"}
            {f.predict_job && <>; scored by the Cloud Run job <Typography.Text code>{f.predict_job}</Typography.Text></>}.
          </Typography.Text>
          <Spacer size="sm" />
          {me.data && <LineageTab center={`model:${id}@${version}`} title={`${f.name} version ${version}`} project={me.data.project} />}
          {f.predict_job && (
            <>
              <Spacer size="md" />
              <Typography.Text bold>Latest runs of {f.predict_job}</Typography.Text>
              <Spacer size="xs" />
              {data.runs.length === 0 ? <Typography.Text color="secondary">No runs found.</Typography.Text> : (
                <Table scrollable>
                  <TableRow isHeader>
                    <TableHeader componentId="version.r.h.start">Start time</TableHeader>
                    <TableHeader componentId="version.r.h.dur">Duration</TableHeader>
                    <TableHeader componentId="version.r.h.state">Status</TableHeader>
                    <TableHeader componentId="version.r.h.id">Execution</TableHeader>
                  </TableRow>
                  {data.runs.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell>{when(r.start)}</TableCell>
                      <TableCell>{duration(r.start, r.end)}</TableCell>
                      <TableCell><Status state={r.state} /></TableCell>
                      <TableCell><Typography.Text color="secondary" css={{ fontSize: theme.typography.fontSizeSm }}>{r.id}</Typography.Text></TableCell>
                    </TableRow>
                  ))}
                </Table>
              )}
            </>
          )}
        </>
      )}
    </>
  );
}

/** A list of column names with a filter, the ones in `marked` tagged with `mark`. */
function Names({ title, names, marked = [], mark, hint }: { title: string; names: string[]; marked?: string[]; mark?: string; hint?: string }) {
  const { theme } = useDesignSystemTheme();
  const [q, setQ] = useState("");
  const shown = names.filter((n) => n.toLowerCase().includes(q.toLowerCase()));
  const tagged = new Set(marked);
  return (
    <div css={{ flex: "1 1 260px", minWidth: 0, border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd,
                padding: theme.spacing.sm }}>
      <div css={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: theme.spacing.sm }}>
        <Typography.Text bold>{title}</Typography.Text>
        <Typography.Text color="secondary" size="sm">{names.length}</Typography.Text>
      </div>
      {hint && <Typography.Text color="secondary" size="sm">{hint}</Typography.Text>}
      {names.length > 12 && <div css={{ margin: `${theme.spacing.xs}px 0` }}>
        <Input componentId="version.features.filter" size="small" placeholder="Filter" value={q} onChange={(e) => setQ(e.target.value)} allowClear /></div>}
      <div css={{ maxHeight: 260, overflow: "auto", marginTop: theme.spacing.xs }}>
        {shown.map((n) => (
          <div key={n} css={{ display: "flex", justifyContent: "space-between", gap: theme.spacing.xs, fontSize: theme.typography.fontSizeSm }}>
            <Typography.Text code css={{ fontSize: theme.typography.fontSizeSm }}>{n}</Typography.Text>
            {tagged.has(n) && mark && <Typography.Text color="secondary" size="sm">{mark}</Typography.Text>}
          </div>))}
      </div>
    </div>
  );
}

/** What the version reads before any transform and what its estimator was fit on: names and counts. */
function VersionFeaturesSection({ id, version }: { id: string; version: string }) {
  const { theme } = useDesignSystemTheme();
  const { data, error } = useLoad(() => api.versionFeatures(id, version), [id, version]);
  const sets = Object.entries(data?.model_inputs ?? {});
  return (
    <>
      <Typography.Title level={4}>Features</Typography.Title>
      {error && <Alert componentId="version.features.error" type="error" message="Couldn't read this version's features" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={3} />}
      {data && data.read == null && sets.length === 0 && (
        <Typography.Text color="secondary">
          This version's artifacts say nothing of its features: no features.json, signature or feature_spec.json. A model logged
          with <Typography.Text code>mlflow.pyfunc.log_model(..., features=pipeline)</Typography.Text> or a signature has them;
          one logged by hand calls <Typography.Text code>mlflow.log_features(read=[...], model_inputs=[...])</Typography.Text>.
        </Typography.Text>)}
      {data && (data.read != null || sets.length > 0) && (
        <>
          <Typography.Text color="secondary">
            {data.logged ? "From the version's features.json" : "Derived from its MLmodel signature and feature_spec.json (no features.json)"}
            {Object.keys(data.sources).length > 0 && ` · ${Object.entries(data.sources).map(([k, s]) => `${k.replace("_", " ")}: ${s}`).join(", ")}`}.
          </Typography.Text>
          <Spacer size="sm" />
          <div css={{ display: "flex", gap: theme.spacing.sm, flexWrap: "wrap" }}>
            {data.read != null && (
              <Names title="Read at fetch" names={data.read} marked={data.read_not_fit} mark="not fit on"
                     hint="Columns the training read selected, before any transform." />)}
            {sets.map(([set, names]) => (
              <Names key={set} title={sets.length > 1 || set !== "model" ? `Fit on · ${set}` : "Fit on"} marked={data.fit_not_read}
                     mark="computed" names={names}
                     hint={sets.length > 1 ? "One input set or submodel." : "The columns the estimator was trained on."} />))}
            {data.computed && data.computed.length > 0 && (
              <Names title="Computed" names={data.computed} hint="Features the pipeline derives from what it read." />)}
          </div>
        </>)}
    </>
  );
}

const pct = (v?: number | null) => (v == null ? "-" : `${(v * 100).toFixed(v < 0.01 && v > 0 ? 2 : 1)}%`);
const num = (v?: number | string | null) => (v == null ? "-" : typeof v === "string" ? v.slice(0, 19) : fmtValue(v));

/** What the version trained on (its training_summary.json) and what training cost. */
function VersionTrainingSection({ id, version }: { id: string; version: string }) {
  const { theme } = useDesignSystemTheme();
  const { data, error } = useLoad(() => api.versionTraining(id, version), [id, version]);
  const [q, setQ] = useState("");
  const [all, setAll] = useState(false);
  const s = data?.summary;
  const c = data?.cost;
  const rank = new Map((s?.importance?.top ?? []).map((t, i) => [t.name, i]));
  const stats = Object.entries(s?.features.columns ?? {})
    .filter(([n]) => n.toLowerCase().includes(q.toLowerCase()))
    .sort(([a], [b]) => (rank.get(a) ?? 1e9) - (rank.get(b) ?? 1e9) || a.localeCompare(b));
  const bar = theme.colors.actionPrimaryBackgroundDefault;
  return (
    <>
      <Typography.Title level={4}>Training</Typography.Title>
      {error && <Alert componentId="version.training.error" type="error" message="Couldn't read this version's training" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={3} />}
      {data && !s && (
        <Typography.Text color="secondary">
          No training_summary.json beside this version. A run logs one with
          <Typography.Text code>mlflow.pyfunc.log_model(..., training=mlflow.training_summary(train=..., valid=..., label=..., model=...))</Typography.Text>
          or <Typography.Text code>mlflow.log_training(summary)</Typography.Text>.
        </Typography.Text>)}
      {s && (
        <>
          <div css={{ display: "flex", gap: theme.spacing.sm, flexWrap: "wrap" }}>
            {Object.entries(s.data.splits).map(([k, v]) => (
              <Tile key={k} name={`${k} rows`} value={v.rows.toLocaleString("en-US")}
                    hint={v.from || v.to ? `${num(v.from)} → ${num(v.to)}` : undefined} />))}
            {c && <Tile name="Training cost" value={usd(c.usd)}
                        hint={`${c.seconds != null ? secondsText(c.seconds) : "?"} on ${c.machine}`} />}
            {c?.pipeline && <Tile name="Whole pipeline run" value={usd(c.pipeline.usd)} hint={`${c.pipeline.steps} steps, ${secondsText(c.pipeline.seconds)}`} />}
          </div>
          <Spacer size="xs" />
          <Typography.Text color="secondary" size="sm">
            {[s.data.source.snapshot && `snapshot ${s.data.source.snapshot}`, s.data.source.query_sha256 && `query ${s.data.source.query_sha256}`,
              s.data.time_column && `time column ${s.data.time_column}`, s.execution ? `trained in ${s.execution}` : "trained outside the platform (no cost known)",
              `summarized ${when(s.created, true)}`].filter(Boolean).join(" · ")}
            {data.cost_error && ` · cost unknown: ${data.cost_error}`}
            {c && ` · ${c.price_source}`}
          </Typography.Text>
          {s.label && (
            <>
              <Spacer size="md" />
              <Typography.Text bold>Label <Typography.Text code>{s.label.name}</Typography.Text> ({s.label.kind})</Typography.Text>
              {s.label.definition && <div><Typography.Text color="secondary">{s.label.definition}</Typography.Text></div>}
              <div css={{ display: "flex", gap: theme.spacing.sm, flexWrap: "wrap", marginTop: theme.spacing.xs }}>
                {Object.entries(s.label.splits).map(([k, v]) => (
                  <Tile key={k} name={k}
                        value={s.label!.kind === "binary" ? `${pct(v.positive_rate as number)} positive`
                          : s.label!.kind === "numeric" ? `mean ${num(v.mean as number)}`
                          : ((v.top as [string, number][] | undefined)?.[0]?.[0] ?? "-")}
                        hint={s.label!.kind === "numeric" ? `p50 ${num(v.p50 as number)}, zero ${pct(v.zero_rate as number)}` : undefined} />))}
              </div>
            </>)}
          {s.importance && (
            <>
              <Spacer size="md" />
              <Typography.Text bold>Feature importance</Typography.Text>
              <Typography.Text color="secondary" size="sm"> · {s.importance.kind}, top {s.importance.top.length} of {s.importance.features}</Typography.Text>
              <div css={{ marginTop: theme.spacing.xs, display: "grid", gridTemplateColumns: "minmax(120px, 280px) 1fr 56px", gap: "2px 8px", alignItems: "center" }}>
                {s.importance.top.slice(0, 15).map((t) => (
                  <Fragment key={t.name}>
                    <Typography.Text code ellipsis css={{ fontSize: theme.typography.fontSizeSm }}>{t.name}</Typography.Text>
                    <div css={{ height: 8, background: theme.colors.backgroundSecondary, borderRadius: 4 }}>
                      <div css={{ height: 8, width: `${(t.share ?? 0) / (s.importance!.top[0].share || 1) * 100}%`, background: bar, borderRadius: 4 }} />
                    </div>
                    <Typography.Text color="secondary" size="sm" css={{ textAlign: "right" }}>{pct(t.share)}</Typography.Text>
                  </Fragment>))}
              </div>
            </>)}
          <Spacer size="md" />
          <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: theme.spacing.sm }}>
            <Typography.Text bold>Features at training time
              <Typography.Text color="secondary" size="sm"> · {Object.keys(s.features.columns).length} columns,
                {s.features.sampled ? ` a sample of ${s.features.rows_used.toLocaleString("en-US")} rows` : " every row"}: the baseline for drift</Typography.Text>
            </Typography.Text>
            <div css={{ width: 240 }}><Input componentId="version.training.filter" size="small" placeholder="Filter features" value={q} onChange={(e) => setQ(e.target.value)} allowClear /></div>
          </div>
          <Spacer size="xs" />
          <Table scrollable>
            <TableRow isHeader>
              <TableHeader componentId="version.t.h.name">Feature</TableHeader>
              <TableHeader componentId="version.t.h.kind" css={{ flex: "0 0 100px" }}>Kind</TableHeader>
              <TableHeader componentId="version.t.h.null" align="right" css={{ flex: "0 0 80px" }}>Null</TableHeader>
              <TableHeader componentId="version.t.h.dist">Distribution</TableHeader>
            </TableRow>
            {(all ? stats : stats.slice(0, 50)).map(([n, f]) => (
              <TableRow key={n}>
                <TableCell><Typography.Text code>{n}</Typography.Text></TableCell>
                <TableCell css={{ flex: "0 0 100px" }}>{f.kind}</TableCell>
                <TableCell css={{ flex: "0 0 80px", justifyContent: "flex-end" }}>{pct(f.null_rate)}</TableCell>
                <TableCell>
                  <Typography.Text color="secondary" size="sm">
                    {f.kind === "numeric" ? `mean ${num(f.mean)} · min ${num(f.min)} · p25 ${num(f.p25)} · p50 ${num(f.p50)} · p75 ${num(f.p75)} · p99 ${num(f.p99)} · max ${num(f.max)}`
                      : f.kind === "categorical" ? `${f.distinct} distinct · ${(f.top ?? []).map(([v, sh]) => `${v} ${pct(sh)}`).join(", ")}`
                      : f.kind === "boolean" ? `true ${pct(f.true_rate)}` : `${num(f.min)} → ${num(f.max)}`}
                  </Typography.Text>
                </TableCell>
              </TableRow>))}
          </Table>
          {!all && stats.length > 50 && <Button componentId="version.t.all" type="link" onClick={() => setAll(true)}>Show all {stats.length}</Button>}
        </>)}
    </>
  );
}

/** Databricks' model version page: where the version came from, how it scored, where it serves. */
export default function ModelVersionPage() {
  const { id = "", version = "" } = useParams();
  const { theme } = useDesignSystemTheme();
  const { data: m, error, reload } = useLoad<Model>(() => api.model(id), [id]);
  const aliases = useAliases(id, reload);
  const v = m?.versions.find((x) => x.version === version);
  const run = v?.source_run;
  const schemas = v ? Object.entries(v.schemata).filter(([, uri]) => uri) : [];
  return (
    <>
      <PageTitle crumbs={[{ to: "/models", label: "Models" }, { to: `/models/${id}`, label: m?.name ?? id }]}
        icon={<ModelsIcon />} title={`Version ${version}`} addOns={v && <AliasTags v={v} />}
        actions={v && <Button componentId="version.alias" onClick={() => aliases.ask(v.version)}>Set alias</Button>} />
      <Spacer size="md" />
      {error && <Alert componentId="version.error" type="error" message="Couldn't load this model" description={error} closable={false} />}
      {aliases.banner}
      {!m && !error && <TableSkeleton lines={8} />}
      {m && !v && <Empty title="No such version" description={`${m.name} has no version ${version}.`} />}
      {v && (
        <SidePanel>
          <div css={{ minWidth: 0 }}>
            <Typography.Title level={4}>Description</Typography.Title>
            <Typography.Paragraph color={v.description ? undefined : "secondary"}>{v.description || "No description"}</Typography.Paragraph>
            <Spacer size="sm" />
            <Typography.Title level={4}>Source run</Typography.Title>
            {run ? (
              <>
                <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm, flexWrap: "wrap" }}>
                  <Typography.Text><Link to={`/experiments/${run.experiment}/runs/${run.id}`}>{run.name}</Link></Typography.Text>
                  {run.state && <Status state={run.state} />}
                  <Typography.Text color="secondary">in experiment <Link to={`/experiments/${run.experiment}`}>{run.experiment}</Link></Typography.Text>
                </div>
                <Spacer size="md" />
                <Typography.Text bold>Metrics</Typography.Text>
                <Spacer size="xs" />
                {Object.keys(run.metrics).length === 0 ? <Typography.Text color="secondary">The run logged no metrics.</Typography.Text> : (
                  <Table scrollable>
                    <TableRow isHeader>
                      <TableHeader componentId="version.m.h.name">Metric</TableHeader>
                      <TableHeader componentId="version.m.h.value" align="right">Value</TableHeader>
                    </TableRow>
                    {Object.entries(run.metrics).sort(([a], [b]) => a.localeCompare(b)).map(([k, val]) => (
                      <TableRow key={k}>
                        <TableCell>{k}</TableCell>
                        <TableCell css={{ justifyContent: "flex-end" }}>{fmtValue(val)}</TableCell>
                      </TableRow>
                    ))}
                  </Table>
                )}
                <Spacer size="md" />
                <Typography.Text bold>Parameters</Typography.Text>
                <Spacer size="xs" />
                {Object.keys(run.params ?? {}).length === 0 ? <Typography.Text color="secondary">The run logged no parameters.</Typography.Text> : (
                  <Table scrollable>
                    <TableRow isHeader>
                      <TableHeader componentId="version.p.h.name">Parameter</TableHeader>
                      <TableHeader componentId="version.p.h.value">Value</TableHeader>
                    </TableRow>
                    {Object.entries(run.params).sort(([a], [b]) => a.localeCompare(b)).map(([k, val]) => (
                      <TableRow key={k}>
                        <TableCell>{k}</TableCell>
                        <TableCell><Typography.Text code>{String(val)}</Typography.Text></TableCell>
                      </TableRow>
                    ))}
                  </Table>
                )}
              </>
            ) : (
              <Typography.Text color="secondary">
                {v.labels.mlflow_run_id
                  ? <>Its training run <Typography.Text code>{v.labels.mlflow_run_id}</Typography.Text> isn't in experiment <Typography.Text code>{v.labels.mlflow_experiment}</Typography.Text>.</>
                  : <>The version names no training run (no <Typography.Text code>mlflow_run_id</Typography.Text> tag), and no run
                    in {m?.experiment ? <Link to={`/experiments/${m.experiment}`}>{m.experiment}</Link> : "this model's experiment"} is named after its model_id tag
                    {v.labels.model_id ? <> (<Typography.Text code>{v.labels.model_id}</Typography.Text>)</> : ", which it lacks too"}.</>}
              </Typography.Text>
            )}
            <Spacer size="lg" />
            <VersionFeaturesSection id={id} version={version} />
            <Spacer size="lg" />
            <VersionTrainingSection id={id} version={version} />
            <Spacer size="lg" />
            <VersionLineageSection id={id} version={version} />
            <Spacer size="lg" />
            <Typography.Title level={4}>Serving</Typography.Title>
            {v.serving.length === 0 ? <Typography.Text color="secondary">No serving endpoint serves this version.</Typography.Text> : (
              <Table scrollable>
                <TableRow isHeader>
                  <TableHeader componentId="version.s.h.name">Endpoint</TableHeader>
                  <TableHeader componentId="version.s.h.traffic" align="right">Traffic</TableHeader>
                </TableRow>
                {v.serving.map((e) => (
                  <TableRow key={e.id}>
                    <TableCell><Link to={`/serving/${e.id}`}>{e.name}</Link></TableCell>
                    <TableCell css={{ justifyContent: "flex-end" }}>{e.traffic}%</TableCell>
                  </TableRow>
                ))}
              </Table>
            )}
            {schemas.length > 0 && (
              <>
                <Spacer size="lg" />
                <Typography.Title level={4}>Schema</Typography.Title>
                {schemas.map(([k, uri]) => <Field key={k} name={k.replace("SchemaUri", "")}><Typography.Text code>{uri}</Typography.Text></Field>)}
              </>
            )}
          </div>
          <div>
            <Section title="Details">
              <Field name="Model"><Link to={`/models/${id}`}>{m?.name}</Link></Field>
              <Field name="Version">{v.version}{v.default ? " (Vertex default)" : ""}</Field>
              <Field name="Registered at">{when(v.created, true)}</Field>
              <Field name="Last modified">{when(v.updated, true)}</Field>
              <Field name="Source">{v.source ? v.source.charAt(0) + v.source.slice(1).toLowerCase() : "-"}</Field>
              <Field name="Container">{v.image ? <Typography.Text code>{v.image}</Typography.Text> : "-"}</Field>
              <Field name="Artifact location">
                <span css={{ display: "inline-flex", alignItems: "flex-start", gap: theme.spacing.xs }}>
                  <Typography.Text code>{v.artifact_uri || "-"}</Typography.Text>
                  {v.artifact_uri && <Tooltip componentId="version.copy.tip" content="Copy artifact location">
                    <Button componentId="version.copy" size="small" icon={<CopyIcon />} aria-label="Copy artifact location"
                            onClick={() => navigator.clipboard.writeText(v.artifact_uri)} /></Tooltip>}
                </span>
              </Field>
            </Section>
            <Section title="Aliases">
              {v.aliases.length ? v.aliases.map((a) => (
                <div key={a} css={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <Typography.Text>@{a}</Typography.Text>
                  <Button componentId="version.unalias" size="small" type="tertiary" onClick={() => aliases.apply(a, null)}>Remove</Button>
                </div>)) : <Typography.Text color="secondary">No aliases</Typography.Text>}
            </Section>
            <Section title="Tags">
              {Object.keys(v.labels).length ? Object.entries(v.labels).sort(([a], [b]) => a.localeCompare(b))
                .map(([k, val]) => <Field key={k} name={k}>{val}</Field>) : <Typography.Text color="secondary">No tags</Typography.Text>}
            </Section>
          </div>
        </SidePanel>
      )}
      {aliases.modal}
    </>
  );
}
