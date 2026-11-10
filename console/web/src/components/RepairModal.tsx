import { Fragment, useEffect, useMemo, useState } from "react";
import { Alert, Checkbox, Input, Modal, Spacer, Switch, Tag, Typography, useDesignSystemTheme } from "@databricks/design-system";
import { api, type JobDetail, type RunDetail } from "../api";
import TaskGraph from "./TaskGraph";
import { useToast } from "./Toast";

type Plan = Awaited<ReturnType<typeof api.repairPlan>>;

/** Databricks' "Repair job run": a new attempt of the run from its latest one. The tasks that
 *  didn't succeed are picked to run again, and any task can be picked or dropped on the graph (a
 *  dropped one that didn't succeed keeps how it ended, which its dependents see); a
 *  batch_serve_task's failed models resume, and a ticked model runs again in full. Parameters
 *  override the run's, and later repairs keep them. Starting it toasts "Repair run started".
 *  `task`: opened from that task's page (Databricks' "Repair task"), which starts picked. */
export default function RepairModal({ wf, id, visible, onClose, onStarted, task }: {
  wf: string; id: string; visible: boolean; onClose: () => void; onStarted?: (repairId: number) => void; task?: string;
}) {
  const { theme } = useDesignSystemTheme();
  const notify = useToast();
  const [job, setJob] = useState<JobDetail | null>(null);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [plan, setPlan] = useState<Plan["models"]>({});
  const [info, setInfo] = useState<Plan | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [added, setAdded] = useState<string[]>([]);
  // Tasks that didn't succeed, dropped: they end as they did, not run again.
  const [kept, setKept] = useState<string[]>([]);
  const [fresh, setFresh] = useState<string[]>([]);
  // A pipeline task's succeeded steps to rerun too, and the pipeline tasks started anew instead.
  const [stepsAgain, setStepsAgain] = useState<Record<string, string[]>>({});
  const [freshPipes, setFreshPipes] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!visible) return;
    setJob(null); setRun(null); setPlan({}); setInfo(null); setAdded([]); setKept([]); setFresh([]); setStepsAgain({}); setFreshPipes([]); setError(null);
    api.repairPlan(wf, id).then((p) => Promise.all([api.job(wf), api.run(wf, id, p.latest_repair_id)]).then(([j, r]) => {
      setJob(j); setRun(r); setPlan(p.models); setInfo(p);
      setValues(Object.fromEntries(Object.keys(p.parameters).map((k) => [k, p.overrides[k] ?? base(p, k)])));
      // A task that didn't succeed starts picked anyway; a succeeded one is picked as a click on it would.
      if (task && ["SUCCESS", "REUSED"].includes(r.task_runs[task]?.status ?? "")) {
        setAdded([task]);
        setFresh((p.models[task] ?? []).map((m) => m.model));
      }
    })).catch((e: Error) => setError(e.message));
  }, [visible, wf, id, task]);

  // A task an earlier attempt ran successfully and this one reused succeeded too.
  const succeeded = (k: string) => ["SUCCESS", "REUSED"].includes(run?.task_runs[k]?.status ?? "");
  // A run of some tasks repairs those alone: the rest stay out, as they were.
  const outside = (k: string) => (info?.only.length ?? 0) > 0 && !info!.only.includes(k);
  const nodes = job?.graph.nodes ?? [];
  const again = useMemo(() => nodes.filter((n) => !outside(n.key) && (succeeded(n.key)
    ? added.includes(n.key) || (stepsAgain[n.key]?.length ?? 0) > 0 : !kept.includes(n.key))).map((n) => n.key),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [nodes, run, info, added, kept, stepsAgain]);
  const toggleTask = (k: string) => {
    if (outside(k)) return;
    if (!succeeded(k)) {
      setKept(kept.includes(k) ? kept.filter((x) => x !== k) : [...kept, k]);
      return;
    }
    const on = !added.includes(k);
    setAdded(on ? [...added, k] : added.filter((x) => x !== k));
    // A succeeded batch_serve_task added runs all its models again; dropped, none.
    const models = (plan[k] ?? []).map((m) => m.model);
    setFresh(on ? [...new Set([...fresh, ...models])] : fresh.filter((m) => !models.includes(m)));
  };
  const toggleModel = (task: string, model: string) => {
    const on = !fresh.includes(model);
    const next = on ? [...fresh, model] : fresh.filter((m) => m !== model);
    setFresh(next);
    if (succeeded(task)) {
      const any = (plan[task] ?? []).some((m) => next.includes(m.model));
      setAdded(any ? [...new Set([...added, task])] : added.filter((x) => x !== task));
    }
  };
  const start = async () => {
    setBusy(true);
    try {
      // A value back at the run's own drops its override; any other overrides it.
      const params: Record<string, string | null> = {};
      for (const [k, v] of Object.entries(values)) {
        if (v !== base(info!, k)) params[k] = v;
        else if (k in info!.overrides) params[k] = null;
      }
      const steps = Object.fromEntries(Object.entries(stepsAgain).filter(([k, v]) => v.length && !freshPipes.includes(k)));
      const r = await api.repair(wf, id, { models: fresh, tasks: added, keep: kept, params, latest_repair_id: info!.latest_repair_id,
                                           steps, fresh: freshPipes });
      onClose();
      notify({ title: `Repair ${r.repair_id} started`, description: `${again.length} task${again.length === 1 ? "" : "s"} running again.`,
               link: { to: `/jobs/${wf}/runs/${r.run_id}?repair_id=${r.repair_id}`, text: "View run" } });
      onStarted?.(r.repair_id);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const status = run ? Object.fromEntries(nodes.map((n) => [n.key, run.task_runs[n.key]?.status ?? null])) : undefined;
  return (
    <Modal componentId="run.repair.modal" title={task ? "Repair task" : "Repair job run"} visible={visible} size="wide" onCancel={onClose}
           okText={`${task ? "Repair task" : "Repair run"} (${again.length})`} okButtonProps={{ disabled: !job || !run || again.length === 0 }}
           confirmLoading={busy} cancelText="Cancel" onOk={start}>
      {error && <><Alert componentId="run.repair.error" type="error" message={error} closable={false} /><Spacer size="sm" /></>}
      {job && run && (
        <div css={{ display: "grid", gridTemplateColumns: Object.keys(plan).length || Object.keys(info?.pipelines ?? {}).length
                      ? "minmax(0, 1fr) 300px" : "1fr", gap: theme.spacing.lg }}>
          <div>
            <Typography.Text bold>Tasks to repair <Typography.Text color="secondary">({again.length} of {nodes.length - nodes.filter((n) => outside(n.key)).length})</Typography.Text></Typography.Text>
            <Spacer size="sm" />
            <TaskGraph nodes={nodes} edges={job.graph.edges} height={400} status={status} selected={again} onPick={toggleTask} />
            <Spacer size="xs" />
            <Typography.Hint>Click a task to pick it or drop it. The tasks that didn't succeed are picked; one dropped
              keeps how it ended, as its dependents see it. The rest reuse this run's results.{(info?.only.length ?? 0) > 0 && " This run ran only some tasks: the rest stay out."}</Typography.Hint>
          </div>
          {Object.keys(info?.pipelines ?? {}).length > 0 && (
            <div>
              <Typography.Text bold>Pipeline steps</Typography.Text>
              <Spacer size="xs" />
              <Typography.Hint>Rerun in Vertex (Preview): steps that succeeded keep their outputs, the rest run on the
                code deployed now. Tick a step to run it again too.</Typography.Hint>
              {Object.entries(info!.pipelines).map(([task, p]) => {
                const anew = freshPipes.includes(task);
                return (
                  <div key={task} css={{ marginTop: theme.spacing.md }}>
                    <Typography.Text color="secondary" size="sm">{task} · {p.pipeline_job.split("/").pop()}</Typography.Text>
                    {p.steps.map((s) => {
                      const failed = s.state !== "SUCCEEDED";
                      const picked = failed || (stepsAgain[task] ?? []).includes(s.name);
                      return (
                        <div key={s.name} css={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                                                   gap: theme.spacing.sm, marginTop: theme.spacing.xs }}>
                          <Checkbox componentId="run.repair.step" isChecked={anew || picked} disabled={anew || failed}
                                    onChange={() => setStepsAgain({ ...stepsAgain, [task]: picked
                                      ? (stepsAgain[task] ?? []).filter((x) => x !== s.name) : [...(stepsAgain[task] ?? []), s.name] })}>
                            {s.name}</Checkbox>
                          <Tag componentId="run.repair.step.state" color={anew || picked ? "coral" : "default"}>
                            {anew ? "Runs" : picked ? "Reruns" : "Reused"}</Tag>
                        </div>);
                    })}
                    <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm, marginTop: theme.spacing.sm }}>
                      <Switch componentId="run.repair.fresh" checked={anew}
                              onChange={(on) => setFreshPipes(on ? [...freshPipes, task] : freshPipes.filter((x) => x !== task))} />
                      <Typography.Text size="sm">Start a new pipeline instead</Typography.Text>
                    </div>
                  </div>);
              })}
              <Spacer size="md" />
            </div>
          )}
          {info && Object.keys(info.parameters).length > 0 && (
            <div css={{ gridColumn: "1 / -1" }}>
              <Typography.Text bold>Job parameters</Typography.Text>
              <Spacer size="xs" />
              <Typography.Hint>Override the run's values for this repair; later repairs keep the overrides. Set one back to
                the run's value to drop its override.</Typography.Hint>
              <div css={{ display: "grid", gridTemplateColumns: "minmax(120px, 220px) minmax(0, 1fr) auto", gap: theme.spacing.sm,
                          alignItems: "center", marginTop: theme.spacing.sm }}>
                {Object.keys(info.parameters).map((k) => (
                  <Fragment key={k}>
                    <Typography.Text css={{ fontFamily: "monospace" }}>{k}</Typography.Text>
                    <Input componentId="run.repair.param" value={values[k] ?? ""} placeholder={base(info, k)}
                           onChange={(e) => setValues({ ...values, [k]: e.target.value })} />
                    {values[k] !== base(info, k)
                      ? <Tag componentId="run.repair.param.tag" color="lemon">Override</Tag>
                      : <Typography.Text color="secondary" size="sm">the run's</Typography.Text>}
                  </Fragment>
                ))}
              </div>
            </div>
          )}
          {Object.keys(plan).length > 0 && (
            <div>
              <Typography.Text bold>Batch serving models</Typography.Text>
              <Spacer size="xs" />
              <Typography.Hint>A failed model resumes on this run's window and feature snapshot, skipping the chunks it
                finished. Tick a model to run it again in full.</Typography.Hint>
              {Object.entries(plan).map(([task, models]) => (
                <div key={task} css={{ marginTop: theme.spacing.md }}>
                  <Typography.Text color="secondary" size="sm">{task}</Typography.Text>
                  {models.map((m) => (
                    <div key={m.model} css={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                                               gap: theme.spacing.sm, marginTop: theme.spacing.xs }}>
                      <Checkbox componentId="run.repair.model" isChecked={fresh.includes(m.model)}
                                onChange={() => toggleModel(task, m.model)}>{m.model}</Checkbox>
                      <Tag componentId="run.repair.status" color={m.status === "FAILED" && !fresh.includes(m.model) ? "coral" : "default"}>
                        {fresh.includes(m.model) ? "In full" : m.status === "FAILED" ? "Resumes" : "Reused"}
                      </Tag>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

/** A parameter's value without the repairs' overrides: the run's own, else the job's default. */
function base(p: Plan, k: string): string {
  return p.original[k] ?? p.parameters[k] ?? "";
}
