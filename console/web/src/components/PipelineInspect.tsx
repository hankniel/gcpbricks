import { useState } from "react";
import {
  Alert, Spacer, TableCell, TableRow, TableSkeleton, Tabs, Tag, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "./Table";
import { api, type GraphNode, type PipelineStep, type StepArtifact, type StepComponent } from "../api";
import { Field } from "./Panel";
import { Status } from "./Status";
import TaskGraph from "./TaskGraph";
import { UnitCharts } from "./TaskMetrics";
import { clock, duration, fmtValue, useLoad, when } from "../util";

export function asNode(s: PipelineStep, deps: string[]): GraphNode {
  // A dsl.If / Else pair is a group with no condition of its own: its two branches carry them.
  const name = s.kind === "condition" ? (s.condition ? `If ${s.condition}` : "If / Else") : s.label;
  return { key: s.key, label: name, type: s.kind === "condition" ? "condition" : "cloud_run",
           kind: s.kind === "condition" ? "condition_task" : "pipeline_step", inner_kind: null, level: s.level, run_if: "",
           for_each: false, max_retries: 0, detail: s.kind === "condition" ? "Condition" : s.key, depends_on: deps,
           parent: s.parent };
}

function Pre({ children, max = 360 }: { children: React.ReactNode; max?: number }) {
  const { theme } = useDesignSystemTheme();
  return <pre css={{ maxHeight: max, overflow: "auto", margin: 0, background: theme.colors.backgroundSecondary, padding: theme.spacing.sm,
                     fontSize: 12, borderRadius: theme.borders.borderRadiusMd, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{children}</pre>;
}

const show = (v: unknown) => v === undefined || v === null || v === "" ? "-" : fmtValue(v);

/** A step's component as compiled: what feeds each input, what it outputs, what it runs. */
export function ComponentView({ c, values }: { c: StepComponent; values?: Record<string, unknown> }) {
  const r = c.runs;
  return (
    <>
      {c.description && <><Typography.Text color="secondary">{c.description}</Typography.Text><Spacer size="sm" /></>}
      <Field name="Component">{c.component}</Field>
      {c.condition && <Field name="Runs when">{c.condition}</Field>}
      <Field name="Cache">{c.cache ? "On: an unchanged step reuses its last result" : "Off"}</Field>
      {r.image && <Field name="Image">{r.image}</Field>}
      {r.function && <Field name="Function">{r.function}</Field>}
      {r.resources && <Field name="Machine">{[r.resources.cpu && `${r.resources.cpu} CPU`, r.resources.memory,
        r.resources.accelerator && `${r.resources.accelerators ?? 1}× ${r.resources.accelerator}`].filter(Boolean).join(" · ") || "default"}</Field>}
      {r.artifact_uri && <Field name="Imports">{r.type} from {r.artifact_uri}</Field>}
      <Spacer size="sm" />
      <Typography.Title level={5}>Inputs</Typography.Title>
      <Table scrollable>
        <TableRow isHeader>
          <TableHeader componentId="pipe.c.in.name">Name</TableHeader>
          <TableHeader componentId="pipe.c.in.type">Type</TableHeader>
          <TableHeader componentId="pipe.c.in.from">From</TableHeader>
          {values && <TableHeader componentId="pipe.c.in.value">Value in this run</TableHeader>}
        </TableRow>
        {c.inputs.map((i) => (
          <TableRow key={i.name}>
            <TableCell>{i.name}{i.kind === "artifact" && <> <Tag componentId="pipe.c.in.art">artifact</Tag></>}</TableCell>
            <TableCell>{i.type}</TableCell>
            <TableCell>{i.from || (i.default !== undefined ? `default ${show(i.default)}` : "-")}</TableCell>
            {values && <TableCell><Typography.Text code css={{ overflowWrap: "anywhere" }}>{show(values[i.name])}</Typography.Text></TableCell>}
          </TableRow>))}
      </Table>
      <Spacer size="sm" />
      <Typography.Title level={5}>Outputs</Typography.Title>
      <Typography.Text>{c.outputs.map((o) => `${o.name} (${o.type})`).join(", ") || "-"}</Typography.Text>
      {r.source && <><Spacer size="sm" /><Typography.Title level={5}>Source</Typography.Title><Pre>{r.source}</Pre></>}
      {r.command && !r.source && r.command.length > 0 && <><Spacer size="sm" /><Typography.Title level={5}>Command</Typography.Title>
        <Pre>{[...r.command, ...(r.args ?? [])].join(" ")}</Pre></>}
    </>
  );
}

/** A pipeline as deploy last published it: its steps, its parameters, and each step's component. */
export function PipelineDefinition({ wf, task }: { wf: string; task: string }) {
  const { theme } = useDesignSystemTheme();
  const { data, error } = useLoad(() => api.pipelineDefinition(wf, task), [wf, task]);
  const [picked, setPicked] = useState<string | null>(null);
  if (error) return <Alert componentId="pipe.def.error" type="warning" message="Couldn't read the published pipeline" description={error} closable={false} />;
  if (!data) return <TableSkeleton lines={4} />;
  const nodes = data.nodes.map((s) => asNode(s, data.edges.filter((e) => e.to === s.key).map((e) => e.from)));
  const step = picked ? data.steps[picked] : null;
  return (
    <>
      <Typography.Text color="secondary">{data.name} · version {data.version?.slice(0, 12)} · {data.sdk}</Typography.Text>
      <Spacer size="sm" />
      <Typography.Title level={5}>Parameters</Typography.Title>
      <Table scrollable>
        <TableRow isHeader>
          <TableHeader componentId="pipe.def.p.name">Name</TableHeader>
          <TableHeader componentId="pipe.def.p.type">Type</TableHeader>
          <TableHeader componentId="pipe.def.p.value">This task passes</TableHeader>
          <TableHeader componentId="pipe.def.p.default">Default</TableHeader>
        </TableRow>
        {data.parameters.map((p) => (
          <TableRow key={p.name}>
            <TableCell>{p.name}</TableCell><TableCell>{p.type}</TableCell>
            <TableCell>{show(p.value)}</TableCell><TableCell>{show(p.default)}</TableCell>
          </TableRow>))}
      </Table>
      <Spacer size="sm" />
      <TaskGraph nodes={nodes} edges={data.edges.map((e) => ({ ...e, outcome: "" }))} selected={picked}
                 onPick={(k) => setPicked(picked === k ? null : k)} height={560} vertical />
      {step ? (
        <div css={{ marginTop: theme.spacing.sm, padding: theme.spacing.md, border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd }}>
          <Typography.Title level={4} css={{ "&&": { marginTop: 0 } }}>{step.label}</Typography.Title>
          <ComponentView c={step} />
        </div>
      ) : <Typography.Text color="secondary">Pick a step to see its component.</Typography.Text>}
    </>
  );
}

function ArtifactView({ a, open }: { a: StepArtifact; open?: () => void }) {
  const metrics = a.type === "system.Metrics" ? Object.entries(a.metadata).filter(([k]) => k !== "display_name") : [];
  return (
    <div css={{ marginBottom: 12 }}>
      <Typography.Text bold>{a.name}</Typography.Text> <Tag componentId="pipe.art.type">{a.type}</Tag>
      {open && <Typography.Link componentId="pipe.art.open" onClick={open}>View</Typography.Link>}
      <div><Typography.Text color="secondary" size="sm" css={{ overflowWrap: "anywhere" }}>{a.uri}</Typography.Text></div>
      {metrics.length > 0 && (
        <Table scrollable>
          {metrics.map(([k, v]) => <TableRow key={k}><TableCell>{k}</TableCell><TableCell>{show(v)}</TableCell></TableRow>)}
        </Table>)}
    </div>
  );
}

/** One step of a pipeline run: what it got and gave, its component, its logs and its compute. */
export function StepDetail({ wf, id, task, step }: { wf: string; id: string; task: string; step: string }) {
  const { theme } = useDesignSystemTheme();
  const { data, error } = useLoad(() => api.pipelineStep(wf, id, task, step), [wf, id, task, step]);
  const [text, setText] = useState<{ name: string; type: string; text: string } | null>(null);
  const [errorsOnly, setErrorsOnly] = useState(false);
  if (error) return <Alert componentId="pipe.step.load" type="error" message="Couldn't read the step" description={error} closable={false} />;
  if (!data) return <TableSkeleton lines={4} />;
  const open = (a: StepArtifact) => api.pipelineArtifact(wf, id, task, step, a.name).then(setText, (e: Error) => setText({ name: a.name, type: "error", text: e.message }));
  const logs = data.logs.filter((l) => !errorsOnly || l.severity === "ERROR" || l.severity === "CRITICAL" || /error|traceback/i.test(l.text));
  return (
    <Tabs.Root componentId="pipe.step.tabs" defaultValue="overview">
      <Tabs.List>
        <Tabs.Trigger value="overview">Overview</Tabs.Trigger>
        <Tabs.Trigger value="inputs">Inputs</Tabs.Trigger>
        <Tabs.Trigger value="outputs">Outputs</Tabs.Trigger>
        <Tabs.Trigger value="component">Component</Tabs.Trigger>
        <Tabs.Trigger value="logs">Logs ({data.logs.length})</Tabs.Trigger>
        <Tabs.Trigger value="compute">Compute</Tabs.Trigger>
      </Tabs.List>
      <Tabs.Content value="overview">
        <Spacer size="sm" />
        <Field name="Status"><Status state={data.state} />{data.cached && <> <Tag componentId="pipe.step.cached" color="lime">from cache</Tag></>}</Field>
        <Field name="Started">{data.start ? when(data.start, true) : "-"}</Field>
        <Field name="Duration">{data.start ? duration(data.start, data.end) : "-"}</Field>
        {data.compute && <Field name="Machine">{data.compute.machine.machine_type ?? "-"}</Field>}
        {data.custom_job && <Field name="Custom job"><a href={data.compute?.link} target="_blank" rel="noreferrer">{data.custom_job.split("/").pop()}</a></Field>}
        {data.error && <Alert componentId="pipe.step.error" type="error" message="Error" description={data.error} closable={false} />}
      </Tabs.Content>
      <Tabs.Content value="inputs">
        <Spacer size="sm" />
        {data.component ? <ComponentView c={data.component} values={data.inputs} /> : (
          <Table scrollable>{Object.entries(data.inputs).map(([k, v]) => <TableRow key={k}><TableCell>{k}</TableCell><TableCell>{show(v)}</TableCell></TableRow>)}</Table>)}
        {data.input_artifacts.length > 0 && <><Spacer size="sm" /><Typography.Title level={5}>Input artifacts</Typography.Title>
          {data.input_artifacts.map((a) => <ArtifactView key={a.resource} a={a} />)}</>}
      </Tabs.Content>
      <Tabs.Content value="outputs">
        <Spacer size="sm" />
        {Object.keys(data.outputs).length > 0 && (
          <Table scrollable>
            <TableRow isHeader><TableHeader componentId="pipe.out.name">Output</TableHeader><TableHeader componentId="pipe.out.value">Value</TableHeader></TableRow>
            {Object.entries(data.outputs).map(([k, v]) => (
              <TableRow key={k}><TableCell>{k}</TableCell>
                <TableCell><Typography.Text code css={{ overflowWrap: "anywhere" }}>{show(v)}</Typography.Text></TableCell></TableRow>))}
          </Table>)}
        <Spacer size="sm" />
        {data.output_artifacts.map((a) => <ArtifactView key={a.resource} a={a} open={a.text ? () => open(a) : undefined} />)}
        {!Object.keys(data.outputs).length && !data.output_artifacts.length && <Typography.Text color="secondary">No outputs recorded.</Typography.Text>}
        {text && <>
          <Spacer size="sm" />
          <Typography.Text bold>{text.name}</Typography.Text> <Typography.Link componentId="pipe.art.close" onClick={() => setText(null)}>Close</Typography.Link>
          {text.type === "system.HTML"
            ? <iframe title={text.name} sandbox="" srcDoc={text.text} css={{ width: "100%", height: 600, border: `1px solid ${theme.colors.border}` }} />
            : <Pre max={600}>{text.text}</Pre>}
        </>}
      </Tabs.Content>
      <Tabs.Content value="component">
        <Spacer size="sm" />
        {data.component ? <ComponentView c={data.component} /> : <Typography.Text color="secondary">The run carries no compiled spec.</Typography.Text>}
      </Tabs.Content>
      <Tabs.Content value="logs">
        <Spacer size="sm" />
        <Typography.Link componentId="pipe.logs.errors" onClick={() => setErrorsOnly(!errorsOnly)}>{errorsOnly ? "Show all lines" : "Errors only"}</Typography.Link>
        <Spacer size="xs" />
        {data.logs.length === 0 ? <Typography.Text color="secondary">No logs: the step ran no custom job, or its logs are past retention.</Typography.Text> : (
          <Pre max={520}>{logs.map((l) => `${clock(l.time)} ${l.source === "service" ? "[vertex] " : ""}${l.text}`).join("\n")}</Pre>)}
      </Tabs.Content>
      <Tabs.Content value="compute">
        <Spacer size="sm" />
        {data.compute ? <>
          <Field name="Machine">{data.compute.machine.machine_type}{(data.compute.machine.replicas ?? 1) > 1 ? ` × ${data.compute.machine.replicas}` : ""}</Field>
          <Field name="CPU avg / peak">{[data.compute.usage.cpu_mean, data.compute.usage.cpu_peak].map((v) => v == null ? "-" : `${Math.round(v * 100)}%`).join(" / ")}</Field>
          <Field name="Memory peak">{data.compute.usage.memory_peak == null ? "-" : `${Math.round(data.compute.usage.memory_peak * 100)}%`}</Field>
          <Field name="Machine-hours">{(data.compute.usage.machine_hours ?? 0).toFixed(3)}</Field>
          <Spacer size="sm" />
          <UnitCharts u={data.compute} />
        </> : <Typography.Text color="secondary">This step ran no custom job (an importer, a condition).</Typography.Text>}
      </Tabs.Content>
    </Tabs.Root>
  );
}
