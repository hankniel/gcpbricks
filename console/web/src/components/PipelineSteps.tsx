import { useState } from "react";
import { Alert, Spacer, TableCell, TableRow, TableSkeleton, Typography, useDesignSystemTheme } from "@databricks/design-system";
import { api, type PipelineStep } from "../api";
import { asNode, PipelineDefinition, StepDetail } from "./PipelineInspect";
import { Field } from "./Panel";
import { Status } from "./Status";
import { Table, TableHeader } from "./Table";
import TaskGraph from "./TaskGraph";
import { duration, useLoad } from "../util";

const live = (s?: string) => s === "RUNNING" || s === "QUEUED";

/** A dsl.ParallelFor's iterations, as Databricks' iterations table: each one's state and time, and
 *  its steps (`step[i]`), one of which opens below. */
function Iterations({ loop, inner, picked, onPick }: { loop: PipelineStep; inner: string[]; picked: string | null;
                                                      onPick: (ref: string) => void }) {
  const its = loop.iterations ?? [];
  if (!its.length) return <Typography.Text color="secondary">No iteration has started yet.</Typography.Text>;
  return (
    <Table scrollable>
      <TableRow isHeader>
        <TableHeader componentId="pipe.it.index">Iteration</TableHeader>
        <TableHeader componentId="pipe.it.state">Status</TableHeader>
        <TableHeader componentId="pipe.it.time">Duration</TableHeader>
        <TableHeader componentId="pipe.it.steps">Steps</TableHeader>
      </TableRow>
      {its.map((i) => (
        <TableRow key={i.index}>
          <TableCell>{i.index}</TableCell>
          <TableCell><Status state={i.state} /></TableCell>
          <TableCell>{i.start ? duration(i.start, i.end ?? null) : "-"}</TableCell>
          <TableCell>{inner.map((k) => {
            const ref = `${k}[${i.index}]`;
            return <Typography.Link key={ref} componentId="pipe.it.step" onClick={() => onPick(ref)}
                                    css={{ marginRight: 8, fontWeight: picked === ref ? 600 : undefined }}>{ref}</Typography.Link>;
          })}</TableCell>
        </TableRow>))}
    </Table>
  );
}

/** The steps of the Vertex pipeline run a vertex_pipeline_task's run started, as GCP's
 *  pipeline view shows them; a step opens its details. */
export default function PipelineSteps({ wf, id, task }: { wf: string; id: string; task: string }) {
  const { theme } = useDesignSystemTheme();
  const [running, setRunning] = useState(false);
  const { data, error } = useLoad(async () => {
    const r = await api.taskPipeline(wf, id, task);
    setRunning(live(r.run?.state));
    return r;
  }, [wf, id, task], running ? 15000 : 0);
  const [picked, setPicked] = useState<string | null>(null);
  const [iteration, setIteration] = useState<string | null>(null);
  if (error) return <Alert componentId="pipe.error" type="error" message="Couldn't read the pipeline run" description={error} closable={false} />;
  if (!data) return <TableSkeleton lines={4} />;
  if (!data.run) return (
    <>
      <Typography.Text color="secondary">No pipeline run yet: it starts once the runner publishes it, or the runner skipped it because one was already running. The pipeline it runs:</Typography.Text>
      <Spacer size="sm" />
      <PipelineDefinition wf={wf} task={task} />
    </>);
  const steps = data.flow?.nodes ?? [], edges = data.flow?.edges ?? [];
  const nodes = steps.map((s) => asNode(s, edges.filter((e) => e.to === s.key).map((e) => e.from)));
  const status = Object.fromEntries(steps.map((s) => [s.key, s.state]));
  const timing = Object.fromEntries(steps.map((s) => [s.key, { start: s.start, end: s.end }]));
  const step = steps.find((s) => s.key === picked);
  return (
    <>
      <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.md, flexWrap: "wrap" }}>
        <Status state={data.run.state} />
        <Typography.Text color="secondary">{data.run.id}{data.run.start ? ` · ${duration(data.run.start, data.run.end)}` : ""}</Typography.Text>
        <Typography.Link componentId="pipe.gcp" href={data.run.link} openInNewTab>Open in Vertex AI</Typography.Link>
      </div>
      {data.run.error && <><Spacer size="sm" /><Alert componentId="pipe.run.error" type="error" message="The pipeline failed" description={data.run.error} closable={false} /></>}
      <Spacer size="sm" />
      <TaskGraph nodes={nodes} edges={edges} status={status} timing={timing}
                 selected={picked} onPick={(k) => { setPicked(picked === k ? null : k); setIteration(null); }} height={620} layered />
      {step && (
        <div css={{ marginTop: theme.spacing.sm, padding: theme.spacing.md, border: `1px solid ${theme.colors.border}`,
                    borderRadius: theme.borders.borderRadiusMd }}>
          <Typography.Text bold>{step.kind === "decision" ? `${step.label} ${step.condition}` : step.label}</Typography.Text>
          <Spacer size="xs" />
          {step.kind === "decision" && <>
            <Field name="Test">{step.condition}</Field>
            <Field name="Took">{step.taken === "true" ? "True" : step.taken === "false" ? "False" : "Not evaluated"}</Field>
            {step.error && <Alert componentId="pipe.step.error" type="error" message="Error" description={step.error} closable={false} />}
          </>}
          {step.kind === "for_each" && <>
            {step.error && <Alert componentId="pipe.loop.error" type="error" message="Error" description={step.error} closable={false} />}
            <Iterations loop={step} inner={(data.nodes ?? []).filter((n) => n.parent === step.key).map((n) => n.key)}
                        picked={iteration} onPick={setIteration} />
            {iteration && <><Spacer size="sm" /><Typography.Text bold>{iteration}</Typography.Text>
              <StepDetail wf={wf} id={id} task={task} step={iteration} /></>}
          </>}
          {step.kind !== "decision" && step.kind !== "for_each" && <StepDetail wf={wf} id={id} task={task} step={step.key} />}
        </div>
      )}
    </>
  );
}
