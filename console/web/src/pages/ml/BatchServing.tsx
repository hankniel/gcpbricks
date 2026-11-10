import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Alert, Empty, Input, SearchIcon, Spacer, TableCell, TableRow, TableSkeleton, Tag, Typography,
  WorkflowsIcon,
} from "@databricks/design-system";
import { Table, TableHeader } from "../../components/Table";
import { api, ServedModel } from "../../api";
import { Field, Section } from "../../components/Panel";
import PageTitle from "../../components/PageTitle";
import SidePanel from "../../components/SidePanel";
import RunsTable from "../../components/RunsTable";
import TableLink from "../../components/TableLink";
import { Status } from "../../components/Status";
import { runState, useLoad } from "../../util";

/** What scores a model: its Cloud Run job, or a Vertex AI batch prediction. */
function runner(m: ServedModel) {
  return m.vertex ? `Vertex AI batch prediction: ${m.vertex.registered_model}` : m.job;
}

/** Every bundle job's batch_serve_task: what it scores and how its last run went. */
export function BatchServingPage() {
  const { data, error } = useLoad(api.batchTasks, []);
  const [q, setQ] = useState("");
  const ql = q.toLowerCase();
  const rows = (data ?? []).filter((t) => [t.job, t.task, t.bundle, ...t.models.map((m) => m.model)]
    .some((s) => s.toLowerCase().includes(ql)));
  return (
    <>
      <PageTitle title="Batch serving" />
      <Spacer size="md" />
      <div css={{ width: 420 }}>
        <Input componentId="batch.filter" prefix={<SearchIcon />} placeholder="Filter by job or model" value={q}
               onChange={(e) => setQ(e.target.value)} allowClear />
      </div>
      <Spacer size="md" />
      {error && <Alert componentId="batch.error" type="error" message="Couldn't load batch serving" description={error} closable={false} />}
      {!data && !error && <TableSkeleton lines={4} />}
      {data && (
        <Table scrollable empty={rows.length ? undefined : <Empty title="No batch serving yet"
          description={data.length ? "No task matches the filter." : "Batch serving appears here once a bundle job with a batch_serve_task is deployed."} />}>
          <TableRow isHeader>
            <TableHeader componentId="batch.h.task">Task</TableHeader>
            <TableHeader componentId="batch.h.models">Models</TableHeader>
            <TableHeader componentId="batch.h.window">Window</TableHeader>
            <TableHeader componentId="batch.h.target">Target</TableHeader>
            <TableHeader componentId="batch.h.last">Last run</TableHeader>
          </TableRow>
          {rows.map((t) => (
            <TableRow key={`${t.workflow}/${t.task}`}>
              <TableCell><Link to={`/batch-serving/${t.workflow}/${encodeURIComponent(t.task)}`}>{t.workflow} / {t.task}</Link></TableCell>
              <TableCell><Typography.Text ellipsis>{t.models.map((m) => m.model).join(", ")}</Typography.Text></TableCell>
              <TableCell>{t.window_days} days</TableCell>
              <TableCell><Tag componentId="batch.target">{t.target}</Tag></TableCell>
              <TableCell>{t.last_run ? <Link to={`/jobs/${t.workflow}/runs/${t.last_run.id}`}>
                <Status state={runState(t.last_run)} /></Link> : <Typography.Text color="secondary">Never run</Typography.Text>}</TableCell>
            </TableRow>
          ))}
        </Table>
      )}
    </>
  );
}

/** One batch_serve_task: its models and its job's runs, settings beside. */
export function BatchServeTaskPage() {
  const { wf = "", task = "" } = useParams();
  const { data: t, error } = useLoad(() => api.batchTask(wf, task), [wf, task]);
  const me = useLoad(api.me, []);
  const models = useLoad(api.models, []);
  const modelId = (name: string) => models.data?.find((m) => m.name === name)?.id;
  return (
    <>
      <PageTitle crumbs={[{ to: "/batch-serving", label: "Batch serving" }]} icon={<WorkflowsIcon />}
        title={t ? `${t.workflow} / ${t.task}` : task} addOns={t && <Tag componentId="batch.task.target">{t.target}</Tag>} />
      <Spacer size="md" />
      {error && <Alert componentId="batch.task.error" type="error" message="Couldn't load this task" description={error} closable={false} />}
      {!t && !error && <TableSkeleton lines={8} />}
      {t && (
        <SidePanel>
          <div css={{ minWidth: 0 }}>
            <Typography.Title level={4}>Models</Typography.Title>
            <Table scrollable>
              <TableRow isHeader>
                <TableHeader componentId="batch.m.h.model">Model</TableHeader>
                <TableHeader componentId="batch.m.h.runner">Runs on</TableHeader>
                <TableHeader componentId="batch.m.h.window">Window</TableHeader>
                <TableHeader componentId="batch.m.h.out">Predictions</TableHeader>
              </TableRow>
              {t.models.map((m) => {
                const id = modelId(m.model);
                return (
                  <TableRow key={m.model}>
                    <TableCell>{id ? <Link to={`/models/${id}`}>{m.model}</Link> : m.model}</TableCell>
                    <TableCell>{runner(m)}</TableCell>
                    <TableCell>{m.window_days ?? t.window_days} days</TableCell>
                    <TableCell>{m.vertex ? <TableLink name={m.vertex.output_table} project={me.data?.project} />
                      : <Typography.Text color="secondary">Written by its job</Typography.Text>}</TableCell>
                  </TableRow>
                );
              })}
            </Table>
            <Spacer size="lg" />
            <Typography.Title level={4}>Runs</Typography.Title>
            <Typography.Text color="secondary" size="sm">The job's latest runs; a failed run's error names the models that failed.</Typography.Text>
            <Spacer size="sm" />
            {t.runs.length === 0 ? <Typography.Text color="secondary">No runs yet.</Typography.Text>
              : <RunsTable runs={t.runs.map((r) => ({ ...r, workflow: t.workflow }))} />}
          </div>
          <div>
            <Section title="Details">
              <Field name="Job"><Link to={`/jobs/${t.workflow}`}>{t.workflow}</Link></Field>
              <Field name="Bundle">{t.bundle}</Field>
            </Section>
            <Section title="Settings">
              <Field name="Window">{t.settings.window_days} days</Field>
              <Field name="Limit">{t.settings.max_hours} hours per model</Field>
              <Field name="Day boundary">UTC{Number(t.settings.utc_offset_hours) >= 0 ? "+" : ""}{t.settings.utc_offset_hours}</Field>
            </Section>
          </div>
        </SidePanel>
      )}
    </>
  );
}
