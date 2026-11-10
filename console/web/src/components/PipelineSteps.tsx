import { useState } from "react";
import { Alert, Spacer, TableSkeleton, Typography, useDesignSystemTheme } from "@databricks/design-system";
import { api } from "../api";
import { asNode, PipelineDefinition, StepDetail } from "./PipelineInspect";
import { Field } from "./Panel";
import { Status } from "./Status";
import TaskGraph from "./TaskGraph";
import { duration, useLoad } from "../util";

const live = (s?: string) => s === "RUNNING" || s === "QUEUED";

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
  if (error) return <Alert componentId="pipe.error" type="error" message="Couldn't read the pipeline run" description={error} closable={false} />;
  if (!data) return <TableSkeleton lines={4} />;
  if (!data.run) return (
    <>
      <Typography.Text color="secondary">No pipeline run yet: it starts once the runner publishes it, or the runner skipped it because one was already running. The pipeline it runs:</Typography.Text>
      <Spacer size="sm" />
      <PipelineDefinition wf={wf} task={task} />
    </>);
  const steps = data.nodes ?? [], edges = data.edges ?? [];
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
      <TaskGraph nodes={nodes} edges={edges.map((e) => ({ ...e, outcome: "" }))} status={status} timing={timing}
                 selected={picked} onPick={(k) => setPicked(picked === k ? null : k)} height={620} vertical />
      {step && (
        <div css={{ marginTop: theme.spacing.sm, padding: theme.spacing.md, border: `1px solid ${theme.colors.border}`,
                    borderRadius: theme.borders.borderRadiusMd }}>
          <Typography.Text bold>{step.kind === "condition" ? `If ${step.condition}` : step.label}</Typography.Text>
          <Spacer size="xs" />
          {step.kind === "condition" && <>
            <Field name="Status"><Status state={step.state} /></Field>
            <Field name="Duration">{step.start ? duration(step.start, step.end) : "-"}</Field>
          </>}
          {step.kind === "condition" && <Field name="Runs its steps when">{step.condition}</Field>}
          {step.kind === "condition" && step.error && <Alert componentId="pipe.step.error" type="error" message="Error" description={step.error} closable={false} />}
          {step.kind !== "condition" && <StepDetail wf={wf} id={id} task={task} step={step.key} />}
        </div>
      )}
    </>
  );
}
