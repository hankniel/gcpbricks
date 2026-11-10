import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Alert, Button, DropdownMenu, Empty, Input, Modal, OverflowIcon, PauseIcon, PlayIcon,
  SegmentedControlButton, SegmentedControlGroup, Spacer, SplitButton, TableSkeleton, Tabs, Tag, Typography,
  WorkflowsIcon, useDesignSystemTheme,
} from "@databricks/design-system";
import { api, type Declared } from "../api";
import PageTitle from "../components/PageTitle";
import SidePanel from "../components/SidePanel";
import { Field, Mono, ParamList, Section } from "../components/Panel";
import { RetiredNotice, RetiredTag } from "../components/Retired";
import RunsMatrix from "../components/RunsMatrix";
import RunsTable from "../components/RunsTable";
import TaskGraph from "../components/TaskGraph";
import TaskSettings from "../components/TaskSettings";
import { PipelineDefinition } from "../components/PipelineInspect";
import { live, secondsText, useLoad, useTick } from "../util";

/** Databricks' words for a notification event. */
const EVENT: Record<string, string> = {
  on_start: "Start", on_success: "Success", on_failure: "Failure",
  on_duration_warning_threshold_exceeded: "Duration warning",
};

function TriggerText({ trigger }: { trigger: NonNullable<Declared["trigger"]> }) {
  const waits = trigger.kind === "periodic" ? [] : [
    trigger.wait_after_last_change_seconds ? `waits ${secondsText(trigger.wait_after_last_change_seconds)} after the last change` : "",
    trigger.min_time_between_triggers_seconds ? `at most every ${secondsText(trigger.min_time_between_triggers_seconds)}` : "",
  ].filter(Boolean);
  return (
    <>
      {trigger.kind === "periodic" && <Field name="Periodic">Every {trigger.interval} {trigger.unit.toLowerCase()}</Field>}
      {trigger.kind === "table_update" && <>
        <Field name="Table update">{trigger.condition === "ALL_UPDATED" ? "When all tables change" : "When any table changes"}</Field>
        <Field name="Tables">{trigger.table_names.map((t) => <div key={t}><Mono>{t}</Mono></div>)}</Field>
      </>}
      {trigger.kind === "file_arrival" && <Field name="File arrival"><Mono>{trigger.url}</Mono></Field>}
      {waits.length > 0 && <Field name="Debounce">{waits.join(", ")}</Field>}
    </>
  );
}

export default function JobPage() {
  const { wf = "" } = useParams();
  const nav = useNavigate();
  const { theme } = useDesignSystemTheme();
  // A run in progress: its row's status and duration follow it within seconds.
  const [anyLive, setAnyLive] = useState(false);
  const { data: job, error, reload } = useLoad(() => api.job(wf), [wf], anyLive ? 5000 : 20000);
  useEffect(() => setAnyLive(!!job?.runs.some((r) => live(r.state))), [job]);
  useTick(anyLive);
  const [view, setView] = useState<"matrix" | "table">("matrix");
  const [ask, setAsk] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; error?: boolean } | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  // As Databricks opens the Tasks tab: the first task's settings until another is picked.
  const shown = picked ?? job?.graph.nodes[0]?.key ?? null;
  const [toRun, setToRun] = useState<string[]>([]);
  const run = async (params: Record<string, string>, only: string[] = []) => {
    setBusy(true);
    try { const r = await api.runNow(wf, params, only); nav(`/jobs/${wf}/runs/${r.execution}`); }
    catch (e) { setMsg({ text: (e as Error).message, error: true }); }
    finally { setBusy(false); }
  };
  const act = async (what: () => Promise<unknown>, done: string) => {
    try { await what(); setMsg({ text: done }); reload(); }
    catch (e) { setMsg({ text: (e as Error).message, error: true }); }
  };
  const start = job ? job.schedule ?? job.trigger : null;
  const line = `1px solid ${theme.colors.border}`;
  return (
    <>
      <PageTitle crumbs={[{ to: "/", label: "Jobs & Pipelines" }]} icon={<WorkflowsIcon />} title={job?.name ?? wf}
        addOns={<RetiredTag retired={job?.retired} />}
        actions={job && (
          <>
            <DropdownMenu.Root>
              <DropdownMenu.Trigger asChild>
                <Button componentId="job.more" icon={<OverflowIcon />} aria-label="More actions" />
              </DropdownMenu.Trigger>
              <DropdownMenu.Content align="end">
                {start && <DropdownMenu.Item componentId="job.pause" onClick={() => act(() => start.paused ? api.resume(wf) : api.pause(wf),
                  start.paused ? "Trigger resumed." : "Trigger paused.")}>{start.paused ? "Resume trigger" : "Pause trigger"}</DropdownMenu.Item>}
                <DropdownMenu.Item componentId="job.copy" onClick={() => navigator.clipboard.writeText(job.workflow)}>Copy job ID</DropdownMenu.Item>
              </DropdownMenu.Content>
            </DropdownMenu.Root>
            {!job.retired && <SplitButton componentId="job.run" type="primary" loading={busy} onClick={() => run({})}
              menu={<DropdownMenu.Content align="end">
                <DropdownMenu.Item componentId="job.run.params" onClick={() => { setValues({}); setToRun(job.graph.nodes.map((n) => n.key)); setAsk(true); }}>
                  Run now with different parameters</DropdownMenu.Item>
              </DropdownMenu.Content>}>
              Run now
            </SplitButton>}
          </>
        )} />
      <Spacer size="md" />
      {error && <Alert componentId="job.error" type="error" message="Couldn't load this job" description={error} closable={false} />}
      {job && <RetiredNotice kind="job" name={job.workflow} retired={job.retired} remove={() => api.deleteJob(job.workflow)} back="/" />}
      {msg && <><Alert componentId="job.msg" type={msg.error ? "error" : "info"} message={msg.text} onClose={() => setMsg(null)} /><Spacer size="sm" /></>}
      {!job && !error && <TableSkeleton lines={6} />}
      {job && (
        <Tabs.Root componentId="job.tabs" defaultValue="runs">
          <Tabs.List>
            <Tabs.Trigger value="runs">Runs</Tabs.Trigger>
            <Tabs.Trigger value="tasks">Tasks</Tabs.Trigger>
          </Tabs.List>
          {/* Databricks keeps the job's details beside either tab: the tab's work left, a 456px panel right. */}
          <SidePanel>
            <div css={{ flex: 1, minWidth: 0 }}>
              <Tabs.Content value="runs">
                <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", minHeight: 32 }}>
                  <Typography.Title level={2} css={{ "&&": { margin: 0 } }}>Runs</Typography.Title>
                  <SegmentedControlGroup componentId="job.view" name="view" value={view} onChange={(e) => setView(e.target.value)}>
                    <SegmentedControlButton value="table">Table</SegmentedControlButton>
                    <SegmentedControlButton value="matrix">Matrix</SegmentedControlButton>
                  </SegmentedControlGroup>
                </div>
                <Spacer size="sm" />
                {job.runs.length === 0 ? <Empty title="No runs yet" description="Start the first run with Run now." /> : (
                  <>
                    {view === "matrix" && <><div css={{ border: line, borderRadius: 8, padding: theme.spacing.sm, overflow: "hidden" }}>
                      <RunsMatrix wf={wf} runs={job.runs} tasks={job.graph.nodes.map((n) => n.key)} /></div><Spacer size="md" /></>}
                    <div css={{ border: line, borderRadius: 12, padding: theme.spacing.lg }}>
                      {(() => {
                          const ok = job.runs.find((r) => r.state === "SUCCEEDED" && !r.skipped);
                          return ok ? <div css={{ display: "flex", alignItems: "center", minHeight: 32, marginBottom: 12 }}>
                            <Typography.Link componentId="job.latest_ok" onClick={() => nav(`/jobs/${wf}/runs/${ok.id}`)}>
                              View latest successful run</Typography.Link></div> : null;
                        })()}
                      <RunsTable runs={job.runs.map((r) => ({ ...r, workflow: wf }))} />
                    </div>
                  </>
                )}
              </Tabs.Content>
              <Tabs.Content value="tasks">
                {/* Databricks' Tasks tab: the graph over the selected task's settings, in one bordered card. */}
                <div css={{ border: line, borderRadius: 8, overflow: "hidden" }}>
                  <TaskGraph nodes={job.graph.nodes} edges={job.graph.edges} height={Math.min(420, 120 + job.graph.nodes.length * 70)}
                             selected={shown} onPick={setPicked} />
                  {(() => {
                    const node = job.graph.nodes.find((n) => n.key === shown);
                    if (!node) return null;
                    return (
                      <div css={{ padding: theme.spacing.md, borderTop: line }}>
                        <Typography.Title level={4} css={{ "&&": { marginTop: 0 } }}>{node.key}</Typography.Title>
                        <div css={{ columnCount: 2, columnGap: theme.spacing.lg * 2, "& > *": { breakInside: "avoid" } }}>
                          <TaskSettings spec={job.tasks[node.key]} node={node} notifications={job.declared?.notifications} dependency={(k, text) => (
                            <Typography.Link key={k} componentId="job.task.dep" onClick={() => setPicked(k)}>
                              <Tag componentId="job.task.dep.tag">{text}</Tag></Typography.Link>)} />
                        </div>
                        {node.kind === "vertex_pipeline_task" && <>
                          <Typography.Title level={4}>Pipeline</Typography.Title>
                          <PipelineDefinition wf={wf} task={node.key} />
                        </>}
                      </div>
                    );
                  })()}
                </div>
              </Tabs.Content>
            </div>
            <div css={{ minWidth: 0 }}>
              <Section title="Job details">
                <Field name="Job ID">{job.workflow}</Field>
                <Field name="Run as">{job.run_as}</Field>
                <Field name="Bundle"><Tag componentId="job.bundle">{job.bundle}</Tag> <Tag componentId="job.target">{job.target}</Tag></Field>
                <Field name="Description">{job.description}</Field>
                <Field name="Tags">{Object.keys(job.tags).length === 0 ? "-" : Object.entries(job.tags).map(([k, v]) =>
                  <Tag key={k} componentId="job.tag">{k}: {v}</Tag>)}</Field>
              </Section>
              {!job.declared && <><Spacer size="sm" /><Alert componentId="job.declared.missing" type="info" closable={false}
                message="Deployed before the console read job settings"
                description="Redeploy the bundle on gcpbricks 0.5.2 or later to see its clusters, environments and notifications." /></>}
              <Section title="Schedules & Triggers">
                {start ? (
                  <>
                    {job.schedule && <Field name="Cron">{job.schedule.cron} ({job.schedule.time_zone})</Field>}
                    {job.declared?.trigger ? <TriggerText trigger={job.declared.trigger} />
                      : !job.schedule && <Typography.Paragraph>When its tables change</Typography.Paragraph>}
                    <Field name="Status">{start.paused ? "Paused" : "Active"}</Field>
                    <Button componentId="job.trigger.toggle" size="small" icon={start.paused ? <PlayIcon /> : <PauseIcon />}
                            onClick={() => act(() => start.paused ? api.resume(wf) : api.pause(wf), start.paused ? "Trigger resumed." : "Trigger paused.")}>
                      {start.paused ? "Resume" : "Pause"}
                    </Button>
                  </>
                ) : <Typography.Text color="secondary">No schedule or trigger: runs start by hand or from another job.</Typography.Text>}
              </Section>
              <Section title="Compute">
                {job.declared && job.declared.job_clusters.length > 0 ? job.declared.job_clusters.map((c) => (
                    <div key={c.key} css={{ marginBottom: theme.spacing.sm }}>
                      <Tag componentId="job.cluster">{c.key}</Tag>
                      <Field name="Size">{c.cpu ? `${c.cpu} vCPU, ${c.memory}` : "Cloud Run default"}{c.node_type_id ? ` (${c.node_type_id})` : ""}</Field>
                      {c.num_workers ? <Field name="Workers">{c.num_workers}</Field> : null}
                      <Field name={c.image ? "Image" : "Environment"}>{c.image ?? c.environment_key}</Field>
                      {Object.keys(c.env_vars).length > 0 && <><Spacer size="xs" /><ParamList rows={Object.entries(c.env_vars)} empty="" /></>}
                    </div>))
                  : [...new Set(job.graph.nodes.map((n) => n.run_job).filter(Boolean))].map((r) => <div key={r}><Tag componentId="job.compute">{r}</Tag></div>)}
              </Section>
              {job.declared && job.declared.environments.length > 0 && (
                <Section title="Environments">
                  {job.declared.environments.map((e) => (
                    <div key={e.key} css={{ marginBottom: theme.spacing.sm }}>
                      <Tag componentId="job.environment">{e.key}</Tag>
                      <Field name="Dependencies">{e.dependencies.map((d) => <div key={d}><Mono>{d}</Mono></div>)}</Field>
                      <Field name="Image">{e.image}</Field>
                    </div>))}
                </Section>
              )}
              <Section title="Job parameters">
                <ParamList rows={Object.entries(job.parameters)} empty="No job parameters are defined for this job" />
              </Section>
              {job.declared && (
                <Section title="Job notifications">
                  {job.declared.notifications.length === 0
                    ? <Typography.Text color="secondary">No notifications. Failures still raise the bundle's run-failed alert.</Typography.Text>
                    : job.declared.notifications.map((n) => (
                      <Field key={`${n.scope}:${n.event}`} name={`${n.scope === "job" ? "" : `${n.scope}: `}${EVENT[n.event] ?? n.event}`}>
                        {[...n.emails, ...n.webhooks.map((w) => `webhook ${w}`)].map((to) => <div key={to}>{to}</div>)}
                      </Field>))}
                  {job.declared.no_alert_for_skipped_runs && <Field name="Skipped runs">No alert</Field>}
                </Section>
              )}
              <Section title="Advanced settings">
                <Field name="Max concurrent runs">{String(job.settings.max_concurrent_runs)}</Field>
                <Field name="Queue">{job.settings.queue ? "Enabled" : "Disabled"}</Field>
                <Field name="Timeout">{job.settings.timeout_seconds ? secondsText(Number(job.settings.timeout_seconds)) : "None"}</Field>
                <Field name="Duration warning">{job.settings.duration_warning_seconds ? secondsText(Number(job.settings.duration_warning_seconds)) : "None"}</Field>
              </Section>
            </div>
          </SidePanel>
        </Tabs.Root>
      )}
      {/* Databricks' run-with-different-settings: the tasks to run on the left, parameters on the right. */}
      <Modal componentId="job.params.modal" title="Run now with different settings" visible={ask} size="wide"
             okText={job && toRun.length < job.graph.nodes.length ? `Run ${toRun.length} task(s)` : "Run"}
             okButtonProps={{ disabled: toRun.length === 0 }} cancelText="Cancel" confirmLoading={busy}
             onCancel={() => setAsk(false)}
             onOk={() => job && run(Object.fromEntries(Object.entries(values).filter(([, v]) => v !== "")),
                                    toRun.length < job.graph.nodes.length ? toRun : [])}>
        {job && (
          <div css={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 300px", gap: theme.spacing.lg }}>
            <div>
              <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: theme.spacing.sm }}>
                <Typography.Text bold>Tasks to run <Typography.Text color="secondary">
                  ({toRun.length} of {job.graph.nodes.length})</Typography.Text></Typography.Text>
                <span css={{ display: job.runs_some ? "inline-flex" : "none", gap: theme.spacing.sm }}>
                  <Typography.Link componentId="job.params.all" onClick={() => setToRun(job.graph.nodes.map((n) => n.key))}>All</Typography.Link>
                  <Typography.Link componentId="job.params.none" onClick={() => setToRun([])}>None</Typography.Link>
                </span>
              </div>
              <TaskGraph nodes={job.graph.nodes} edges={job.graph.edges} height={460} selected={toRun}
                         onPick={job.runs_some ? (k) => setToRun(toRun.includes(k) ? toRun.filter((x) => x !== k) : [...toRun, k]) : undefined} />
              <Spacer size="xs" />
              <Typography.Hint>{job.runs_some ? "Click a task to add or remove it. A task runs even when a task it depends on is left out."
                : "Every task runs: this job was deployed before a run could pick its tasks. Its bundle's next deploy allows it."}</Typography.Hint>
            </div>
            <div>
              <Typography.Text bold>Job parameters</Typography.Text>
              <Spacer size="sm" />
              {Object.keys(job.parameters).length === 0 && <Typography.Text color="secondary">This job has no parameters.</Typography.Text>}
              {Object.keys(job.parameters).length > 0 && (
                // Databricks' Key | Value table: the name in the code font, an input for its value.
                <div css={{ display: "grid", gridTemplateColumns: "minmax(90px, 40%) minmax(0, 1fr)", alignItems: "center",
                            border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd, overflow: "hidden" }}>
                  <Typography.Text color="secondary" size="sm" css={{ padding: `${theme.spacing.xs}px ${theme.spacing.sm}px`,
                    background: theme.colors.backgroundSecondary, borderBottom: `1px solid ${theme.colors.border}` }}>Key</Typography.Text>
                  <Typography.Text color="secondary" size="sm" css={{ padding: `${theme.spacing.xs}px ${theme.spacing.sm}px`,
                    background: theme.colors.backgroundSecondary, borderBottom: `1px solid ${theme.colors.border}` }}>Value</Typography.Text>
                  {Object.entries(job.parameters).map(([name, def], i) => (
                    <div key={name} css={{ display: "contents" }}>
                      <label htmlFor={`p-${name}`} css={{ padding: `${theme.spacing.xs}px ${theme.spacing.sm}px`,
                        borderTop: i ? `1px solid ${theme.colors.border}` : undefined, alignSelf: "stretch", display: "flex", alignItems: "center" }}>
                        <Mono>{name}</Mono></label>
                      <div css={{ padding: theme.spacing.xs, borderTop: i ? `1px solid ${theme.colors.border}` : undefined }}>
                        <Input id={`p-${name}`} componentId="job.params.input" placeholder={def || "(empty)"} value={values[name] ?? ""}
                               onChange={(e) => setValues({ ...values, [name]: e.target.value })} />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </Modal>
    </>
  );
}
