// The console API (gcpbricks.console.app), typed as the service returns it.
export type Start = { name: string; cron: string; time_zone: string; paused: boolean } | null;
export type RunSummary = {
  id: string; state: string; skipped: boolean; with_failures?: boolean; start: string; end?: string;
  params: Record<string, string>; tasks: Record<string, number>; repair: boolean;
  task_states: Record<string, string>; error_message: string;
  /** The run this execution is an attempt of, and which (0: the original, n: repair n). */
  run_id: string; repair_id: number;
  /** On a job's runs, one row per run: its latest attempt's execution and how many repairs it has. */
  execution?: string; repairs?: number;
  /** Finished tasks' durations, on the job page's runs. */
  task_seconds?: Record<string, number>;
};
export type Job = {
  /** `job` is its key, `name` what it's called (the bundle's `name`, else its key). */
  workflow: string; job: string; name: string; target: string; bundle: string; description: string; run_as: string;
  /** When its bundle stopped declaring it: kept, but nothing starts it. */
  retired: string | null;
  schedule: Start; trigger: Start; runs: RunSummary[];
};
export type GraphNode = {
  /** What a node reads as, when not its key (a pipeline step's display name). */
  label?: string;
  key: string; type: string; kind: string; inner_kind: string | null; level: number; run_if: string; run_job?: string;
  workflow?: string; for_each: boolean; max_retries: number; detail: string; depends_on: string[];
  /** A pipeline step's enclosing condition (a KFP dsl.If / Else group): drawn inside it. */
  parent?: string | null;
};
/** A job as its bundle declares it (bundle.job_settings), every default resolved. */
export type Declared = {
  trigger: ({ kind: "periodic"; interval: number; unit: string }
    | { kind: "table_update"; table_names: string[]; condition: string; min_time_between_triggers_seconds: number;
        wait_after_last_change_seconds: number }
    | { kind: "file_arrival"; url: string; min_time_between_triggers_seconds: number;
        wait_after_last_change_seconds: number }) | null;
  notifications: { scope: string; event: string; emails: string[]; webhooks: string[] }[];
  no_alert_for_skipped_runs: boolean;
  job_clusters: { key: string; node_type_id: string | null; cpu: string | null; memory: string | null; image: string | null;
                  environment_key: string | null; num_workers: number | null; env_vars: Record<string, string> }[];
  environments: { key: string; dependencies: string[]; image: string }[];
};
export type JobDetail = Job & {
  /** Its Workflow runs some tasks only; one deployed earlier runs them all. */
  runs_some: boolean;
  parameters: Record<string, string>;
  settings: Record<string, number | boolean | null>;
  /** null for a job deployed before the platform carried it. */
  declared: Declared | null;
  tags: Record<string, string>;
  graph: { nodes: GraphNode[]; edges: { from: string; to: string; outcome: string }[] };
  tasks: Record<string, TaskSpec>;
};
/** A Vertex pipeline run's steps (a vertex_pipeline_task's run), as the GCP pipeline view draws them. */
export type PipelineStep = { key: string; label: string; kind: "step" | "condition"; condition: string; state: string;
  start: string | null; end: string | null; error: string; parent: string | null; level: number };
export type PipelineRun = { run: { name: string; id: string; state: string; start: string | null; end: string | null;
  error: string; link: string } | null; nodes?: PipelineStep[]; edges?: { from: string; to: string }[] };
export type Execution = { name: string; kind: "cloud_run" | "workflow"; input: string; start: string };
export type TaskRun = {
  key: string; status: string | null; start: string | null; end: string | null; outcome?: string; error: string;
  executions: Execution[];
  /** The attempt of the run that ran it (a reused task's earlier one). */
  ran_in?: number;
  /** A vertex_pipeline_task that succeeded: its steps a condition kept from running (a gate that said no). */
  excluded?: string[];
};
/** An attempt of a run, as Databricks' repair_history lists them; a repair still starting has no execution. */
export type RepairAttempt = {
  type: "ORIGINAL" | "REPAIR"; repair_id: number; execution: string; state?: string; start?: string; end?: string;
  by?: string; time?: string; overrides?: Record<string, string>; tasks?: string[]; models?: string[];
};
export type RunDetail = RunSummary & {
  workflow: string; error?: string; task_runs: Record<string, TaskRun>;
  latest_repair_id: number; repair_history: RepairAttempt[];
};
/** One execution a task started, with its own state; `job` is set when it ran a bundle job. */
export type Attempt = Execution & {
  id: string; state: string; end: string | null; workflow: string | null; job: string | null;
  /** A container's peak memory against its limit (Cloud Run attempts only). */
  memory?: { limit: string | null; peak_bytes: number | null; peak_fraction: number | null };
};
/** A task's definition, as the job's rendered plan has it. */
export type TaskSpec = {
  type: string; run_job?: string; args?: string[]; workflow?: string; argument?: Record<string, unknown>;
  options?: Record<string, string>; op?: string; left?: string; right?: string; inputs?: string; concurrency?: number;
  task?: TaskSpec; max_retries?: number; min_retry_interval_millis?: number; timeout_seconds?: number;
  retry_on_timeout?: boolean; run_if?: string; depends_on?: { task_key: string; outcome?: string }[];
};
export type TaskRunDetail = TaskRun & {
  job: string; job_name: string; workflow: string; node: GraphNode; spec: TaskSpec; attempts: Attempt[];
  run: { id: string; state: string; start: string; end?: string; params: Record<string, string>; repair: boolean; skipped: boolean; with_failures?: boolean;
         run_id: string; repair_id: number; latest_repair_id: number };
  /** The task in each attempt of its run. */
  history: { repair_id: number; execution: string; status: string | null; start: string | null; end: string | null }[];
};
/** What one execution (a Cloud Run execution, a pipeline step's custom job) used, as the
 *  task's Metrics show it; fractions are 0-1. */
export type MetricUnit = {
  label: string; kind: "cloud_run" | "custom_job"; name: string; id: string; start: string | null; end: string | null;
  /** A custom job's creation: before its start, it waited for machines. */
  created?: string | null;
  state?: string; service_account?: string; link: string; cost_usd: number | null;
  machine: { cpu?: number | null; memory_bytes?: number | null; tasks?: number; machine_type?: string;
    accelerator?: string | null; accelerators?: number | null; replicas?: number };
  usage: { cpu_mean?: number | null; cpu_peak?: number | null; memory_peak?: number | null; memory_peak_bytes?: number | null;
    accelerator_mean?: number | null; net_in_bytes?: number; net_out_bytes?: number; vcpu_seconds?: number;
    gib_seconds?: number; machine_hours?: number; estimated?: boolean };
  series: { t: string[]; [metric: string]: (number | null)[] | string[] };
};
export type BqJob = { job_id: string; project: string; location: string; type: string; statement: string | null;
  state: string; error: string | null; bytes_processed: number; bytes_billed: number; slot_ms: number; cache_hit: boolean;
  spilled_bytes: number; destination: string | null; cost_usd: number; labels: Record<string, string> };
export type DbtModel = { unique_id: string; name: string; resource: string; command: string; status: string;
  seconds: number | null; start: string | null; relation: string | null; message: string | null; rows: number | null;
  job_id: string | null; project: string | null; location: string | null; bytes_processed: number | null;
  bytes_billed: number | null; slot_ms: number | null; cache_hit: boolean | null; spilled_bytes: number | null;
  cost_usd: number | null };
export type Insight = { level: "warning" | "info"; text: string };
export type TaskStats = { kind: string; units: MetricUnit[]; dbt: DbtModel[]; insights: Insight[];
  bigquery: { jobs: BqJob[]; totals: { jobs: number; bytes_processed: number; bytes_billed: number; slot_ms: number; cost_usd: number } };
  totals: { compute_usd: number | null; bigquery_usd: number; seconds: number | null };
  prices: { bq_usd_per_tib: number; run_usd_per_vcpu_s: number; run_usd_per_gib_s: number } };
export type BqStage = { id: string; name: string; status: string; start_ms: number; end_ms: number;
  wait_avg: number; wait_max: number; read_avg: number; read_max: number; compute_avg: number; compute_max: number;
  write_avg: number; write_max: number; records_read: number; records_written: number; shuffle_bytes: number;
  spilled_bytes: number; slot_ms: number; parallel_inputs: number; inputs: string[];
  steps: { kind: string; substeps: string[] }[] };
export type BqProfile = BqJob & { query: string | null; partitions: number; referenced: string[]; stages: BqStage[];
  insights: Insight[]; link: string };
/** A pipeline step's component as compiled: inputs and what feeds each, outputs, what it runs. */
export type StepComponent = { key: string; label: string; component: string; description: string; cache: boolean;
  condition: string; depends_on: string[];
  inputs: { name: string; kind: "parameter" | "artifact"; type: string | null; default?: unknown; optional: boolean; from: string }[];
  outputs: { name: string; kind: "parameter" | "artifact"; type: string | null }[];
  runs: { kind: string | null; image?: string; command?: string[]; args?: string[]; function?: string | null;
    source?: string | null; artifact_uri?: string; type?: string;
    resources?: { cpu: string | null; memory: string | null; accelerator: string | null; accelerators: string | null } } };
export type PipelineDefinition = { version: string; image: string; spec_uri: string; name: string; sdk: string;
  parameters: { name: string; type: string; default: unknown; value: unknown }[];
  nodes: PipelineStep[]; edges: { from: string; to: string }[]; steps: Record<string, StepComponent> };
export type StepArtifact = { name: string; type: string; uri: string; metadata: Record<string, unknown>; resource: string; text: boolean };
export type StepRun = { key: string; state: string; start: string | null; end: string | null; cached: boolean; error: string;
  component: StepComponent | null; inputs: Record<string, unknown>; outputs: Record<string, unknown>;
  input_artifacts: StepArtifact[]; output_artifacts: StepArtifact[]; custom_job: string | null;
  compute: MetricUnit | null; logs: { time: string; severity: string; source: string; text: string }[] };
/** Where a run's failure came from (console/failure.py): the chain walked down to its leaf,
 *  the leaf's own error and last log lines, and the code the bundle last deployed. */
export type WhyFailed = {
  path: { kind: string; label: string; state: string | null; link: string }[];
  error: string; logs: { time: string; severity: string; text: string }[];
  code: { version?: string; commit?: string; branch?: string; commit_url?: string; dirty?: boolean;
          deployed_at?: string; deployed_by?: string; ci_url?: string; mlops_version?: string };
};
export type LogLine = { time: string; severity: string; execution: string; input: string; task: string; text: string };

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, init);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    // A change made as the user needs their own GCP token first: Google's consent, then back here.
    if (r.status === 401 && String(body.detail || "").startsWith("gcp_access")) {
      window.location.href = `/api/notebooks/auth/start?next=${encodeURIComponent(location.pathname + location.search)}`;
      throw new Error("Allowing GCP access: do it again once you're back");
    }
    throw new Error(body.detail || `${r.status} ${r.statusText}`);
  }
  return r.json();
}

const post = <T,>(path: string, body?: unknown) =>
  call<T>(path, { method: "POST", headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(body ?? {}) });

export type Experiment = { id: string; name: string; created: string; updated: string; description: string;
  retired: string | null };
export type ExpRun = { id: string; name: string; type: string; created: string; updated: string; state: string;
  params: Record<string, unknown>; metrics: Record<string, number>;
  /** A nested run's parent run name (MLflow's mlflow.parentRunId), e.g. a sweep's trial's sweep. */
  parent: string | null };
export type ModelSummary = { id: string; name: string; description: string; latest: string | null; versions: number;
  aliases: Record<string, string>; updated: string; retired: string | null };
/** What a model version reads and is fit on (its features.json, else its signature and feature_spec.json). */
export type VersionFeatures = { artifact_uri: string; logged: boolean; sources: Record<string, string>;
  read: string[] | null; model_inputs: Record<string, string[]>; computed: string[] | null;
  signature: { name: string; type: string }[]; read_not_fit: string[]; fit_not_read: string[] };
/** A version's training_summary.json (mlflow.training_summary()) and what its training cost. */
export type FeatureStat = { kind: "numeric" | "categorical" | "datetime" | "boolean"; null_rate: number; mean?: number | null;
  std?: number | null; min?: number | string | null; p01?: number | null; p25?: number | null; p50?: number | null;
  p75?: number | null; p99?: number | null; max?: number | string | null; distinct?: number; top?: [string, number][];
  true_rate?: number | null };
export type TrainingSummary = { created: string; execution: string | null;
  data: { splits: Record<string, { rows: number; from?: string | null; to?: string | null }>; time_column: string | null;
          source: { snapshot?: string; query_sha256?: string } };
  features: { rows_used: number; sampled: boolean; columns: Record<string, FeatureStat> };
  label: { name: string; kind: "binary" | "numeric" | "categorical"; definition: string | null;
           splits: Record<string, Record<string, number | null | [string, number][]>> } | null;
  importance: { kind: string; features: number; top: { name: string; value: number | null; share: number | null }[] } | null };
export type VersionTraining = { artifact_uri: string; summary: TrainingSummary | null; cost_error: string | null;
  cost: { kind: "vertex" | "cloud_run"; name: string; start: string | null; end: string | null; seconds: number | null;
          usd: number | null; usd_per_hour?: number | null; machine: string; price_source: string;
          pipeline?: { steps: number; usd: number; seconds: number } } | null };
/** The Costs page (console/costs.py): list-price estimates per day and per service, and what bills right now. */
export type CostSource = "bigquery" | "cloud_run" | "vertex" | "notebooks";
export type CostItem = { source: CostSource; name: string; kind: string; usd: number; usd_upper?: number; owner?: string;
  machine?: string; usd_per_hour?: number; disk_usd?: number; jobs?: number; tib_billed?: number; min_instances?: number;
  min_replicas?: number; unpriced?: string };
export type CostRunning = { source: CostSource; name: string; kind: string; usd_per_hour: number; since: string | null;
  owner?: string; detail?: string };
export type CostReport = { window: { start: string; end: string; days: number };
  days: ({ day: string; notebooks_upper?: number } & Partial<Record<CostSource, number>>)[];
  totals: Partial<Record<CostSource | "all" | "notebooks_upper", number>>; items: CostItem[]; running: CostRunning[];
  errors: Partial<Record<CostSource, string>>; notes: string[]; price_source: string;
  prices: { sku: string; description: string; usd: number; unit: string }[] };
export type ModelVersion = { version: string; aliases: string[]; default: boolean; labels: Record<string, string>;
  created: string; updated: string; description: string; artifact_uri: string; image: string; source: string;
  schemata: { instanceSchemaUri?: string; parametersSchemaUri?: string; predictionSchemaUri?: string };
  source_run: { experiment: string; id: string; name: string; state: string; params: Record<string, unknown>;
    metrics: Record<string, number> } | null;
  serving: { id: string; name: string; traffic: number }[] };
export type Model = { id: string; name: string; description: string; created: string; updated: string;
  labels: Record<string, string>; retired: string | null; experiment: string | null; versions: ModelVersion[] };
export type Deployed = { id: string; name: string; model: string; version: string; traffic: number; machine: string;
  replicas: [number | null, number | null]; created: string };
export type Endpoint = { id: string; name: string; description: string; labels: Record<string, string>; created: string;
  updated: string; ready: boolean; deployed: Deployed[]; retired: string | null };
export type FeatureGroup = { name: string; offline_table: string; keys: string[]; key_types: Record<string, string>;
  timestamp: string; display_name?: string; kind?: "feature" | "label"; description: string;
  project: string; features: { name: string; dtype: string; family: string; description: string }[];
  families: Record<string, number>; n_features: number };

export type Dataset = { id: string; location: string; labels: Record<string, string> };
export type TableSummary = { id: string; type: string; created: string | null; labels: Record<string, string>;
  partitioning: string | null; clustering: string[] };
export type DatasetDetail = Dataset & { description: string; created: string | null; updated: string | null;
  default_table_expiration_ms: string | null; tables: TableSummary[] };
export type Column = { name: string; type: string; mode: string; description: string; fields: Column[] };
export type TableDetail = { id: string; dataset: string; project: string; full_name: string; type: string; location: string;
  description: string; labels: Record<string, string>; created: string | null; updated: string | null; expires: string | null;
  rows: number | null; bytes: number | null; partitioning: { type: string; field: string } | null;
  require_partition_filter: boolean; clustering: string[]; view_query: string | null;
  external: { format: string; uris: string[] } | null; previewable: boolean; schema: Column[] };
export type Sample = { columns: string[]; rows: unknown[][]; total_rows: number; billed_bytes: number; source: "preview" | "query" };
/** A lineage node; its id is "<kind>:<name>" (table, path, model). A table carries its type and
 * columns; `via` is the process on its edge to the node it was listed from (a predict job). */
export type LineageNode = { id: string; kind: "table" | "path" | "model"; name: string; label: string;
  project?: string; dataset?: string; table?: string; type?: string; columns?: { name: string; type: string }[];
  model_id?: string; model_name?: string; version?: string; aliases?: string[];
  role?: "features" | "labels"; last_seen?: string; readable?: boolean; via?: { kind: "job"; name: string } | null };
export type Lineage = { node: LineageNode; upstream: LineageNode[]; downstream: LineageNode[] };
export type VersionFlow = { model: string; name: string; version: string; aliases: string[];
  train_start: string | null; train_cutoff: string | null; features: string | null; labels: string | null;
  predict_job: string | null; predictions: string | null };
export type VersionLineage = { flow: VersionFlow | null;
  runs: { id: string; start: string; end: string | null; state: string }[] };

/** A batch_serve_task's model: its Cloud Run job, or a Vertex AI batch prediction into output_table. */
export type ServedModel = { model: string; job?: string; vertex?: { registered_model: string; output_table: string };
  window_days?: number };
export type ServeTask = { workflow: string; job: string; bundle: string; target: string; task: string;
  models: ServedModel[]; window_days: number; schedule: Start; trigger: Start; last_run: RunSummary | null };
export type ServeTaskDetail = Omit<ServeTask, "schedule" | "trigger" | "last_run"> & {
  settings: Record<string, number | string | null>; runs: RunSummary[] };

/** Another project's table: the API reads it in `project`, as the console's account may. */
const inProject = (project?: string) => (project ? `?project=${encodeURIComponent(project)}` : "");

/** A bundle's dashboard (gcpbricks.dashboards), as the console finds it. */
export type DashboardSummary = { bucket: string; object: string; key: string; title: string; bundle: string;
                                 target: string; widgets: number; updated: string };
export type WidgetSpec = {
  title: string; type: "counter" | "table" | "line" | "bar" | "logs" | "alerts" | "text";
  source?: string; dataset?: string; measure: string; by: string[]; bin?: string; window?: string; width: number;
};
export type DashboardFilter = { source?: string; dataset?: string; field: string; label?: string };
export type DashboardDetail = DashboardSummary & {
  spec: { window: string; widgets: WidgetSpec[]; filters?: Record<string, DashboardFilter> };
};
/** What a deploy of a bundle to a target put where (bundle.deployment). */
export type Deployment = {
  bundle: string; target: string; mode: string | null; project: string; region: string; bucket: string; run_as: string;
  version: string; mlops_version: string; declared_mlops_version: string; deployed_at: string; deployed_by: string;
  ci_url: string;
  resources: { jobs: Record<string, string>; experiments: Record<string, string>; registered_models: Record<string, string>;
               model_serving_endpoints: Record<string, string>; schemas: Record<string, { project: string; dataset: string }>;
               dashboards: Record<string, string> };
};
export type BundleRow = { bundle: string; target: string; mode: string | null; version: string | null;
  mlops_version: string | null; deployed_at: string; deployed_by: string; jobs: number; recorded: boolean };
export type BundleDetail = { bundle: string; targets: { target: string; deployment: Deployment | null;
  jobs: { workflow: string; job: string; description: string; schedule: Start; trigger: Start; runs: RunSummary[] }[] }[] };
export type FeatureGroupDetail = FeatureGroup & {
  live: { project: string; dataset: string; id: string; full_name: string; rows: number | null; bytes: number | null;
          updated: string | null; previewable: boolean; types: Record<string, string> } | null;
  live_error: string | null; declared_not_live: string[]; live_not_declared: string[];
};
/** One widget's data: columns + rows for a data widget, else its lines, alerts or text. */
export type WidgetData = {
  type: WidgetSpec["type"]; window: string; columns?: string[]; rows?: (string | number | null)[][];
  lines?: { time: string; severity: string; text: string }[];
  alerts?: { policy: string; opened: string; name: string; severity: string }[]; text?: string;
};
/** nbformat 4, as much of it as the editor reads and writes. */
export type CellOutput = { output_type: "stream"; name: string; text: string | string[] }
  | { output_type: "display_data" | "execute_result"; data: Record<string, unknown>; metadata?: Record<string, unknown>; execution_count?: number | null }
  | { output_type: "error"; ename: string; evalue: string; traceback: string[] };
export type Cell = { cell_type: "code" | "markdown" | "raw"; source: string | string[]; metadata: { tags?: string[]; [k: string]: unknown };
  outputs?: CellOutput[]; execution_count?: number | null; id?: string };
export type Notebook = { nbformat: number; nbformat_minor: number; metadata: Record<string, unknown>; cells: Cell[] };
export type WorkspaceFolder = { path: string; home: string; folders: { name: string; path: string }[];
  notebooks: { name: string; path: string; updated: string; size: number; generation: string }[] };
export type RuntimeTemplate = { id: string; name: string; description: string; machine: string; accelerator: string;
  accelerator_count: number; disk_gb: number | string | null; disk_type: string; idle_shutdown: string | null; internet: boolean;
  image: string; env: Record<string, string>; startup_script: string; startup_script_url: string; startup_behavior: string;
  default: boolean; labels: Record<string, string>; created: string; updated: string; etag: string };
export type NotebookRuntime = { id: string; name: string; user: string; state: string; health: string; template: string;
  machine: string; proxy: string; created: string; updated: string; expires: string | null; idle?: string | null };
/** How a run stands now (console/runs.py's liveness): its record next to the runtime's and the kernel's own state. */
export type RunLive = "running" | "runner_gone" | "lost" | "kernel_dead" | "kernel_gone" | "runtime_stopped" | "stale"
  | "done" | "failed" | "cancelled";
export type RunCell = { index: number; id: string; state: "queued" | "running" | "ok" | "error" | "failed" | "cancelled" | string;
  started: number | null; ended: number | null; execution_count: number | null; outputs: CellOutput[] };
/** A run of a notebook's cells, by the editor (this page or another) or by Claude, with its heartbeat. */
export type NotebookRun = { run_id: string; by: "editor" | "claude" | string; user: string; runtime: string; kernel: string;
  state: string; error: string | null; started: number; ended: number | null; heartbeat: number; cells: RunCell[];
  live: RunLive; why: string | null; kernel_state?: string | null; kernel_activity?: string | null };
export type KernelStatus = { execution_state: string | null; last_activity?: string; connections?: number };
export type KernelInfo = { id: string; name: string; execution_state: string; last_activity: string };
export type Completions = { matches: string[]; cursor_start: number; cursor_end: number; types: Record<string, string> };
export type KernelVariable = { name: string; type: string; shape: string; value: string };
/** A message the relay streams back while a cell runs: an output, a heartbeat while it is silent, or the end. */
export type RunMessage = CellOutput | { output_type: "clear_output" } | { output_type: "heartbeat" }
  | { output_type: "done"; execution_count: number | null; status: string };

const send = <T,>(method: string, path: string, body?: unknown) =>
  call<T>(path, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
const q = (path: string) => `path=${encodeURIComponent(path)}`;

/** Runs a cell on the relay, calling `on` with each message as it arrives. `cell` names the notebook cell it
 *  is, so the run keeps a record any page opened later (or Claude) reads. */
export async function runCell(runtime: string, kernel: string, code: string, on: (m: RunMessage) => void,
                              cell?: { path: string; cell_id: string; index: number }): Promise<void> {
  const r = await fetch(`/api/notebooks/runtimes/${runtime}/kernels/${kernel}/execute`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, ...cell }) });
  if (!r.ok || !r.body) {
    const body = await r.json().catch(() => ({}));
    throw Object.assign(new Error(body.detail || `${r.status} ${r.statusText}`), { status: r.status });
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) on(JSON.parse(line));
    }
  }
}

/** What you may do with a notebook: its owner and admins manage (share) it. */
export type NotebookAccess = "view" | "edit" | "manage";
/** Who a Users/ notebook is shared with: everyone views it ("view") or only the people added ("private"). */
export type NotebookShare = { path: string; owner: string; general: "view" | "private"; people: Record<string, "view" | "edit"> };

export const notebooksApi = {
  folder: (path: string) => call<WorkspaceFolder>(`/api/workspace?${q(path)}`),
  read: (path: string) => call<{ path: string; generation: string; notebook: Notebook; access: NotebookAccess }>(`/api/workspace/notebook?${q(path)}`),
  share: (path: string) => call<NotebookShare>(`/api/workspace/notebook/share?${q(path)}`),
  setShare: (path: string, share: Pick<NotebookShare, "general" | "people">) =>
    send<NotebookShare>("PUT", `/api/workspace/notebook/share?${q(path)}`, share),
  generation: (path: string) => call<{ path: string; generation: string | null }>(`/api/workspace/notebook/generation?${q(path)}`),
  runs: (path: string) => call<{ runs: NotebookRun[]; now: number }>(`/api/workspace/notebook/runs?${q(path)}`),
  kernelStatus: (runtime: string, kernel: string) => call<KernelStatus>(`/api/notebooks/runtimes/${runtime}/kernels/${kernel}/status`),
  save: (path: string, notebook: Notebook, generation: string | null) =>
    send<{ path: string; generation: string }>("PUT", `/api/workspace/notebook?${q(path)}`, { notebook, generation }),
  create: (path: string) => send<{ path: string; generation: string }>("POST", "/api/workspace/notebooks", { path }),
  mkdir: (path: string) => send<{ path: string }>("POST", "/api/workspace/folders", { path }),
  remove: (path: string) => send<{ deleted: string }>("DELETE", `/api/workspace/notebook?${q(path)}`),
  templates: () => call<RuntimeTemplate[]>("/api/compute/templates"),
  runtimes: () => call<NotebookRuntime[]>("/api/compute/runtimes"),
  createTemplate: (spec: object) => send<{ name: string }>("POST", "/api/compute/templates", spec),
  updateTemplate: (id: string, spec: object) => send<{ template: string }>("PATCH", `/api/compute/templates/${id}`, spec),
  deleteTemplate: (id: string) => send<{ deleted: string }>("DELETE", `/api/compute/templates/${id}`),
  createRuntime: (template: string, name = "") => send<{ name: string }>("POST", "/api/compute/runtimes", { template, name }),
  runtimeAction: (id: string, action: "start" | "stop") => send<{ runtime: string }>("POST", `/api/compute/runtimes/${id}/${action}`),
  deleteRuntime: (id: string) => send<{ runtime: string }>("DELETE", `/api/compute/runtimes/${id}`),
  access: () => call<{ configured: boolean; allowed: boolean; user: string }>("/api/notebooks/auth"),
  allowUrl: (next: string) => `/api/notebooks/auth/start?next=${encodeURIComponent(next)}`,
  kernels: (runtime: string) => call<KernelInfo[]>(`/api/notebooks/runtimes/${runtime}/kernels`),
  startKernel: (runtime: string) => send<KernelInfo>("POST", `/api/notebooks/runtimes/${runtime}/kernels`),
  kernelAction: (runtime: string, kernel: string, action: "interrupt" | "restart" | "shutdown") =>
    send<unknown>("POST", `/api/notebooks/runtimes/${runtime}/kernels/${kernel}/${action}`),
  complete: (runtime: string, kernel: string, code: string, cursor_pos: number) =>
    send<Completions>("POST", `/api/notebooks/runtimes/${runtime}/kernels/${kernel}/complete`, { code, cursor_pos }),
  inspect: (runtime: string, kernel: string, code: string, cursor_pos: number) =>
    send<{ found: boolean; text: string }>("POST", `/api/notebooks/runtimes/${runtime}/kernels/${kernel}/inspect`, { code, cursor_pos }),
  variables: (runtime: string, kernel: string) =>
    call<KernelVariable[]>(`/api/notebooks/runtimes/${runtime}/kernels/${kernel}/variables`),
};

/** A blog post as the list shows it; `updated` and `created` are ISO times. */
export type PostSummary = { slug: string; title: string; author: string; tags: string[]; summary: string;
  created: string; updated: string };
export type Post = PostSummary & { body: string; generation: string; can_edit: boolean };

export const blogApi = {
  posts: () => call<PostSummary[]>("/api/blog/posts"),
  post: (slug: string) => call<Post>(`/api/blog/posts/${slug}`),
  create: (title: string) => send<{ slug: string; generation: string }>("POST", "/api/blog/posts", { title }),
  save: (slug: string, post: Pick<Post, "title" | "tags" | "summary" | "body" | "generation">) =>
    send<{ slug: string; generation: string }>("PUT", `/api/blog/posts/${slug}`, post),
  remove: (slug: string) => send<{ deleted: string }>("DELETE", `/api/blog/posts/${slug}`),
  upload: (slug: string, file: File) =>
    call<{ url: string }>(`/api/blog/posts/${slug}/images`, { method: "POST", headers: { "Content-Type": file.type }, body: file }),
};

export const api = {
  dashboards: () => call<DashboardSummary[]>("/api/dashboards"),
  dashboard: (bundle: string, target: string, key: string) => call<DashboardDetail>(`/api/dashboards/${bundle}/${target}/${key}`),
  dashboardWidget: (bundle: string, target: string, key: string, i: number, window: string, picked: Record<string, string> = {}) =>
    call<WidgetData>(`/api/dashboards/${bundle}/${target}/${key}/widgets/${i}?window=${encodeURIComponent(window)}${
      Object.entries(picked).filter(([, v]) => v).map(([k, v]) => `&f.${k}=${encodeURIComponent(v)}`).join("")}`),
  dashboardFilter: (bundle: string, target: string, key: string, name: string, window: string) =>
    call<string[]>(`/api/dashboards/${bundle}/${target}/${key}/filters/${name}?window=${encodeURIComponent(window)}`),
  bundles: () => call<BundleRow[]>("/api/bundles"),
  bundle: (name: string) => call<BundleDetail>(`/api/bundles/${name}`),
  featureGroup: (name: string) => call<FeatureGroupDetail>(`/api/features/${name}`),
  batchTasks: () => call<ServeTask[]>("/api/batch-serving"),
  batchTask: (wf: string, task: string) => call<ServeTaskDetail>(`/api/batch-serving/${wf}/${encodeURIComponent(task)}`),
  datasets: () => call<Dataset[]>("/api/catalog"),
  dataset: (ds: string) => call<DatasetDetail>(`/api/catalog/${ds}`),
  table: (ds: string, t: string, project?: string) => call<TableDetail>(`/api/catalog/${ds}/${t}${inProject(project)}`),
  preview: (ds: string, t: string, project?: string) => call<Sample>(`/api/catalog/${ds}/${t}/preview${inProject(project)}`),
  queryPreview: (ds: string, t: string, project?: string) => post<Sample>(`/api/catalog/${ds}/${t}/query${inProject(project)}`),
  lineage: (node: string) => call<Lineage>(`/api/lineage?node=${encodeURIComponent(node)}`),
  versionLineage: (id: string, version: string) => call<VersionLineage>(`/api/models/${id}/versions/${version}/lineage`),
  versionFeatures: (id: string, version: string) => call<VersionFeatures>(`/api/models/${id}/versions/${version}/features`),
  versionTraining: (id: string, version: string) => call<VersionTraining>(`/api/models/${id}/versions/${version}/training`),
  experiments: () => call<Experiment[]>("/api/experiments"),
  experiment: (id: string) => call<Experiment & { runs: ExpRun[] }>(`/api/experiments/${id}`),
  models: () => call<ModelSummary[]>("/api/models"),
  model: (id: string) => call<Model>(`/api/models/${id}`),
  costs: (days: number) => call<CostReport>(`/api/costs?days=${days}`),
  setAlias: (id: string, alias: string, version: string | null) =>
    post<{ alias: string; version: string | null; from: string[] }>(`/api/models/${id}/aliases`, { alias, version }),
  endpoints: () => call<Endpoint[]>("/api/serving"),
  endpoint: (id: string) => call<Endpoint>(`/api/serving/${id}`),
  features: () => call<FeatureGroup[]>("/api/features"),
  me: () => call<{ email: string; project: string; region: string; admin: boolean; workspace: string; platform_git: string }>("/api/me"),
  deleteExperiment: (id: string) => call<{ deleted: string }>(`/api/experiments/${id}`, { method: "DELETE" }),
  deleteModel: (id: string) => call<{ deleted: string }>(`/api/models/${id}`, { method: "DELETE" }),
  deleteEndpoint: (id: string) => call<{ deleted: string }>(`/api/serving/${id}`, { method: "DELETE" }),
  jobs: () => call<Job[]>("/api/jobs"),
  deleteJob: (wf: string) => call<{ deleted: string }>(`/api/jobs/${wf}`, { method: "DELETE" }),
  job: (wf: string) => call<JobDetail>(`/api/jobs/${wf}`),
  run: (wf: string, id: string, repairId?: number | null) =>
    call<RunDetail>(`/api/jobs/${wf}/runs/${id}${repairId != null ? `?repair_id=${repairId}` : ""}`),
  taskRun: (wf: string, id: string, task: string) =>
    call<TaskRunDetail>(`/api/jobs/${wf}/runs/${id}/tasks/${encodeURIComponent(task)}`),
  taskNotebook: (wf: string, id: string, task: string) =>
    call<{ run: { name: string; id: string; state: string; start: string | null; end: string | null; error: string; link: string } | null;
               notebook: Notebook | null }>(`/api/jobs/${wf}/runs/${id}/tasks/${encodeURIComponent(task)}/notebook`),
  taskPipeline: (wf: string, id: string, task: string) =>
    call<PipelineRun>(`/api/jobs/${wf}/runs/${id}/tasks/${encodeURIComponent(task)}/pipeline`),
  logs: (wf: string, id: string, task: string) =>
    call<LogLine[]>(`/api/jobs/${wf}/runs/${id}/tasks/${encodeURIComponent(task)}/logs`),
  pipelineDefinition: (wf: string, task: string) =>
    call<PipelineDefinition>(`/api/jobs/${wf}/tasks/${encodeURIComponent(task)}/pipeline`),
  pipelineStep: (wf: string, id: string, task: string, step: string) =>
    call<StepRun>(`/api/jobs/${wf}/runs/${id}/tasks/${encodeURIComponent(task)}/pipeline/steps/${encodeURIComponent(step)}`),
  pipelineArtifact: (wf: string, id: string, task: string, step: string, name: string) =>
    call<{ name: string; type: string; uri: string; text: string; truncated: boolean }>(
      `/api/jobs/${wf}/runs/${id}/tasks/${encodeURIComponent(task)}/pipeline/steps/${encodeURIComponent(step)}/artifacts/${encodeURIComponent(name)}`),
  taskStats: (wf: string, id: string, task: string) =>
    call<TaskStats>(`/api/jobs/${wf}/runs/${id}/tasks/${encodeURIComponent(task)}/stats`),
  bigqueryJob: (project: string, location: string, jobId: string) =>
    call<BqProfile>(`/api/bigquery/jobs/${project}/${location}/${jobId}`),
  runNow: (wf: string, params: Record<string, string>, only: string[] = []) =>
    post<{ execution: string }>(`/api/jobs/${wf}/run`, { params, only }),
  /** A failed run's batch_serve_tasks' models: a FAILED one resumes; any can run again in full. */
  repairPlan: (wf: string, id: string) =>
    call<{ models: Record<string, { model: string; status: string }[]>; parameters: Record<string, string>;
           /** Each vertex_pipeline_task's pipeline run, its steps as they ran: a repair reruns it in Vertex. */
           pipelines: Record<string, { pipeline_job: string; steps: { name: string; state: string }[] }>;
           original: Record<string, string>; overrides: Record<string, string>; latest_repair_id: number;
           /** A run of some tasks: those tasks ([]: all); a repair leaves the rest out again. */
           only: string[]; execution: string }>(
      `/api/jobs/${wf}/runs/${id}/repair`),
  /** `tasks`: ones that succeeded, to run again too; `keep`: ones that didn't, not to run again;
   *  `models`: batch_serve models to run again in full;
   *  `params`: overrides later repairs keep (null drops one); `latest_repair_id`: the attempt repaired. */
  repair: (wf: string, id: string, body: { models?: string[]; tasks?: string[]; keep?: string[]; params?: Record<string, string | null>;
                                            latest_repair_id?: number; steps?: Record<string, string[]>; fresh?: string[] }) =>
    post<{ run_id: string; repair_id: number; execution: string; reused: string[];
           pipelines: Record<string, { pipeline_job: string; rerun: string[] }> }>(`/api/jobs/${wf}/runs/${id}/repair`, body),
  why: (wf: string, id: string, task?: string) =>
    call<WhyFailed>(`/api/jobs/${wf}/runs/${id}/why${task ? `?task=${encodeURIComponent(task)}` : ""}`),
  cancel: (wf: string, id: string) =>
    post<{ cancelled: string; containers: string[] }>(`/api/jobs/${wf}/runs/${id}/cancel`),
  pause: (wf: string) => post<{ paused: boolean }>(`/api/jobs/${wf}/pause`),
  resume: (wf: string) => post<{ paused: boolean }>(`/api/jobs/${wf}/resume`),
};
