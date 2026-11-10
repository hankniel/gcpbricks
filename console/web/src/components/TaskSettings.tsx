import type { ReactNode } from "react";
import { Tag, Typography, useDesignSystemTheme } from "@databricks/design-system";
import type { Declared, GraphNode, TaskSpec } from "../api";
import { Field, Mono, ParamList } from "./Panel";
import { duration, KIND } from "../util";

const RUN_IF: Record<string, string> = { ALL_SUCCESS: "All succeeded", AT_LEAST_ONE_SUCCESS: "At least one succeeded",
  NONE_FAILED: "None failed", ALL_DONE: "All done", AT_LEAST_ONE_FAILED: "At least one failed", ALL_FAILED: "All failed" };
const EVENT: Record<string, string> = { on_start: "Start", on_success: "Success", on_failure: "Failure",
  on_duration_warning_threshold_exceeded: "Duration warning" };
/** A runner task's argument keys that are plumbing, not settings a person reads. */
const PLUMBING = new Set(["job", "target", "region", "bucket", "service_account", "sql_object", "guard_prefix",
  "stage_prefix", "spec_prefix", "models", "object", "output_prefix", "runtime_template"]);

/** What a container task runs, as Databricks' task panel lists it per type. */
function runs(spec: TaskSpec, node: GraphNode): [string, string][] {
  const s = spec.type === "for_each" ? spec.task ?? spec : spec;
  const kind = node.inner_kind ?? node.kind;
  const a = s.args ?? [];
  const flag = (f: string) => { const i = a.indexOf(f); return i >= 0 ? a[i + 1] : ""; };
  if (kind === "notebook_task") {
    const arg = (s.argument ?? {}) as Record<string, unknown>;
    const object = String(arg.object ?? "");
    return [["Notebook", object.startsWith("workspace/") ? `/Workspace/${object.slice("workspace/".length)}` : object],
            ["Runtime template", String(arg.runtime_template ?? "")]];
  }
  if (kind === "python_wheel_task") return [["Package", a[3]], ["Entry point", a[4]]];
  if (kind === "spark_python_task") return [["Python file", a[1] === "-c" ? `-c ${a[2]}` : a[1]]];
  if (kind === "dbt_task") {
    return [["Project directory", flag("--project-directory")],
            ["Schema", [flag("--catalog"), flag("--schema")].filter(Boolean).join(".")], ["Profiles directory", flag("--profiles-directory")]];
  }
  return [];
}

/** The task's parameters as name -> value: Databricks' parameter table. */
function parameters(spec: TaskSpec, node: GraphNode): [string, string][] {
  const s = spec.type === "for_each" ? spec.task ?? spec : spec;
  const kind = node.inner_kind ?? node.kind;
  const a = s.args ?? [];
  const named = Object.entries(s.options ?? spec.options ?? {});
  if (kind === "python_wheel_task") return [...named, ...a.slice(5).map((v, i): [string, string] => [`[${i}]`, v])];
  if (kind === "spark_python_task") return [...named, ...a.slice(a[1] === "-c" ? 3 : 2).map((v, i): [string, string] => [`[${i}]`, v])];
  if (kind === "dbt_task") return a.slice(a.indexOf("--") + 1).map((c, i): [string, string] => [`command ${i + 1}`, c]);
  return named;
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div css={{ borderTop: `1px solid ${theme.colors.border}`, paddingTop: theme.spacing.sm, marginTop: theme.spacing.sm }}>
      <Typography.Text bold>{title}</Typography.Text>
      <div css={{ marginTop: theme.spacing.xs }}>{children}</div>
    </div>
  );
}

/** A task's settings, grouped as Databricks' task panel groups them; `dependency` renders each
 *  upstream task, `notifications` (the job's declared ones) adds the task's own. */
export default function TaskSettings({ spec, node, dependency, notifications }: {
  spec: TaskSpec; node: GraphNode; dependency: (taskKey: string, label: string) => ReactNode;
  notifications?: Declared["notifications"];
}) {
  const { theme } = useDesignSystemTheme();
  const kind = node.kind;
  const inner = node.inner_kind ?? kind;
  const settings = spec.type === "workflow" && kind !== "run_job_task"
    ? Object.entries(spec.argument ?? {}).filter(([k]) => !PLUMBING.has(k)) : [];
  const mine = (notifications ?? []).filter((n) => n.scope === node.key);
  const job = (notifications ?? []).filter((n) => n.scope === "job");
  return (
    <>
      <Field name="Type">{KIND[kind] ?? kind}{spec.type === "for_each" ? ` of ${KIND[inner] ?? inner}` : ""}</Field>
      {(spec.run_job || spec.task?.run_job) && <Field name="Compute"><Tag componentId="task.compute">{spec.run_job ?? spec.task?.run_job}</Tag></Field>}
      {runs(spec, node).filter(([, v]) => v).map(([k, v]) => <Field key={k} name={k}><Mono>{v}</Mono></Field>)}
      {spec.type === "workflow" && <Field name={kind === "run_job_task" ? "Job" : "Runner"}>{spec.workflow}</Field>}
      {spec.type === "for_each" && <><Field name="Inputs"><Mono>{spec.inputs}</Mono></Field>
        <Field name="Concurrency">{spec.concurrency ?? 1}</Field></>}
      {spec.type === "condition" && <Field name="Condition"><Mono>{spec.left} {spec.op} {spec.right}</Mono></Field>}

      <Group title="Parameters">
        <ParamList rows={parameters(spec, node)} empty="No parameters" />
      </Group>
      {settings.length > 0 && <Group title="Runner settings"><ParamList rows={settings} empty="" /></Group>}

      <Group title="Dependencies">
        <Field name="Depends on">{node.depends_on.length ? (
          <span css={{ display: "inline-flex", gap: theme.spacing.xs, flexWrap: "wrap" }}>
            {(spec.depends_on ?? []).map((x) =>
              dependency(x.task_key, `${x.task_key}${x.outcome ? ` (${x.outcome === "true" ? "True" : "False"})` : ""}`))}
          </span>) : "-"}</Field>
        {node.depends_on.length > 0 && <Field name="Run if">{RUN_IF[spec.run_if ?? ""] ?? spec.run_if}</Field>}
      </Group>

      {spec.type !== "condition" && (
        <Group title="Retries & timeout">
          <Field name="Retries">{spec.max_retries ? `${spec.max_retries}${spec.min_retry_interval_millis ? `, ${Math.round(spec.min_retry_interval_millis / 1000)}s apart` : ""}` : "None"}</Field>
          <Field name="Retry on timeout">{spec.retry_on_timeout ? "Yes" : "No"}</Field>
          <Field name="Timeout">{spec.timeout_seconds ? duration(new Date(0).toISOString(), new Date(spec.timeout_seconds * 1000).toISOString()) : "None"}</Field>
        </Group>
      )}

      {notifications && (
        <Group title="Notifications">
          {mine.length ? mine.map((n) => <Field key={n.event} name={EVENT[n.event] ?? n.event}>
            {[...n.emails, ...n.webhooks.map((w) => `webhook ${w}`)].join(", ")}</Field>)
            : <Typography.Text color="secondary">None on this task{job.length ? `; the job notifies on ${job.map((n) => EVENT[n.event] ?? n.event).join(", ").toLowerCase()}` : ""}.</Typography.Text>}
        </Group>
      )}
    </>
  );
}
