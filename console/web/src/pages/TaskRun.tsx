import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  Alert, Button, CopyIcon, DownloadIcon, Empty, Input, SearchIcon, Spacer, TableCell, TableRow,
  TableSkeleton, Tag, ToggleButton, Tooltip, Typography, WorkflowsIcon, useDesignSystemTheme,
} from "@databricks/design-system";
import PageTitle from "../components/PageTitle";
import SidePanel from "../components/SidePanel";
import { Table, TableHeader } from "../components/Table";
import { api, Attempt, LogLine } from "../api";
import { Field, ParamList, Section } from "../components/Panel";
import RepairModal from "../components/RepairModal";
import WhyFailed from "../components/WhyFailed";
import { Status } from "../components/Status";
import TaskSettings from "../components/TaskSettings";
import PipelineSteps from "../components/PipelineSteps";
import NotebookRun from "../components/NotebookRun";
import TaskMetrics from "../components/TaskMetrics";
import { clock, duration, KIND, live, memoryText, runState, TIME_ZONE_LABEL, useLoad, useTick, when } from "../util";

const OPS: Record<string, string> = { EQUAL_TO: "==", NOT_EQUAL: "!=", GREATER_THAN: ">", GREATER_THAN_OR_EQUAL: ">=",
  LESS_THAN: "<", LESS_THAN_OR_EQUAL: "<=" };

/** Databricks' task run page: the task's output and what it ran, with the run's details beside. */
/** Tasks whose compute and BigQuery use the Metrics section shows. */
const METRIC_KINDS = new Set(["dbt_task", "spark_python_task", "python_wheel_task", "vertex_pipeline_task", "batch_serve_task"]);

export default function TaskRunPage() {
  const { wf = "", id = "", task = "" } = useParams();
  const { theme } = useDesignSystemTheme();
  // While the task or its run moves, poll every few seconds (logs a little slower) and tick.
  const [running, setRunning] = useState(false);
  const t = useLoad(() => api.taskRun(wf, id, task), [wf, id, task], running ? 5000 : 0);
  const logs = useLoad(() => api.logs(wf, id, task), [wf, id, task], running ? 10000 : 0);
  const d = t.data;
  useEffect(() => setRunning(live(d?.status) || live(d?.run.state)), [d]);
  useTick(running);
  const [repairing, setRepairing] = useState(false);
  const nav = useNavigate();
  // This task's run page, at the attempt the task ran in.
  const runLink = d ? `/jobs/${wf}/runs/${d.run.run_id}?repair_id=${d.run.repair_id}` : `/jobs/${wf}/runs/${id}`;
  const attemptName = (n: number) => (n === 0 ? "Original run" : `Repair ${n}`);
  const [q, setQ] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [wrap, setWrap] = useState(true);
  const [only, setOnly] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ text: string; error?: boolean } | null>(null);
  const lines = useMemo(() => (logs.data ?? []).filter((l) =>
    (!only || l.execution === only) && (!errorsOnly || l.severity === "ERROR" || l.severity === "CRITICAL")
    && (!q || l.text.toLowerCase().includes(q.toLowerCase()))), [logs.data, q, errorsOnly, only]);
  const text = (ls: LogLine[]) => ls.map((l) => `${clock(l.time)}  ${l.task ? `[${l.task}] ` : ""}${l.input ? `(${l.input}) ` : ""}${l.text}`).join("\n");
  const download = () => {
    const url = URL.createObjectURL(new Blob([text(lines)], { type: "text/plain" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `${task}-${id.slice(0, 8)}.log` });
    a.click(); URL.revokeObjectURL(url);
  };
  const kind = d ? d.node.kind : "";
  const attempts = d?.attempts ?? [];
  const select = (a: Attempt) => setOnly(only === a.id ? null : a.id);
  return (
    <>
      <PageTitle crumbs={[{ to: "/", label: "Jobs & Pipelines" }, { to: `/jobs/${wf}`, label: d?.job_name ?? wf },
                          { to: runLink, label: d ? `Run ${when(d.run.start, true)}${d.run.repair_id ? ` · Repair ${d.run.repair_id}` : ""}` : "Run" }]}
        icon={<WorkflowsIcon />} title={`${task} run`} addOns={d && <Status state={d.status ?? "EXCLUDED"} />}
        actions={d && (d.run.state === "FAILED" || d.run.state === "CANCELLED" || d.run.with_failures) && d.run.repair_id === d.run.latest_repair_id
          && <Button componentId="task.repair" type="primary" onClick={() => setRepairing(true)}>Repair task</Button>} />
      {t.error && <><Spacer size="md" /><Alert componentId="task.error" type="error" message="Couldn't load this task" description={t.error} closable={false} /></>}
      {msg && <><Spacer size="sm" /><Alert componentId="task.msg" type={msg.error ? "error" : "info"} message={msg.text} onClose={() => setMsg(null)} /></>}
      {!d && !t.error && <><Spacer size="md" /><TableSkeleton lines={8} /></>}
      {d && (
        // As Databricks' task run: the details panel from right under the title, the task's work beside it.
        <SidePanel>
          <div css={{ minWidth: 0, paddingTop: theme.spacing.md }}>
            {d.status === "FAILED" ? <WhyFailed wf={wf} id={id} task={task} start={d.run.start} />
              : d.error && <><Alert componentId="task.failure" type="error" message="Error" description={d.error} closable={false} /><Spacer size="md" /></>}
            {d.excluded && <><Alert componentId="task.excluded" type="warning" closable={false}
              message={`Succeeded, but its pipeline skipped ${d.excluded.join(", ")}`}
              description="A condition in the pipeline didn't hold, as when a gate rejects a model. Its steps below say which." />
              <Spacer size="md" /></>}

            {d.spec.type === "condition" && (
              <>
                <Typography.Title level={4}>Condition</Typography.Title>
                <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm, flexWrap: "wrap" }}>
                  <Tag componentId="task.cond.left">{d.spec.left}</Tag>
                  <Typography.Text bold>{OPS[d.spec.op ?? ""] ?? d.spec.op}</Typography.Text>
                  <Tag componentId="task.cond.right">{d.spec.right}</Tag>
                  {d.outcome && <><Typography.Text color="secondary">evaluated to</Typography.Text>
                    <Tag componentId="task.cond.outcome" color={d.outcome === "true" ? "lime" : "charcoal"}>{d.outcome === "true" ? "True" : "False"}</Tag></>}
                </div>
                <Spacer size="sm" />
                <Typography.Text color="secondary">An If/else condition starts no compute, so it has no output: its outcome decides which downstream tasks run.</Typography.Text>
                <Spacer size="lg" />
              </>
            )}

            {d.spec.type === "for_each" && (
              <>
                <Typography.Title level={4}>Iterations</Typography.Title>
                {attempts.length === 0 ? <Typography.Text color="secondary">No iterations started.</Typography.Text> : (
                  <Table scrollable>
                    <TableRow isHeader>
                      <TableHeader componentId="task.it.h.input">Input</TableHeader>
                      <TableHeader componentId="task.it.h.status">Status</TableHeader>
                      <TableHeader componentId="task.it.h.start">Start time</TableHeader>
                      <TableHeader componentId="task.it.h.duration" align="right">Duration</TableHeader>
                      <TableHeader componentId="task.it.h.memory">Peak memory</TableHeader>
                      <TableHeader componentId="task.it.h.output">Output</TableHeader>
                    </TableRow>
                    {attempts.map((a) => (
                      <TableRow key={a.name}>
                        <TableCell><Typography.Text code>{a.input}</Typography.Text></TableCell>
                        <TableCell><Status state={a.state} /></TableCell>
                        <TableCell>{when(a.start, true)}</TableCell>
                        <TableCell css={{ justifyContent: "flex-end" }}>{duration(a.start, a.end)}</TableCell>
                        <TableCell>{memoryText(a.memory)}</TableCell>
                        <TableCell><Typography.Link componentId="task.it.show" onClick={() => select(a)}>
                          {only === a.id ? "Show all output" : "Show its output"}</Typography.Link></TableCell>
                      </TableRow>
                    ))}
                  </Table>
                )}
                <Spacer size="lg" />
              </>
            )}

            {kind === "notebook_task" && (
              <>
                <Typography.Title level={4}>Notebook</Typography.Title>
                <NotebookRun wf={wf} id={id} task={task} />
                <Spacer size="lg" />
              </>
            )}

            {kind === "vertex_pipeline_task" && (
              <>
                <Typography.Title level={4}>Pipeline</Typography.Title>
                <PipelineSteps wf={wf} id={id} task={task} />
                <Spacer size="lg" />
              </>
            )}

            {d.spec.type === "workflow" && (
              <>
                <Typography.Title level={4}>{kind === "run_job_task" ? "Job run" : KIND[kind] ?? "Child run"}</Typography.Title>
                {attempts.length === 0 ? (
                  <Typography.Text color="secondary">
                    {d.status ? "This run started its child before the platform recorded child runs, so it can't be linked." : "Not started."}
                  </Typography.Text>
                ) : (
                  <Table scrollable>
                    <TableRow isHeader>
                      <TableHeader componentId="task.child.h.job">{kind === "run_job_task" ? "Job" : "Runner"}</TableHeader>
                      <TableHeader componentId="task.child.h.run">Run</TableHeader>
                      <TableHeader componentId="task.child.h.status">Status</TableHeader>
                      <TableHeader componentId="task.child.h.duration" align="right">Duration</TableHeader>
                    </TableRow>
                    {attempts.map((a) => (
                      <TableRow key={a.name}>
                        <TableCell>{a.job ? <Link to={`/jobs/${a.workflow}`}>{a.workflow}</Link> : a.workflow}</TableCell>
                        <TableCell>{a.job ? <Link to={`/jobs/${a.workflow}/runs/${a.id}`}>{when(a.start, true)}</Link> : when(a.start, true)}</TableCell>
                        <TableCell><Status state={a.state} /></TableCell>
                        <TableCell css={{ justifyContent: "flex-end" }}>{duration(a.start, a.end)}</TableCell>
                      </TableRow>
                    ))}
                  </Table>
                )}
                <Spacer size="lg" />
              </>
            )}

            {d.spec.type === "cloud_run" && attempts.length > 1 && (
              <>
                <Typography.Title level={4}>Attempts</Typography.Title>
                <Table scrollable>
                  <TableRow isHeader>
                    <TableHeader componentId="task.att.h.n">Attempt</TableHeader>
                    <TableHeader componentId="task.att.h.status">Status</TableHeader>
                    <TableHeader componentId="task.att.h.start">Start time</TableHeader>
                    <TableHeader componentId="task.att.h.duration" align="right">Duration</TableHeader>
                    <TableHeader componentId="task.att.h.memory">Peak memory</TableHeader>
                    <TableHeader componentId="task.att.h.output">Output</TableHeader>
                  </TableRow>
                  {attempts.map((a, i) => (
                    <TableRow key={a.name}>
                      <TableCell>{i === 0 ? "First attempt" : `Retry ${i}`}</TableCell>
                      <TableCell><Status state={a.state} /></TableCell>
                      <TableCell>{when(a.start, true)}</TableCell>
                      <TableCell css={{ justifyContent: "flex-end" }}>{duration(a.start, a.end)}</TableCell>
                      <TableCell>{memoryText(a.memory)}</TableCell>
                      <TableCell><Typography.Link componentId="task.att.show" onClick={() => select(a)}>
                        {only === a.id ? "Show all output" : "Show its output"}</Typography.Link></TableCell>
                    </TableRow>
                  ))}
                </Table>
                <Spacer size="lg" />
              </>
            )}

            {METRIC_KINDS.has(kind) && d.status && (
              <>
                <Typography.Title level={4}>Metrics</Typography.Title>
                <TaskMetrics wf={wf} id={id} task={task} running={live(d.status)} />
                <Spacer size="lg" />
              </>
            )}

            {d.spec.type !== "condition" && (
              <>
                <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: theme.spacing.sm, flexWrap: "wrap" }}>
                  <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>
                    Output{only && <Typography.Text color="secondary"> · {d.spec.type === "for_each" ? "one iteration" : "one attempt"}</Typography.Text>}
                  </Typography.Title>
                  <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm }}>
                    <div css={{ width: 240 }}>
                      <Input componentId="task.logs.filter" prefix={<SearchIcon />} placeholder="Filter output" size="small"
                             value={q} onChange={(e) => setQ(e.target.value)} allowClear aria-label="Filter output" />
                    </div>
                    <ToggleButton componentId="task.logs.errors" size="small" pressed={errorsOnly} onPressedChange={setErrorsOnly}>Errors only</ToggleButton>
                    <ToggleButton componentId="task.logs.wrap" size="small" pressed={wrap} onPressedChange={setWrap}>Wrap lines</ToggleButton>
                    <Tooltip componentId="task.logs.copy.tip" content="Copy output">
                      <Button componentId="task.logs.copy" size="small" icon={<CopyIcon />} aria-label="Copy output"
                              onClick={() => navigator.clipboard.writeText(text(lines))} disabled={!lines.length} />
                    </Tooltip>
                    <Tooltip componentId="task.logs.download.tip" content="Download output">
                      <Button componentId="task.logs.download" size="small" icon={<DownloadIcon />} aria-label="Download output"
                              onClick={download} disabled={!lines.length} />
                    </Tooltip>
                  </div>
                </div>
                <Spacer size="sm" />
                {logs.error && <Alert componentId="task.logs.error" type="error" message="Couldn't load the output" description={logs.error} closable={false} />}
                {!logs.data && !logs.error && <TableSkeleton lines={6} />}
                {logs.data && (logs.data.length === 0 ? (
                  <Empty title="No output" description={
                    d.status === "REUSED" ? "This task was reused from an earlier run, so it didn't run again."
                    : d.status === "EXCLUDED" || d.status === "UPSTREAM_FAILED" ? "This task didn't run in this run."
                    : running ? "Output appears here as soon as the task prints something."
                    : d.spec.type === "workflow" && attempts.length === 0 ? "This task ran another job before child runs were recorded."
                    : "The task printed nothing."} />
                ) : lines.length === 0 ? (
                  <Empty title="No matching output" description="No line matches the filter. Clear it to see all the output." />
                ) : (
                  <div role="log" aria-label={`Output of ${task}`} css={{
                    background: theme.colors.backgroundSecondary, border: `1px solid ${theme.colors.border}`,
                    borderRadius: theme.borders.borderRadiusMd, padding: `${theme.spacing.sm}px 0`, maxHeight: 640, overflow: "auto",
                    fontFamily: "\"DM Mono\", monospace", fontSize: theme.typography.fontSizeSm, lineHeight: theme.typography.lineHeightSm }}>
                    {lines.map((l, i) => {
                      const err = l.severity === "ERROR" || l.severity === "CRITICAL";
                      return (
                        <div key={i} css={{ display: "grid", gridTemplateColumns: "88px minmax(0, 1fr)", gap: theme.spacing.sm,
                                            padding: `0 ${theme.spacing.md}px`,
                                            background: err ? theme.colors.backgroundDanger : undefined,
                                            "&:hover": { background: theme.colors.actionDefaultBackgroundHover } }}>
                          <span css={{ color: theme.colors.textSecondary, userSelect: "none" }} title={`${when(l.time, true)} ${TIME_ZONE_LABEL}`}>{clock(l.time)}</span>
                          <span css={{ whiteSpace: wrap ? "pre-wrap" : "pre", overflowWrap: wrap ? "anywhere" : undefined,
                                       color: err ? theme.colors.textValidationDanger : theme.colors.textPrimary }}>
                            {l.task && <span css={{ color: theme.colors.textSecondary }}>[{l.task}] </span>}
                            {l.input && d.spec.type !== "cloud_run" && <span css={{ color: theme.colors.textSecondary }}>({l.input}) </span>}
                            {l.text}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                ))}
                {logs.data && logs.data.length > 0 && (
                  <><Spacer size="xs" /><Typography.Hint>{lines.length} of {logs.data.length} lines · times in {TIME_ZONE_LABEL}
                    {d.spec.type === "workflow" && kind !== "run_job_task" ? " · what the runner logged; its containers' own output is in Cloud Logging" : ""}</Typography.Hint></>
                )}
              </>
            )}
          </div>

          <div>
            <Section title="Task run details">
              <Field name="Job"><Link to={`/jobs/${wf}`}>{wf}</Link></Field>
              <Field name="Job run"><Link to={runLink}>{d.run.run_id.slice(0, 13)}</Link></Field>
              <Field name="Job run status"><Status state={runState(d.run)} /></Field>
              <Field name="Attempt">{attemptName(d.run.repair_id)}</Field>
              <Field name="Started">{d.start ? `${when(d.start, true)} ${TIME_ZONE_LABEL}` : "-"}</Field>
              <Field name="Ended">{d.end ? `${when(d.end, true)} ${TIME_ZONE_LABEL}` : "-"}</Field>
              <Field name="Duration">{d.start ? duration(d.start, d.end) : "-"}</Field>
              <Field name="Status"><Status state={d.status ?? "EXCLUDED"} /></Field>
              {d.spec.type === "cloud_run" && <Field name="Attempts">{attempts.length || "-"} of {(d.spec.max_retries ?? 0) + 1}</Field>}
              {d.spec.type === "cloud_run" && attempts.length > 0 && (
                <Field name="Peak memory">{memoryText(attempts[attempts.length - 1].memory)}</Field>)}
            </Section>
            {d.history.length > 1 && (
              <Section title="Task run history">
                {[...d.history].reverse().map((h) => (
                  <div key={h.repair_id} css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: theme.spacing.sm,
                                                padding: `${theme.spacing.xs}px 0` }}>
                    {h.execution === id
                      ? <Typography.Text bold>{attemptName(h.repair_id)}</Typography.Text>
                      : <Link to={`/jobs/${wf}/runs/${h.execution}/tasks/${encodeURIComponent(task)}`}>{attemptName(h.repair_id)}</Link>}
                    <span css={{ display: "inline-flex", alignItems: "center", gap: theme.spacing.sm }}>
                      <Typography.Text color="secondary" size="sm">{h.start ? duration(h.start, h.end) : ""}</Typography.Text>
                      <Status state={h.status ?? "EXCLUDED"} />
                    </span>
                  </div>))}
              </Section>
            )}
            <Section title="Task">
              <TaskSettings spec={d.spec} node={d.node} dependency={(k, text) => (
                <Link key={k} to={`/jobs/${wf}/runs/${id}/tasks/${encodeURIComponent(k)}`}><Tag componentId="task.dep">{text}</Tag></Link>)} />
            </Section>
            <Section title="Job parameters">
              <ParamList rows={Object.entries(d.run.params)} empty="The run used the job's defaults" />
            </Section>
          </div>
        </SidePanel>
      )}
      <RepairModal wf={wf} id={id} task={task} visible={repairing} onClose={() => setRepairing(false)}
                   onStarted={(n) => d && nav(`/jobs/${wf}/runs/${d.run.run_id}?repair_id=${n}`)} />
    </>
  );
}
