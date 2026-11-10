import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Alert, BeakerIcon, Empty, Input, ParagraphSkeleton, SearchIcon, Spacer, TableCell, TableRow, TableSkeleton,
  Tag, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../../components/Table";
import { api, ExpRun, Model } from "../../api";
import { Field, Section } from "../../components/Panel";
import PageTitle from "../../components/PageTitle";
import { Status } from "../../components/Status";
import { fmtValue, TIME_ZONE_LABEL, useLoad, when } from "../../util";
import { MetricBars } from "./Experiment";

/** A parent run's children, as MLflow lists a sweep's trials: their status, the parent's metric
 *  (a tuning.sweep logs which, and its goal) with the best child tagged, and their params. */
function ChildRuns({ parent, runs, expId }: { parent: ExpRun; runs: ExpRun[]; expId: string }) {
  const { theme } = useDesignSystemTheme();
  const metric = typeof parent.params.metric === "string" ? parent.params.metric
    : [...new Set(runs.flatMap((c) => Object.keys(c.metrics)))].sort()[0];
  const goal = parent.params.goal;
  const scored = runs.filter((c) => metric && typeof c.metrics[metric] === "number");
  const best = (goal === "maximize" || goal === "minimize") && scored.length
    ? scored.reduce((b, c) => ((goal === "maximize" ? c.metrics[metric] > b.metrics[metric] : c.metrics[metric] < b.metrics[metric]) ? c : b))
    : undefined;
  const params = [...new Set(runs.flatMap((c) => Object.keys(c.params)))].filter((k) => !k.startsWith("tag_") && k !== "status").sort().slice(0, 6);
  return (
    <div css={{ display: "grid", gridTemplateColumns: "minmax(0, 2fr) minmax(320px, 1fr)", gap: theme.spacing.lg, alignItems: "start" }}>
      <div css={{ minWidth: 0 }}>
        <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>Child runs <Typography.Text color="secondary">({runs.length})</Typography.Text></Typography.Title>
        <Spacer size="sm" />
        <Table scrollable>
          <TableRow isHeader>
            <TableHeader componentId="run.child.h.name" css={{ flex: "2 1 220px" }}>Run Name</TableHeader>
            <TableHeader componentId="run.child.h.state">Status</TableHeader>
            {metric && <TableHeader componentId="run.child.h.metric" align="right">{metric}</TableHeader>}
            {params.map((k) => <TableHeader key={k} componentId="run.child.h.param" title={k}>{k}</TableHeader>)}
          </TableRow>
          {runs.map((c) => (
            <TableRow key={c.id}>
              <TableCell css={{ flex: "2 1 220px" }}>
                <span css={{ display: "inline-flex", verticalAlign: "top", alignItems: "center", gap: theme.spacing.xs, minWidth: 0 }}>
                  <Link css={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={c.name} to={`/experiments/${expId}/runs/${c.id}`}>{c.name}</Link>
                  {best?.id === c.id && <Tag componentId="run.child.best" color="lime" css={{ flex: "none" }}>Best</Tag>}
                </span>
              </TableCell>
              <TableCell>{c.state ? <Status state={c.state} /> : "-"}</TableCell>
              {metric && <TableCell css={{ justifyContent: "flex-end" }}>{metric in c.metrics ? fmtValue(c.metrics[metric]) : "-"}</TableCell>}
              {params.map((k) => <TableCell key={k}><Typography.Text ellipsis>{k in c.params ? fmtValue(c.params[k]) : "-"}</Typography.Text></TableCell>)}
            </TableRow>
          ))}
        </Table>
      </div>
      {metric && <MetricBars metric={metric} runs={runs} expId={expId} />}
    </div>
  );
}

function KeyValues({ title, values, componentId }: { title: string; values: Record<string, unknown>; componentId: string }) {
  const { theme } = useDesignSystemTheme();
  const [q, setQ] = useState("");
  const rows = Object.entries(values).filter(([k]) => k.toLowerCase().includes(q.toLowerCase())).sort(([a], [b]) => a.localeCompare(b));
  return (
    <div css={{ minWidth: 0 }}>
      <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: theme.spacing.sm }}>
        <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>{title} <Typography.Text color="secondary">({Object.keys(values).length})</Typography.Text></Typography.Title>
        <div css={{ width: 220 }}>
          <Input componentId={`${componentId}.filter`} size="small" prefix={<SearchIcon />} placeholder={`Search ${title.toLowerCase()}`}
                 value={q} onChange={(e) => setQ(e.target.value)} allowClear aria-label={`Search ${title.toLowerCase()}`} />
        </div>
      </div>
      <Spacer size="sm" />
      <Table scrollable empty={rows.length ? undefined : <Empty title={q ? "No match" : `No ${title.toLowerCase()}`}
                                                              description={q ? "Clear the search to see them all." : `The run logged no ${title.toLowerCase()}.`} />}>
        <TableRow isHeader>
          <TableHeader componentId={`${componentId}.h.key`}>{title === "Metrics" ? "Metric" : "Parameter"}</TableHeader>
          <TableHeader componentId={`${componentId}.h.value`} align={title === "Metrics" ? "right" : undefined}>Value</TableHeader>
        </TableRow>
        {rows.map(([k, v]) => (
          <TableRow key={k}>
            <TableCell><Typography.Text ellipsis title={k}>{k}</Typography.Text></TableCell>
            <TableCell css={title === "Metrics" ? { justifyContent: "flex-end" } : undefined}><Typography.Text ellipsis title={fmtValue(v)}>{fmtValue(v)}</Typography.Text></TableCell>
          </TableRow>
        ))}
      </Table>
    </div>
  );
}

/** MLflow's run page: what the run is, and every metric and parameter it logged. */
export default function ExperimentRunPage() {
  const { id = "", run = "" } = useParams();
  const { theme } = useDesignSystemTheme();
  const exp = useLoad(() => api.experiment(id), [id]);
  // The model trained in this experiment carries its name with _ for - (ml.model_experiment).
  const model = useLoad<Model | null>(async () => {
    const m = (await api.models()).find((x) => x.name === id.replace(/-/g, "_"));
    return m ? api.model(m.id) : null;
  }, [id]);
  const r = exp.data?.runs.find((x) => x.id === run);
  const parent = r?.parent ? exp.data?.runs.find((x) => x.name === r.parent && !x.parent) : undefined;
  const children = r ? (exp.data?.runs ?? []).filter((x) => x.parent === r.name)
    .sort((a, b) => (b.created ?? "").localeCompare(a.created ?? "")) : [];
  const versions = (model.data?.versions ?? []).filter((v) => v.source_run?.id === run);
  return (
    <>
      <PageTitle crumbs={[{ to: "/experiments", label: "Experiments" }, { to: `/experiments/${id}`, label: exp.data?.name ?? id }]}
                 icon={<BeakerIcon />} title={r?.name ?? run} addOns={r?.state ? <Status state={r.state} /> : undefined} />
      <Spacer size="md" />
      {exp.error && <Alert componentId="run.exp.error" type="error" message="Couldn't load this run" description={exp.error} closable={false} />}
      {!exp.data && !exp.error && <TableSkeleton lines={8} />}
      {exp.data && !r && <Empty title="No such run" description={`${exp.data.name} has no run ${run}.`} />}
      {r && (
        <div css={{ display: "grid", gridTemplateColumns: "340px minmax(0, 1fr)", gap: theme.spacing.lg, alignItems: "start" }}>
          <div>
            <Section title="About this run">
              <Field name="Created at">{when(r.created, true)} {TIME_ZONE_LABEL}</Field>
              <Field name="Last updated">{when(r.updated, true)} {TIME_ZONE_LABEL}</Field>
              <Field name="Experiment"><Link to={`/experiments/${id}`}>{exp.data?.name}</Link></Field>
              <Field name="Status">{r.state ? <Status state={r.state} /> : "-"}</Field>
              <Field name="Run ID">{r.id}</Field>
              {r.parent && <Field name="Parent run">{parent ? <Link to={`/experiments/${id}/runs/${parent.id}`}>{parent.name}</Link>
                : <Typography.Text title="Not logged yet: a parent is logged when it ends">{r.parent}</Typography.Text>}</Field>}
              {children.length > 0 && <Field name="Child runs">{children.length}</Field>}
              <Field name="Type">{r.type}</Field>
            </Section>
            <Section title="Registered models">
              {model.data === null || (model.data && versions.length === 0) ? <Typography.Text color="secondary">No model version came from this run</Typography.Text>
                : !model.data ? <ParagraphSkeleton label="Loading model versions" />
                : versions.map((v) => (
                  <div key={v.version} css={{ display: "flex", alignItems: "center", gap: theme.spacing.xs, flexWrap: "wrap", padding: `${theme.spacing.xs}px 0` }}>
                    <Typography.Text><Link to={`/models/${model.data!.id}/versions/${v.version}`}>{model.data!.name} · Version {v.version}</Link></Typography.Text>
                    {v.aliases.map((a) => <Tag key={a} componentId="run.model.alias" color="indigo">@{a}</Tag>)}
                  </div>))}
            </Section>
          </div>
          <div css={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: theme.spacing.lg, minWidth: 0 }}>
            <KeyValues title="Metrics" values={r.metrics} componentId="run.metrics" />
            <KeyValues title="Parameters" values={r.params} componentId="run.params" />
          </div>
        </div>
      )}
      {r && children.length > 0 && <><Spacer size="lg" /><ChildRuns parent={r} runs={children} expId={id} /></>}
    </>
  );
}
