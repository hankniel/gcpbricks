# Reference

The full reference for gcpbricks: every job kind and resource, the feature store SDK, the
console, the CI flow and the known problems. New here? Start with the
[README](../README.md) and [Getting started](getting-started.md). The design is in
[Idea](idea.md) and [Architecture](architecture.md).

Notes on wording: "the platform" is this repo; "a project" is a repo that carries an
`mlops.yaml`; `demo_model` and `Demo App` are example projects.

## Layout

| Path | What it is |
|---|---|
| `src/gcpbricks/bundle.py` | `mlops.yaml`'s schema and loader (includes, targets, `${...}`); `job_argument()`, the one definition of what a job's run receives; `render()`, the terraform module's input |
| `src/gcpbricks/cli.py` | `mlops bundle validate / summary / render / deploy / run / runs / repair / redeploy` |
| `src/gcpbricks/files.py` | The bundle's files as `deploy` uploads them and a `dbt_task` reads them |
| `src/gcpbricks/task_runner.py` | `mlops-task dbt`: what a `dbt_task`'s Cloud Run job runs, keeping each command's run_results for the console |
| `src/gcpbricks/job_labels.py` | `bigquery_client()`: a BigQuery client whose jobs carry `mlops_exec`, the execution that ran them |
| `src/gcpbricks/ml.py` | `experiments`, `registered_models`, `model_serving_endpoints`: their schema and how deploy applies them on Vertex AI |
| `src/gcpbricks/mlflow/` | MLflow's tracking and registry API over Vertex AI Experiments and Model Registry (`[ml]` extra) |
| `src/gcpbricks/tuning.py` | `tuning.sweep()`: an Optuna hyperparameter sweep, each trial a child run of the sweep's run (`[tuning]` extra) |
| `src/gcpbricks/pipelines.py` | A `vertex_pipeline_task`'s `python_file`: compiled at deploy, its steps' bundle env, `use_bundle` |
| `src/gcpbricks/environments.py` | Job environments: their build context, content tag and Cloud Build build on the platform runtime |
| `src/gcpbricks/publish.py` | Uploads a build's `<name>_pipeline.json` + `<name>_params.json` under a version, then `current.json` |
| `src/gcpbricks/targets.py` | Target namespacing: prod at the root, other targets under their name |
| `src/gcpbricks/content_hash.py` | AST hash of `.py` files, for content-addressed tags and retrain keys |
| `src/gcpbricks/import_graph.py` | Files a set of entrypoints can import, read with `ast` |
| `src/gcpbricks/batch_serve.py` | A `batch_serve_task` model job's building blocks: its window, `score_batch`, `write` (`write_window`, append, insert_new, merge), `scored_keys` |
| `src/gcpbricks/features.py` | The feature store SDK (`[features]` extra): Tecton's and Databricks Feature Engineering's workflow over dbt-features's feature groups as feature views, on its own Ibis-compiled query engine for BigQuery, Spark and DuckDB |
| `src/gcpbricks/feature_config.py` | `mlops-features render`: the feature groups dbt-features declares (`meta.feature_store`), as the JSON the SDK reads |
| `src/gcpbricks/feature_manager/` | Feature engineering declared in a model's `features.yaml` (`[feature-manager]` extra): compiled into a fitted sklearn transformer the model pickles with its estimator |
| `terraform/modules/bundle` | Per target: one Workflow per job, one Cloud Run job per job cluster (plus the dbt job), the runner Workflows `vertex_pipeline_task` / `batch_serve_task` call, one scheduler per scheduled job, log sinks and run-failed alerts |
| `terraform/modules/bundle/workflows/` | `job.yaml` (run a job's tasks), `vertex_pipeline.yaml` (run the last published pipeline), `batch_serve.yaml` (run each model over its window), `trigger.yaml` (check table_update / file_arrival triggers), `wake.yaml` (check a file_arrival trigger on a GCS event), `watchdog.yaml` (warn on long runs) |
| `images/runtime/` | The platform runtime every job environment builds on: Python, libgomp1, this package with `[ml,features]`, pushed on every tag |
| `images/dbt/` | The platform's dbt image: this package + dbt v2 (adapters bundled), pushed on every tag |
| `templates/project.yml` | GitLab CI/CD component a project includes: validate, render, deploy on main |
| `e2e/` | The release gate: the `mlops-gate` bundle a release deploys and runs on dev before anyone gets it |
| `.gitlab-ci.yml` | This repo's own CI: test and terraform validate on every MR and main push; on a `vX.Y.Z` tag, publish the package and the dbt image and create the GitLab Release |
| `tests/fixtures/bundle/` | A bundle as a project writes it (Demo App's nightly job), and a terraform root CI validates the module through |

## A bundle

```yaml
# mlops.yaml
bundle: {name: demo-app, mlops_version: "0.4.0"}
include: [resources/*.yml]
gcp: {project: my-gcp-project, region: us-central1, bucket: my-ml-bucket}
run_as: {service_principal_name: bundle-runner@my-gcp-project.iam.gserviceaccount.com}
variables:
  image: {default: us-central1-docker.pkg.dev/.../model-serving:latest}
  schema: {default: demo_app_dev}
targets:
  dev: {mode: development, default: true}
  prod: {mode: production, variables: {schema: demo_app}}

# resources/nightly.yml
resources:
  jobs:
    nightly:
      parameters: [{name: run_date, default: "{{job.start_time.iso_date}}"}]
      schedule: {quartz_cron_expression: "0 0 2 * * ?", timezone_id: UTC}
      job_clusters:
        - job_cluster_key: main
          new_cluster: {docker_image: {url: "${var.image}"}, node_type_id: e2-standard-4}
      tasks:
        - task_key: build_tables
          dbt_task: {project_directory: dbt, schema: "${var.schema}",
                     commands: ["dbt build -s tag:demo_app"]}
        - task_key: score
          depends_on: [{task_key: build_tables}]
          job_cluster_key: main
          max_retries: 1
          python_wheel_task: {package_name: model_inference, entry_point: nightly,
                              parameters: ["--date", "{{job.parameters.run_date}}"]}
```

The keys are Databricks'. A Databricks key the platform does not run (`existing_cluster_id`,
`continuous`, `spark_version`, `sql_task`, ...) fails `mlops bundle validate`
with "not supported by gcpbricks"; nothing is silently ignored. The only keys
Databricks doesn't have are `bundle.mlops_version`, `gcp`, `new_cluster`'s `cpu` and `memory`,
`notebook_task.runtime_template`, and the task types `vertex_pipeline_task` and `batch_serve_task`.

**Bundle settings.** `include` globs are merged into the main file. A target overrides any
setting; it merges `tasks` by `task_key`, `job_clusters` by `job_cluster_key` and
`parameters` by `name`, as Databricks does. A job declared only under one target's
`resources` exists only there. Variables resolve, lowest first, from `default`, the target's
`variables`, `BUNDLE_VAR_<name>`, then `--var name=value`; `${var.x}`, `${bundle.name}`,
`${bundle.target}` and `${resources.jobs.<name>.id}` substitute anywhere. `mode:
development` pauses schedules unless a schedule says `pause_status: UNPAUSED`.

**Compute.** `run_as.service_principal_name` is the service account everything runs as. A job
cluster becomes a Cloud Run job named `<bundle>-<job>-<cluster>[-<target>]` running
`docker_image.url`, or the image of the job environment its `environment_key` names (below); `node_type_id` is a GCP machine type whose vCPUs and memory size it
(standard, highcpu and highmem types that fit Cloud Run's 1/2/4/8 vCPU, up to 32 GiB), or,
a GCP extension for an exact size, `cpu` (1, 2, 4, 6 or 8) with `memory` (`"24Gi"`, `"512Mi"`;
at most 4 GiB per vCPU) instead; `spark_env_vars` become its environment. The container's args replace the image's `CMD`, so
its `ENTRYPOINT` must be empty or exec its arguments.

| Task type | Runs | Notes |
|---|---|---|
| `python_wheel_task` | the job cluster's Cloud Run job | the installed distribution's entry point by name, with `parameters` or `named_parameters` (`--k=v`) |
| `spark_python_task` | the job cluster's Cloud Run job | A relative `python_file` is a file of the bundle, as in Databricks: `mlops-task python` fetches the files its deploy uploaded and runs it from the bundle's root, the root on `PYTHONPATH` (`MLOPS_BUNDLE_VERSION` in its env), so the image carries dependencies and `gcpbricks`, not the code. An absolute path is a file in the image. No Spark yet |
| `dbt_task` | the platform's dbt job `<bundle>-dbt[-<target>]`, or the job cluster's if set (its image then needs `gcpbricks` and dbt) | fetches the files its deploy uploaded, runs `dbt deps` when the project has packages, then each command. Without `profiles_directory` it writes a BigQuery profile: `catalog` (default `gcp.project`) and `schema` (the dataset), authenticated as the job's service account. `state_location` (a GCP extension, `gs://...`, e.g. `.../{{job.run_id}}`) keeps each command's `run_results.json` and `manifest.json` there and restores them into `target/` first, so a task with `run_if: AT_LEAST_ONE_FAILED` and `commands: [dbt retry]` over the same location re-runs only the nodes that failed |
| `condition_task` | the Workflow | `op` over `left`/`right`: numbers compare as numbers, anything else only by `EQUAL_TO`/`NOT_EQUAL`. A dependent picks a branch with `depends_on: [{task_key, outcome: "true"}]` |
| `run_job_task` | the other job's Workflow, waiting for it | `job_id: ${resources.jobs.other.id}` (this bundle) or a `lookup` variable (another bundle's job), `job_parameters` over its parameters |
| `for_each_task` | the Workflow | `task` once per element of `inputs` (a JSON array or a `{{...}}` resolving to one), `concurrency` at a time (at most 20), `{{input}}` in its fields; fails after all ran if any failed |
| `vertex_pipeline_task` | the `vertex_pipeline` runner Workflow | runs the last published pipeline: deploy compiles `python_file`'s `function` (a `@dsl.pipeline`, below) with `base_parameters`, or runs `build` and publishes what it writes under `spec_prefix`. `parameters` override pipeline parameters per run |
| `batch_serve_task` | the `batch_serve` runner Workflow | below |
| `notebook_task` | the `notebook` runner Workflow, on Colab Enterprise's notebook execution | `notebook_path`: an `.ipynb` of the bundle (deploy uploads it) or a console workspace notebook under `/Workspace/`. `base_parameters` (`{{...}}` allowed) become a cell of Python assignments after the cell tagged `parameters` (papermill's convention), else first. It runs as `run_as` on the machine of `runtime_template`, a Colab runtime template's display name (default `Default`), so scheduled notebooks get the machine the team's interactive ones use; a job cluster doesn't apply. The executed notebook, outputs included, is the task run's output in the console |

**A run.** Tasks run in levels: a level starts when the previous one is done, its tasks in
parallel, so a task may wait on an unrelated one in its predecessor's level. A task runs when
its `run_if` holds over its dependencies (`ALL_SUCCESS` by default, or
`AT_LEAST_ONE_SUCCESS`, `NONE_FAILED`, `ALL_DONE`, `AT_LEAST_ONE_FAILED`, `ALL_FAILED`);
otherwise it ends `UPSTREAM_FAILED` when a dependency failed, else `EXCLUDED`. `max_retries`
and `min_retry_interval_millis` retry it; `timeout_seconds` bounds it (24 h at most). The job
fails if any task `FAILED`. A run that finds more than `max_concurrent_runs` (default 1)
executions of its job active is skipped; job `timeout_seconds` starts no level after that
long.

**Values.** `{{job.id}}`, `{{job.name}}` (the job's `name`, else its key), `{{job.run_id}}` (the run: its first execution, kept by its repairs), `{{job.repair_count}}` (0, or which repair this is),
`{{job.start_time.iso_date}}`, `{{job.start_time.iso_datetime}}`,
`{{job.start_time.timestamp_ms}}` (UTC, as in Databricks), `{{job.parameters.<name>}}`,
`{{task.name}}`, and `{{input}}` inside a `for_each_task`. A job with a `table_update`
trigger may also use what fired its run, as Databricks names it:
`{{job.trigger.table_update.updated_tables}}` (a JSON list) and, per watched table,
`{{job.trigger.table_update.<table>.commit_timestamp.iso_datetime}}`, `.commit_timestamp.timestamp_ms`
and `.version` (one table: without `<table>.`). A run started any other way gets `[]` and
empty values. Anything else, task values included, fails validation.

**Starting, queueing, watching.**
- `schedule` (Quartz) or `trigger`, not both. `trigger.periodic` becomes the job's cron on the
  clock in UTC: every n minutes (n divides 60), n hours (n divides 24), daily or weekly.
  `trigger.table_update` (up to 10 tables, `ANY_UPDATED` / `ALL_UPDATED`) and
  `trigger.file_arrival` (`gs://bucket/prefix/`) are checked every 5 minutes by the bundle's
  trigger Workflow, honouring `wait_after_last_change_seconds` and
  `min_time_between_triggers_seconds`; the first check records what it sees and starts
  nothing. A BigQuery table (`project.dataset.table`) changes when its `lastModifiedTime`
  does; it has no version. A lakehouse Iceberg table in a BigLake catalog
  (`project.catalog.namespace.table`) changes when its current snapshot does: one commit
  each, so a half-written table never fires, and `.version` is the snapshot id. The run_as
  account needs `biglake.tables.get` on such a table. A table written in many commits
  (dbt microbatch) needs a `wait_after_last_change_seconds` longer than the gap between
  them, as in Databricks. To read exactly what fired the run, pin to it, e.g.
  `FOR SYSTEM_TIME AS OF TIMESTAMP_MILLIS({{job.trigger.table_update.commit_timestamp.timestamp_ms}})`
  on BigQuery, or the snapshot id in Spark. A file_arrival
  trigger is also checked at once: an Eventarc trigger on the bucket starts the job's wake
  Workflow on each object written, which ignores objects outside the prefix. Eventarc
  filters by bucket only, so give a watched prefix its own landing bucket when the bucket is
  busy. Checks write the trigger's state under a GCS generation lock, so two never both start
  a run; with `wait_after_last_change_seconds`, the poll starts the run once the files settle.
- Runs take turns in start order. Over `max_concurrent_runs` a run is skipped, or with
  `queue: {enabled: true}` waits (up to 48 hours).
- `health.rules` (`RUN_DURATION_SECONDS` / `GREATER_THAN`) start a watchdog beside each run,
  which logs a duration warning if the run is still going.
- `email_notifications` and `webhook_notifications` on the job (`on_start`, `on_success`,
  `on_failure`, `on_duration_warning_threshold_exceeded`) and on tasks (`on_start`,
  `on_success`, `on_failure`) become log-based Cloud Monitoring alerts on the runs'
  `mlops_run` / `mlops_task` entries. An email gets its own notification channel; a
  webhook `id` is a Cloud Monitoring notification channel (`projects/<p>/notificationChannels/<id>`
  or the bare id). A skipped run counts as a failure unless
  `notification_settings.no_alert_for_skipped_runs`. Alerts notify at most once per 5 minutes.
- A failed run's email is the platform's own, laid out as Databricks' (the run's status,
  details, error and tasks, and View run into the console), sent once per failed run by the
  project's `mlops-notify` Cloud Run job (`gcpbricks.notify`, deployed with the
  console from `deploy/console/dev`), to the job's `email_notifications.on_failure`, else
  `admin@example.com` (`bundle.DEFAULT_FAILURE_EMAILS`). Cloud Monitoring then emails only
  a skipped run. `gcp.mail: false` goes back to Cloud Monitoring's email. Its SMTP is this
  repo's masked CI variables `SMTP_USER` and `SMTP_PASSWORD` (a Gmail app password, as
  dbt-features's), applied as the job's env. A bundle's `run_as` needs `roles/run.developer` on
  `mlops-notify`. A mail that fails to start is logged (`mlops_mail`) and never fails the run.
- The console (`deploy/console/dev`) opens to the project's `roles/iap.httpsResourceAccessor`
  holders and its `iap_members`. Reading is the console's own account; a run, repair, cancel,
  pause, resume, alias change or delete goes to GCP with the user's own token ("Allow GCP
  access", the OAuth client in this repo's CI variables `NOTEBOOK_OAUTH_CLIENT_ID` /
  `NOTEBOOK_OAUTH_CLIENT_SECRET`), so their own roles decide it: starting or repairing a run needs
  `workflows.executions.create` on its Workflow (`roles/workflows.invoker`), and its tasks run
  as the job's `run_as` whoever started it. Deleting is for `MLOPS_CONSOLE_ADMINS` too; an
  admin's changes go as the console's own account, with no sign-in of their own.
- A task run's Metrics in the console, as Databricks' Spark UI shows a run: each Cloud Run
  execution or pipeline step's CPU, memory and network per minute, against its limits; vCPU-
  and GiB-seconds and their cost at list price; its BigQuery jobs' bytes billed, slot time and
  cost ($6.25/TiB on-demand); a `dbt_task`'s per node (every job whose dbt query comment
  names it, not only the last one run_results records), each opening its query profile (stage
  timings, average against slowest worker, shuffle and spill); and what stands out. A
  BigQuery job counts when it carries a label naming the run: dbt's `dbt_invocation_id`, or
  `mlops_exec` from the platform's own clients (`batch_serve`, `features`) and a project's
  `job_labels.bigquery_client()`.
- A `vertex_pipeline_task`'s pipeline in the console: on the job's Tasks tab (and on a task run
  that started none), the pipeline deploy last published, with its parameters and each step's
  component: what feeds each input, its outputs, image, machine, cache and source. In a run,
  each step's actual input and output values, its artifacts (a Markdown or HTML one opens in
  place, a Metrics one lists its values), whether it came from cache, its custom job's logs,
  and its CPU, memory and network.
- `tags` label the job's Workflow and Cloud Run jobs, so they follow GCP's label rules.
- `name` is the job's display name, as on Databricks: the console, its failure email and
  `{{job.name}}` show it. The key stays its identity (its Workflow `<bundle>-<key>`), so
  renaming a job is changing `name`: its runs, schedule and history stay.
- A job the bundle no longer declares (a removed or renamed key) is retired, not deleted:
  the next deploy keeps its Workflow, its runs and what they use, labelled
  `mlops_retired=<date>`, with no schedule, trigger or alert, so a run in flight finishes and
  its history stays readable. The console hides it until "Show retired" is on, won't start
  it, and lets an admin delete it once no run is going. Declaring the key again brings it
  back. A job removed by a deploy older than this keeps nothing to retire.
- A task's `timeout_seconds` fails it outright unless `retry_on_timeout`.

A job's definition (its tasks and settings) is uploaded to
`<target prefix>.bundle/<bundle>/jobs/<job>.json` and read when a run starts; the run's
argument is only a pointer to it, under Workflows' 32 KB limit. It names the files version
the deploy uploaded (`MLOPS_FILES_VERSION` in each task's container), so a run reads the
files that match its own definition even if another deploy lands while it runs.

**Pipelines in the bundle.** A `vertex_pipeline_task`'s `python_file` is a KFP pipeline the
platform compiles at deploy (`gcpbricks[pipelines]`, kfp 2), its steps running the
task's environment and importing the project's code from the deployed files:

```python
# training/pipeline.py
from kfp import dsl
from gcpbricks import pipelines

@dsl.component(base_image=pipelines.image())        # the task's environment image
def train(model: str) -> str:
    from training.train import fit                   # bundle code, on sys.path
    return fit(model)

@dsl.pipeline(name="train")
def pipeline(model: str):
    train(model=model)
```

```yaml
tasks:
  - task_key: roas_d30
    environment_key: training
    vertex_pipeline_task:
      python_file: training/pipeline.py              # function: pipeline (the default)
      base_parameters: {model: roas_d30}
```

Deploy sets `MLOPS_BUNDLE_FILES`, `MLOPS_BUNDLE_VERSION` and the MLflow env on every step
container of the environment's image; the runtime image's `sitecustomize` then puts exactly
that deploy's files on `sys.path` as Python starts. A step that is not such a container (a
CustomJob's worker pool) takes `pipelines.step_env()` into its own env. The run's display
name, labels, `pipeline_root` (`gs://<bucket>/<target prefix>.bundle/<bundle>/pipeline_root/`)
and service account (`run_as`) are the platform's; the spec lives under
`.bundle/<bundle>/pipelines/<job>-<task_key>` unless `spec_prefix` says otherwise.

**Environments.** As in Databricks, a job declares what its code needs, and the platform
builds it, so a project keeps no Dockerfile:

```yaml
resources:
  jobs:
    serve:
      environments:
        - environment_key: serving
          spec:
            dependencies: ["-r requirements/serving.txt"]   # pip lines; -r / -c files in the bundle
      job_clusters:
        - job_cluster_key: big
          new_cluster: {environment_key: serving, cpu: 8, memory: 32Gi}   # instead of docker_image
      tasks:
        - task_key: train
          environment_key: training                          # a vertex_pipeline_task: its build's {image}
          vertex_pipeline_task: {...}
```

An environment is built `FROM` the platform's `runtime:<mlops_version>` image (Python 3.11,
libgomp1, `gcpbricks[ml,features]`) with its dependencies pip-installed, as
`<gcp.image_repository>/<bundle>-<key>:<tag>`; the repository defaults to
`<region>-docker.pkg.dev/<project>/mlops`. The tag hashes the dependency lines, every file they
name (`-r` / `-c`, followed) and the runtime, so `mlops bundle deploy` builds it with Cloud Build
only when what it installs changed, before anything that runs it is applied. A requirements
file with hashes turns on pip's hash checking for the whole environment, so lock every line
or none. Other tasks take their environment from their job cluster (serverless tasks, with
no cluster, are not supported yet).

## ML resources

```yaml
resources:
  experiments:
    roas: {name: roas-d30}                       # Vertex AI Experiment roas-d30[-<target>]
  registered_models:
    roas: {name: roas_d30, comment: ROAS at day 30}   # Vertex model roas_d30[_<target>]
  model_serving_endpoints:
    roas:
      name: roas-online                          # Vertex AI Endpoint roas-online[-<target>]
      config:
        served_entities:
          - {name: main, entity_name: "${resources.registered_models.roas.name}", entity_version: "3"}
          - {name: canary, entity_name: "${resources.registered_models.roas.name}", entity_version: "4",
             workload_size: Medium, workload_type: GPU_SMALL}
        traffic_config:
          routes: [{served_entity_name: main, traffic_percentage: 90},
                   {served_entity_name: canary, traffic_percentage: 10}]
      tags: [{key: team, value: ua}]
```

`mlops bundle deploy` applies them through the Vertex SDK (`gcpbricks[ml]`). Names
follow the target as demo_model already names them, and `${resources.<kind>.<key>.name}`
resolves to the target's name anywhere in the bundle, e.g. to hand a task its experiment.

- **experiments** are created if missing.
- **registered_models**: Vertex has no empty model; the first upload creates it. Deploy sets
  `comment` as its description and moves each declared alias onto its version. Leave
  aliases a pipeline promotes (`@champion`) out of the bundle. `catalog_name`, when set, is
  the GCP project.
- **model_serving_endpoints**: each served entity is one deployed model of a registered
  version. A new or changed one is deployed with no traffic, then traffic moves to the
  routes, then deployed models no entity names are undeployed. Sizes:

  | `workload_type` | Machine | `workload_size` | Replicas |
  |---|---|---|---|
  | `CPU` (default) | n1-standard-4 | `Small` (default) | 1 |
  | `GPU_SMALL` | n1-standard-8 + 1× T4 | `Medium` | 1–4 |
  | `GPU_MEDIUM` | g2-standard-12 + 1× L4 | `Large` | 2–16 |
  | `GPU_LARGE` | a2-highgpu-1g + 1× A100 | | |

  Vertex keeps at least one replica, so `scale_to_zero_enabled: true` is refused. A model
  served from an endpoint needs a real serving container: demo_model registers with a
  placeholder, so its models can't be served as they are.
- Deploy never deletes. An experiment, model or endpoint the bundle stops declaring (one the
  target's last `deployment.json` named, or an endpoint with the bundle's labels) is retired:
  labelled `mlops_retired=<date>`, and a retired endpoint's models are undeployed, so it
  stops serving and billing. Declaring it again lifts the label. The console hides retired
  resources until "Show retired" is on, and lets an admin (`MLOPS_CONSOLE_ADMINS`) delete one.

## MLflow on Vertex: `gcpbricks.mlflow`

The MLflow tracking and registry API Databricks code uses, with Vertex underneath, so a
project's training and scoring code reads as it would on Databricks (`gcpbricks[ml]`):

```python
from gcpbricks import mlflow

mlflow.set_experiment("roas-d30")
with mlflow.start_run(run_name=model_id):
    mlflow.log_params(params)
    mlflow.log_metrics(metrics)
    info = mlflow.pyfunc.log_model("model", python_model=RoasModel(),
                                   artifacts={"estimator": "model.pkl"},
                                   registered_model_name="roas_d30", tags={"model_id": model_id})

client = mlflow.MlflowClient()
client.set_registered_model_alias("roas_d30", "champion", info.registered_model_version)
model = mlflow.pyfunc.load_model("models:/roas_d30@champion")   # or models:/roas_d30/3
model.predict(df)
```

`log_model(..., signature=mlflow.models.infer_signature(train_df[inputs]), feature_spec=ts.spec)`
saves the inputs in MLflow's own `MLmodel` `signature` and the training set's `FeatureSpec` as
`feature_spec.json` beside it. On load, `model.metadata.get_input_schema().input_names()` and
`model.metadata.feature_spec` read them back (`None` for a model saved without), so a scorer
fetches what that version was trained on, not what today's code declares; `predict()` fails up
front naming a missing required input. Unlike MLflow, extra columns pass through and types are
not cast.

**What a model reads and is fit on.** `log_model` also saves `features.json` beside the model,
from whatever it is given: a feature_manager pipeline (`features=pipe`: the columns its read
fetches, the features it computes, and each `inputs` set or submodel's columns), a
`feature_spec` (what the feature store serves) and a `signature` (what the estimator is fit on).
The counts go in the run's params too (`n_features_read`, `n_model_inputs`,
`n_features_computed`), so an experiment's runs compare on them. A model logged without
`pyfunc.log_model` (a torch checkpoint, any flavor) calls
`mlflow.log_features(read=[...], model_inputs=[...] or {"set": [...]}, computed=[...])` in its run.
The console's model version page lists them, counted, with the columns read but not fit on and
the ones fit on but computed; a version without `features.json` shows what its signature and
`feature_spec.json` say.

**What a run trained on.** `mlflow.training_summary(train=..., valid=..., test=..., label=...,
time_column=..., model=..., snapshot=..., query=...)` summarizes, in the training process from
the frames already in memory (no extra infrastructure): each split's rows and time range and
where it came from (a snapshot, a query's hash); each feature's null rate and distribution
(numbers' quantiles, categories' top values; a frame over 200,000 rows is sampled), the
baseline a drift check compares serving to; the label's kind and per-split positive rate or
distribution; and the fitted model's importances (sklearn, LightGBM, XGBoost, linear models).
`log_model(..., training=summary)` saves it beside the model as `training_summary.json` and
logs `n_<split>_rows` and `label_positive_rate`; `mlflow.log_training(summary)` does the same
for a model logged by hand. It records the Cloud Run execution or Vertex AI custom job the run
ran in, and the console prices that, not the training machine: the version page shows what
training cost and how long it took (and a pipeline step's whole run), with the data, label,
importances and every feature's statistics.

Model validation is MLflow 3's, for `model_type="regressor"`:

```python
from gcpbricks.mlflow.models import MetricThreshold, make_metric

hits = make_metric(eval_fn=lambda predictions, targets, cost: ..., greater_is_better=True, name="hits")
candidate = mlflow.evaluate(data=df, predictions="pred", targets="y", model_type="regressor",
                            extra_metrics=[hits])
baseline = mlflow.evaluate(model="models:/roas_d30@champion", data=df_without_pred, targets="y",
                           predictions="predicted_roas_d30", model_type="regressor")
mlflow.validate_evaluation_results({"hits": MetricThreshold(threshold=70, greater_is_better=True)},
                                   candidate, baseline)   # raises ModelValidationFailedException
```

Metric names, formulas, `eval_fn` argument lookup (data's columns, `evaluator_config`, other
metrics), `MetricThreshold`'s rules (`min_absolute_change` must be positive) and failure
messages are MLflow's, checked against real MLflow in `tests/test_mlflow.py`. Metrics log to the
active run. No `eval_results_table` artifact is written; `evaluators`, `custom_artifacts` and
`sample_weights` are not supported.

| MLflow | Vertex |
|---|---|
| experiment, run, params, metrics | Vertex AI Experiments; a run's tags are params named `tag_<key>` |
| nested run (`start_run(nested=True)`) | a run of the same experiment tagged `mlflow.parentRunId`; the console lists it under its parent |
| registered model, version, alias | a Vertex model (by display name), its version, its version alias |
| version tags | the version's labels: lowercase `[a-z0-9_-]`, at most 63 characters, or refused |
| model artifacts | `MLFLOW_ARTIFACT_ROOT/<experiment>/<run_id>/artifacts/<path>`, MLflow's `MLmodel` layout |
| `evaluate`, `validate_evaluation_results` | computed in process; metrics to the active Vertex run |

Also there: `MlflowClient.get_model_version`, `get_model_version_by_alias`,
`delete_registered_model_alias`, `set_model_version_tag`, `delete_model_version_tag`,
`search_model_versions("name='m' and tags.k='v'")`, `get_registered_model`,
`create_model_version`, and `mlflow.register_model("runs:/<run>/<path>", name)`. A version
records its run (`mlflow_run_id`) and commit (`git_commit`, from `CI_COMMIT_SHA` or git).
Every job cluster's Cloud Run job gets `MLFLOW_TRACKING_URI=vertex://<project>/<region>` and
`MLFLOW_ARTIFACT_ROOT=gs://<bucket>/<target prefix>mlflow`, as a Databricks job finds its
MLflow configured; elsewhere, set both (or call `mlflow.set_tracking_uri`). Versions are
registered for batch use: they carry a placeholder serving container, so they are not
deployable to an endpoint as they are.

A hyperparameter sweep (`gcpbricks[tuning]`) logs as a Databricks sweep does, one run
with a child run per trial:

```python
from gcpbricks import mlflow, tuning

mlflow.set_experiment("roas-d30-hpt")
result = tuning.sweep({"learning_rate": {"type": "double", "min": 1e-3, "max": 0.3, "scale": "log"},
                       "max_depth": {"type": "integer", "min": 3, "max": 10}},
                      objective=train_and_score, metric="r2", n_trials=20, run_name=sweep_id)
result.best_params, result.best_value

# A retrain trains with @champion's tuned params, else its defaults:
params, source = tuning.champion_params("roas_d30", space)   # ({}, "defaults") if none
with mlflow.start_run(run_name=model_id):
    tuning.log_params(params, space, source)                   # what the next retrain reuses
    fit(defaults | params)
```

The params a retrain reuses are the champion's, not the best score ever logged: scores from
different validation windows don't compare, and the champion's are the ones that beat their
predecessor on the same window. A sweep's best becomes them by winning the gate. They are
reused only while the search space is unchanged, and a non-prod run reads prod's champion.

## Datasets and dashboards

```yaml
resources:
  schemas:
    predictions: {name: ml_predictions, comment: Scored cohorts}   # dataset ml_predictions[_<target>]
  dashboards:
    ops:
      display_name: demo_model ops                               # "[<target>] ..." off prod
      file_path: dashboards/ops.yaml
```

Both are applied by `mlops bundle deploy` and removed by `mlops bundle destroy`.

- **schemas** are Databricks' schemas as BigQuery datasets: `catalog_name` is the GCP project
  (default `gcp.project`), `location` defaults to `us-central1`. A dataset that already exists is
  adopted, not recreated, and a non-empty one is never deleted by destroy. `comment` and the
  bundle's labels are set when the dataset is created; later changes to them aren't applied.
  `${resources.schemas.<key>.name}` resolves to the target's dataset, e.g. in a Vertex model's `output_table`.
- **dashboards** are the console's (`gcpbricks.dashboards`), not Cloud Monitoring's:
  `mlops bundle validate` checks the spec and deploy uploads it beside the job definitions,
  where the console finds it. A data widget reads a source or a dataset and aggregates it:

  ```yaml
  # dashboards/ops.yaml
  window: 30d                       # the page's, until a reader picks another
  datasets:                         # BigQuery SQL for data outside the platform; @start/@end = window
    accuracy: {bigquery: "SELECT day, model, mape FROM `p.monitoring.accuracy_daily` WHERE day >= @start", time: day}
  filters:                          # page dropdowns; a pick narrows every widget with the field
    model: {dataset: accuracy, field: model, label: Model}   # and is the dataset's @model ('' for All)
  widgets:
    - {title: Failed runs (7d), type: counter, source: runs, filter: {state: FAILED}, window: 7d, width: 3}
    - {title: Run time by job, type: line, source: runs, measure: avg(duration_min), by: job, bin: 1d}
    - {title: Minutes per task, type: bar, source: tasks, measure: avg(duration_min), by: task}
    - {title: MAPE by model, type: line, dataset: accuracy, measure: avg(mape), by: model, bin: 1w}
    - {title: Open alerts, type: alerts}
    - {title: Warnings, type: logs, query: 'severity>=WARNING'}
  ```

  | | |
  |---|---|
  | `type` | `counter`, `table`, `line`, `bar` read data; `logs`, `alerts`, `text` do not |
  | `source` | the bundle's own, on the target: `runs` (job, state, start, end, duration_min), `tasks` (job, task, status, start, duration_min), `memory` (run_job, state, start, peak_mb, limit_mb, peak_fraction: each recent Cloud Run execution of its job clusters) and `model_metrics` (experiment, run, state, start, metric, value: its experiments' runs, as its deploy recorded them) |
  | `dataset` | one of `datasets`: its SQL runs in BigQuery, capped at 2 GiB billed, cached 2 minutes. Keep the logic in dbt and the dataset a plain SELECT |
  | `measure` | `count`, or `avg` / `sum` / `min` / `max` / `p95` / `last` of a field |
  | `by`, `bin`, `filter`, `window` | group by fields; by `1h` / `1d` / `1w` of the time field; `field: value` or `[values]`; e.g. `24h`, `7d` |
  | `width` | columns of 12 (default 6) |

  `logs` reads a Logging filter over the window; `alerts` the open alerts of the bundle's own
  policies on the target; `text` shows its text, `[label](https://...)` as a link.
  `mlops bundle deploy` dry-runs each dataset's SQL (nothing billed) and refuses a column a
  widget or filter names that it doesn't return.

Each deploy also writes `deployment.json` beside the job definitions (as Databricks keeps one per
target): the code version, the platform that deployed it, when and by whom, and every resource
with its name on the target. The console's Bundles page reads it.

## Jobs across repos

Each repo is its own bundle. To run another repo's job as a step, a job looks it up by
name, Databricks' way, and calls it with `run_job_task`:

```yaml
# demo_model/mlops.yaml: the outcome's owner schedules the chain
variables:
  features_job:
    lookup: {job: dbt-demo-features}   # "<bundle>-<job>" (its Workflow on prod)
resources:
  jobs:
    roas_daily:
      schedule: {quartz_cron_expression: "0 0 16 * * ?", timezone_id: UTC}
      tasks:
        - task_key: features
          run_job_task: {job_id: "${var.features_job}"}    # waits for dbt-features's whole run
        - task_key: serve
          depends_on: [{task_key: features}]
          batch_serve_task: {...}
```

- A lookup resolves offline, to that job's Workflow on the same target (`-dev` and so on),
  in the same project and region; `mlops bundle deploy` fails if the Workflow doesn't exist
  yet. Only `lookup.job` is supported.
- A job's Workflow reads its definition from its own environment, so it starts with no
  argument, or `{"params": {...}}`, whoever starts it. The caller's run_as account needs
  `roles/workflows.invoker` on it.
- The called job runs only from its callers: give it no schedule of its own, and give it
  one caller. Two chains that need the same job would build it twice; merge them, or let
  the second follow with `trigger.table_update`. Between repos, prefer
  `trigger.table_update` on the tables a job reads: the producer keeps its own schedule and
  nobody calls another repo's job. Within a repo, order tasks with `depends_on`.
- Job keys may hold `_`; GCP names (Workflows, Cloud Run, schedulers) use `-` instead.

## Serving predictions: `batch_serve_task`

```yaml
trigger:                               # runs once the features land, instead of a schedule
  table_update: {table_names: [my-gcp-project.ml_feature_store.fs_tapjoy_roas__features]}
tasks:
  - task_key: serve
    batch_serve_task:
      window_days: 14                  # install_date in [today - 14, today]
      models:
        - {model: tapjoy_roas_d30, job_cluster_key: tapjoy-roas-d30, python_file: serve.py}   # + window_days
        - model: ga_pltv_d14
          job_cluster_key: ga-pltv-d14
          python_file: serve.py
          window: {from_days: -17, to_days: 0}   # days from the run's date; instead of window_days
          output:                               # how its job writes (window().output); default replace_window
            mode: append                        # replace_window | append | insert_new | merge
            keys: [install_date, user_pseudo_id, product_id_encrypted]
            watermark: [product_id_encrypted]   # append past each group's latest install_date
            partition_by: predict_date          # set when the first write creates the table
            cluster_by: [product_id_encrypted, install_date]
            stamps: {predict_date: run_date, data_date: data_date}
      parameters:                      # run options, filled per run; empty ones are dropped
        from_date: "{{job.parameters.from_date}}"
        to_date: "{{job.parameters.to_date}}"
        only_models: "{{job.parameters.only_models}}"
```

The task runs every model over its window, in parallel (4 at a time), and fails if any failed;
a failed model doesn't stop the others. That is all it does: it reads nothing back, as a
batch prediction API returns predictions and leaves the rest to you. What a model does with
its predictions is its own code, with `gcpbricks.batch_serve`'s building blocks, each
optional, as a Databricks batch inference notebook calls `score_batch` and writes where it likes:

```python
from gcpbricks import batch_serve

w = batch_serve.window()                                     # the model, FROM_DATE, TO_DATE, the run's start
model = batch_serve.load_model("models:/roas_d30@champion")  # resolved once per run
for f, t in batch_serve.task_chunks(w.from_date, w.to_date, chunk_days=7):   # this Cloud Run task's share
    rows = batch_serve.read_window("ml_feature_store.fs_demo_features", f, t,
                                   date_col="install_date", as_of_ms=w.started_ms)
    preds = batch_serve.score_batch(model, rows, prediction_col="predicted_roas_d30",
                                    keep_columns=["install_date", "campaign_id"])
    # ... the model's own post-processing ...
    batch_serve.write_window(preds, "my-gcp-project.ml_predictions.roas_d30_predictions",
                             f, t, date_col="install_date", entity_id_columns=["install_date", "campaign_id"],
                             prediction_col="predicted_roas_d30")
    batch_serve.chunk_done(f, t)                             # a repair of this run skips it
```

- `score_batch` returns a DataFrame (the kept columns, the prediction, `model_id`,
  `predicted_at`) and writes nothing.
- `read_window(..., as_of_ms=w.started_ms)` pins a read to the run's start (BigQuery time
  travel), the same moment for every model and Cloud Run task of the run, so every chunk
  reads the same version of the features.
- `write(preds, table, f, t, mode=w.output.get("mode", "replace_window"), keys=..., stamps=...)`
  writes as a Databricks job writes its Delta table: `replace_window` is `write_window` below;
  `append` inserts every row; `insert_new` inserts only rows whose `keys` the table lacks (a
  written row never changes, a rerun inserts nothing twice); `merge` updates the matched rows
  and inserts the rest. `stamps` ({column: value}) is set on every row first; the job maps the
  declared sources itself (`w.run_date`, the run's date; `data_date`, its own data's date).
  `scored_keys(table, keys, f, t)` gives the keys already written, for a job to skip them.
  `watermarks(table, by, as_of_ms=w.started_ms)` gives each group's latest date in the table,
  for an append job to write only what lies past it; `w.backfill` is true on a run given its
  dates, where `output.watermark` says to write as insert_new by `keys` instead. The first
  write creates the table partitioned by `partition_by` (default the date column) and
  clustered by `cluster_by` (default model_id).
- `write_window` replaces the table's rows for `[f, t]` by (install_date in the window,
  model_id) in one transaction, creating the table on the first write and adding new columns.
  It checks first (columns, model_id, inside the window, finite, unique keys) and refuses a
  batch with fewer rows than `min_keep_ratio` (0.5) x the rows it replaces. Reruns replace,
  never duplicate; other model versions' rows stay as history. Write chunk by chunk, so Cloud
  Run tasks writing their own chunks never replace each other's.
- `chunk_done(f, t)` notes the chunk finished (an empty object under
  `.bundle/<bundle>/serve_progress/` in the bundle's bucket), and `task_chunks` leaves out
  the chunks the run already noted. Without it a repair scores the whole window again.
- Anything after scoring that spans models, or should rerun on its own, is a later task
  (`depends_on`) reading the tables the models wrote.

**Repairing a serve.** `mlops bundle repair <job> --run <id>`, or Repair run in the console, finishes the
failed run. In a batch_serve_task that failed, only the failed models run again. They keep the
run's `SERVE_RUN_ID`, its start (`started_ms`, so they read the same feature snapshot) and their
windows, and their jobs skip the chunks `chunk_done` noted. `--models a,b` (the console's ticks)
runs those models again in full, whether they failed or not, under a new `SERVE_RUN_ID` and so
from their first chunk, still on the run's snapshot and windows. To score fresh features or
today's window, start a new run (`mlops bundle run`). A succeeded task after a re-run model is
still reused, so name it with `--only` in a new run if it must see the new predictions.

A served model runs on one of the job's `job_clusters`, whose Cloud Run job the platform creates
from `new_cluster` (`docker_image`, `node_type_id`, `spark_env_vars`, and `num_workers` for
parallel tasks: CLOUD_RUN_TASK_INDEX / _COUNT), as a Databricks task runs on its cluster;
`job:` instead names a Cloud Run job that already exists. With `python_file` (+ `parameters`), the
model gets a Cloud Run job of its own, the cluster's image and shape running that bundle file
from the deployed files, as a relative `spark_python_task.python_file` runs: code ships with
`mlops bundle deploy`, and one runtime image serves every model.

Or a model is scored by a Vertex AI batch prediction job (`vertex_batch_prediction`):
`input_table`'s rows with its declared `date_col` in the window go in, and this run's
predictions (the entity columns, `date_col`, `prediction_col`, model_id, scored_at) replace
`output_table`, for a later task to
use; `prediction_expr` picks the prediction out of Vertex's output row.

A backfill: `mlops bundle run serve -t dev --params from_date=2026-09-01,to_date=2026-09-30`;
some models only: `--params only_models=roas_d30`. A run's error names the models that failed.

## Feature store SDK

`pip install "gcpbricks[features]==<version>"`. Feature groups are declared in
dbt-features, on the model that produces the table, and nowhere else:

```yaml
models:
  - name: fs_demo_features
    meta:
      feature_store:
        offline_table: ml_feature_store.fs_demo_features   # what the engine reads
        families: {acquisition: [cost, installs, "cost_day_*"]}  # optional column globs
    columns:
      - {name: install_date, data_type: String, meta: {entity: true}}             # a key
      - {name: feature_timestamp, data_type: DateTime, meta: {feature_timestamp: true}}
      - {name: cost, data_type: Float64, meta: {family: acquisition}}            # a feature
```

Every other column with a `data_type` is a feature; one no family matches is in `default`.
dbt-features's CI checks `mlops-features render` on an MR and, on main, publishes it to
`gs://my-ml-bucket/feature_store/features.json`. The SDK reads that file (or
`FEATURE_CONFIG`, or a dbt-features checkout directory): each declared model is a batch feature
view, and there is no registry to deploy.

```python
from gcpbricks.features import FeatureService, FeatureStore

fs = FeatureStore()                       # engine="bigquery", Application Default Credentials
fs.list_feature_views(family="revenue")   # or kind="label"
fv = fs.get_feature_view("fs_demo_features")
fv.join_keys, fv.timestamp_field, fv.families
df = fv.family("acquisition").get_features_in_range(start, end)   # the window's rows
lbl = fs.get_feature_view("lbl_demo__labels")
ts = fs.create_training_set(events, [fv.family("acquisition"),      # a family
                                     fv[["total_rev_day_7_cum"]],   # chosen features
                                     lbl[["total_rev_day_30_cum"]]])
train_df = ts.load_df()                   # point-in-time join onto the events
train_df = ts.load_df(float32=True)       # float64 columns as float32: half the memory
# A view's own rows in a window as the events; labels are a label view's features.
ts = fs.create_training_set(fv.spine(start, end, where="cost > 0"),
                            ["lbl_demo__labels:total_rev_day_30_cum"],
                            exclude_columns=["feature_timestamp"])
ts.write_parquet("/tmp/train.parquet")    # streamed batch by batch, never all in memory
ts.to_sql()                               # the one query it runs, in the engine's dialect
# A features.yaml's SQL blocks (feature_manager), computed in the same query:
bfv = fs.batch_feature_view("roas_d30__sql", "fs_demo_features",
                            compile_spec(spec).sql_stage(), at_days=7)
ts = fs.create_training_set(fv.spine(start, end), [bfv, fv[["platform"]]])
svc = FeatureService("roas_d30", [fv.family("acquisition")], spine_inputs=["platform"])
svc.save(model_dir)                       # feature_spec.json next to the model
scored = fs.score_batch(model, events, FeatureService.load(model_dir))
fv.table_schema()                         # the live table's {column: type}
fv.max_timestamp()                        # the latest timestamp it holds
```

**`features.FETCH_VERSION`** versions what a training read returns for the same spec and
window. A release that changes a read's rows or values bumps it, so a project that caches
training sets (manifest beside each cached parquet) fetches again instead of
reusing one the earlier release built.

**Engines.** `FeatureStore(engine=...)` picks where tables are read and joins run:

| Engine | `offline_table` | For |
|---|---|---|
| `bigquery` (default) | `dataset.table` or `project.dataset.table`; an Iceberg table BigQuery sees (BigQuery-managed, or in the BigLake metastore) reads like any other | production |
| `spark` (`[spark]` extra: pyspark 4, Java 17) | a Spark table: `spark_catalog=` is prefixed onto a `dataset.table`, e.g. the Iceberg catalog `spark_conf=` configures; or a Parquet path | Spark and Iceberg once DE's cluster exists; `spark_conf` points the session at it |
| `duckdb` | a local Parquet path | tests (CI has no GCP credentials) and local files |

Every read is one query: Ibis builds it over the views' declared columns and compiles it to
the engine's dialect, each view's table going in as a subquery of the engine's own `FROM`
(BigQuery time travel, a Parquet path, a spine's `where`). The engine's own client runs it:
BigQuery's (labelled jobs, results streamed), the Spark session, DuckDB in-process. The
point-in-time join is the latest row at or before each event, per view, on that view's keys.

`data_type` in dbt-features is the adapter's own, BigQuery's, Spark's or ClickHouse's, and
maps to a feature type case-insensitively; arrays, structs and maps aren't features. Keys
are strings or integers: a date key such as `install_date` is a `'YYYY-MM-DD'` `STRING`.

| Tecton / Databricks | Here |
|---|---|
| batch feature view / feature table (primary keys, timestamp key) | a dbt model with `meta.feature_store`: `fs.get_feature_view(name)`; keys are its `meta.entity` columns |
| `fv[["a", "b"]]` | `fv[["a", "b"]]`, and `fv.family(name)` for a declared family |
| `get_features_in_range(start, end)` / `read_table` | `fv.get_features_in_range(start, end, where=)`: every row in the window |
| `create_training_set(df, lookups, exclude_columns=)` / `get_features_for_events(events)` | `fs.create_training_set(events, features, exclude_columns=)` (or `feature_service=`); `fs.get_features_for_events(...)` is its `load_df()`. The events' timestamp column is `event_timestamp` unless `timestamp_key=` says otherwise |
| `df.filter(...)` on the spine before `create_training_set` | `fv.spine(start, end, where="cost > 0")`: the view's own rows (keys and timestamp); the SQL condition is applied in the spine's own read, so a dropped row is never joined or downloaded |
| `training_set.load_df().write.parquet(path)` | `ts.write_parquet(path)`: the result streams to the file batch by batch, so memory stays at a batch, not the window |
| the table's schema | `fv.table_schema()`: the live table's columns and types, metadata only |
| `FeatureService` / feature spec + `log_model` | `FeatureService(name, features, spine_inputs=)`, saved with `svc.save(model_dir)` wherever the model artifact goes, or `mlflow.pyfunc.log_model(..., feature_spec=svc)`; `load_model(...).metadata.feature_spec` reads it back |
| `BatchFeatureView` with a transformation and `Aggregate`s | `fs.batch_feature_view(name, source, stage, at_days=)`: a features.yaml's `aggregations` and `transforms` (feature_manager), computed in the training set's query |
| `score_batch` | `fs.score_batch(model, events, svc)`: `model.predict` on the service's columns, in order; a feature dbt-features has since dropped fails, not a silent NaN |

The as-of match has no age limit, as in Databricks, so a table whose grain includes a date
(one row per cohort per `install_date`) keeps that date as a key: the join never takes
another day's row. `FeatureStore(as_of=datetime)` reads every table as it was at that time
(BigQuery time travel, the last 7 days), so a run that reads in several steps, such as a
scoring job reading in chunks, sees one version of each table while dbt-features rewrites it.

A `spine()` stays in the engine and its own view's features are read off its rows, so a
window of one view plus its labels costs about what the same hand-written join does. On
BigQuery a DataFrame of events is uploaded to the `feast` dataset (named for the engine it
used to serve) as a temp table (30-minute expiry, deleted after the query), which needs
`bigquery.tables.create` there.

The 0.7 names (`training_set`, `spine`, `read_table`, `describe`, `list_groups`, `resolve`,
`table_schema`/`max_timestamp` by group, `FeatureSpec`, `score_batch(fs, ...)`) still read
the same, for projects on `mlops_version: latest` that haven't moved; they go in a later
release.

## Costs

The console's **Costs** page estimates what the MLOps platform spends, over the last 7, 30 or 90 days:
by day (stacked by source), by service, and what bills by the hour right now. It reads what GCP
records of each service's use and prices it at list price from the Cloud Billing Catalog, before
discounts, credits and committed use; it never reads the billing account.

| Source | Use | Price |
|---|---|---|
| BigQuery | bytes billed per query job the console's service account ran with the platform's `mlops_exec` label (as bundle/job) or dbt's, `region-us.INFORMATION_SCHEMA.JOBS_BY_USER` | Analysis, per TiB |
| Cloud Run | vCPU- and GiB-seconds allocated (Cloud Monitoring): bundles' jobs, the `mlops-notify` job and the console's own service, plus its idle minimum instances | Jobs / Services CPU and Memory SKUs |
| Vertex AI | bundles' custom jobs and pipeline steps: machine x replicas x run time, named bundle/job from their labels; labelled endpoints at their minimum replicas | Training/Pipelines and Online/Batch Prediction core, RAM, GPU + management fee |
| Notebooks | Colab runtimes: machine x running time from the audit log (assign, start, stop, delete), plus their disks while they exist | Vertex Colab core, RAM, GPU, disk + management fee |

Colab's idle shutdown isn't in the audit log: a runtime that stopped that way counts from its
start to its idle timeout (the least it ran), and the page gives the most too (to its next
start). The project's other workloads (what carries no `mlops_bundle` / `mlops_job` label, other
services, other users' queries) are left out. A source the console can't read (a missing role) is
named on the page and left out; the console's service account needs Monitoring and Logging
viewer, Run viewer, Vertex AI viewer and `compute.machineTypes.get`. Prices come from the live
catalog when the console's project has the Cloud Billing API (`cloudbilling.googleapis.com`)
enabled, else from the snapshot bundled with the release (`console/price_snapshot.json`), and
the page says which.

## Notebooks: the console's Notebooks, Workspace and editor

Notebooks run on Colab Enterprise; the console is where they're kept, edited and run.

- **Workspace** keeps `.ipynb` notebooks in `gs://<bucket>/workspace/` (`MLOPS_WORKSPACE`):
  `Users/<email>/` and `Shared/`. Each user writes their own folder and `Shared/`, an admin
  anywhere. Everyone views a notebook in someone's folder unless its owner shares it
  otherwise: the editor's Share adds people as Can view or Can edit, and makes it visible to
  only them (settings in `workspace/.shares/<path>.json`). A view-only notebook opens
  read-only. A save is over the version the editor read, so it never overwrites someone's
  newer save. A notebook's `/Workspace/<path>` is what a `notebook_task`'s `notebook_path`
  names to run it on a schedule.
- **Notebooks** starts you off: what running cells takes (a runtime of yours, notebook
  access, Claude), each with its button, your notebooks and New notebook. Its Runtimes tab
  lists every user's runtimes; you create one of yours from a template, and start, stop or
  delete your own (an admin, anyone's). A runtime bills while it runs and stops when idle as
  its template says. Its Templates tab lists the runtime templates (admins change them).
- **The editor** connects as Colab does (its Connect panel: one of your runtimes, started if
  it's stopped, or a new one from a template, its settings shown, secrets masked) and runs a
  notebook's cells on your own running runtime: code and markdown cells,
  run one (Shift+Enter, Ctrl/Cmd+Enter), run all, interrupt, restart; outputs (text, tables,
  images, errors) stream in as they come and save with the notebook. A cell tagged
  `parameters` is where a `notebook_task` puts its `base_parameters`. A save made elsewhere
  (another tab, Claude below) shows in an open editor within seconds; with unsaved edits of
  your own, the editor asks you to reload instead.

A Colab runtime belongs to one user and can't belong to a service account, so cells run with
your own Google token: the editor's **Allow notebook access** (Google OAuth, scope
cloud-platform) keeps it in an HttpOnly cookie for the notebook API, bound to your IAP
sign-in, until it expires in an hour. The console relays each cell to the runtime's Jupyter
server through Colab's proxy, which takes the owner's token with its tunnel cookie; the proxy
refuses to be framed, so the console draws the editor itself.

Setting it up, once per console:

1. A Google OAuth client: APIs & Services > Credentials > Create OAuth client ID, type Web
   application, authorized redirect URI `https://<console host>/api/notebooks/auth/callback`
   (OAuth consent screen: Internal).
2. Its ID and secret as this repo's masked GitLab CI variables `NOTEBOOK_OAUTH_CLIENT_ID` and
   `NOTEBOOK_OAUTH_CLIENT_SECRET`; the console's deploy passes them as
   `MLOPS_OAUTH_CLIENT_ID` / `MLOPS_OAUTH_CLIENT_SECRET` (the secret through the module's
   `secret_env`, kept out of plans). Without them notebooks open and save, but cells don't run.
3. The console's service account: object admin on the workspace prefix, and Vertex AI's
   notebook runtime permissions to list, assign, start, stop and delete runtimes. Each user
   needs the Colab Enterprise user permissions they need to use Colab itself.

### Claude on your notebooks

`mlops-notebooks-mcp` is an MCP server, run on your machine as you, that gives Claude Code (or
any MCP client) the workspace and your runtimes: it reads notebooks (cells numbered from 0,
with their outputs as text), creates them, inserts, edits, moves and deletes cells, runs cells
and saves their outputs into the notebook, runs scratch code, interrupts or restarts the
kernel, and lists, starts, stops and makes your runtimes. Code runs on your running runtime's
first Python kernel, the one the editor connects to, so Claude and the editor share variables;
open the notebook in the console to watch Claude's edits and outputs land.

Every tool call runs on its own thread, so a cell that runs for an hour holds nothing else:
`kernel('interrupt')`, `list_runtimes` and reads answer meanwhile. `run_cells` and `run_code`
return when the run ends or after `wait` seconds (default 90, under the 120 s after which
Claude Code moves a call to the background), whichever is first; a run still going is named,
with its outputs so far, and `run_status(run_id, wait=...)` follows it. Runs on one kernel
take turns, as Jupyter would queue them. `kernel('status')` is the kernel's own word on itself:
idle, busy or dead, and its last activity.

**What a cell is doing, whoever runs it.** A run of a notebook's cells, by the editor or by
Claude, keeps a run record beside the workspace (`.runs/<path>/<run_id>.json`): which cells,
which is running, its outputs so far, and a heartbeat its runner writes at least every 10 s,
output or not. The editor polls the notebook's records and the kernel's status, so a cell
another runner runs (Claude, another tab, a page you closed) shows running there with its
live outputs, and one that can't be running as its record claims says which way it ended:

| Under the cell | What it means |
|---|---|
| Running · by Claude · 12m · heard from 4s ago | its runner is alive (heartbeat within 40 s) |
| Still running on the kernel, not captured | no heartbeat, the kernel busy: the page or session that ran it is gone; outputs from now on are lost; Interrupt stops it |
| Run lost | no heartbeat, the kernel idle: the run ended while nobody was capturing it |
| Kernel died | the kernel is dead: usually out of memory |
| Kernel gone | the kernel was shut down or restarted away |
| Runtime stopped | the runtime is not RUNNING: Colab's idle shutdown, a stop, a delete |

The compute button says the same of the runtime and the kernel (idle, busy, busy with nothing
this notebook runs, dead, gone) and its tooltip gives the runtime's idle shutdown. Colab stops a
runtime after it with no kernel activity **and** no notebook connected; a process a cell
started detached (`setsid nohup`) is not kernel activity. `run_status(path=...)` gives Claude the
same view. A Python process a cell starts calls Google as you, as the kernel does
(`credentials_code`'s sitecustomize): before, only the kernel itself could, and a script it ran
got HTTP 401 from the runtime's metadata server.

It writes the console's **Blog** too, as you: lists and reads posts, publishes a new one
(`create_blog_post`: title, Markdown body, tags, summary), edits your own field by field
(saved over the version it read, so an edit made in the editor meanwhile fails it rather than
being overwritten), and uploads a local image into a post, returning the Markdown that shows
it. Posts live at `MLOPS_BLOG` (default `gs://my-ml-bucket/blog`), by the console's own rules.

The console's **Use with Claude** (Notebooks, Workspace, and the editor's **Claude**) gives the setup filled
in for that console, to copy: sign in once, then one command (it needs `uv` and git access
to this repo; `MLOPS_PLATFORM_GIT` on the console changes where it installs from):

```bash
gcloud auth application-default login             # you, not a service account
claude mcp add --scope user mlops-notebooks -e MLOPS_PROJECT=<project> -e MLOPS_REGION=<region> \
    -e MLOPS_WORKSPACE=gs://<bucket>/workspace -e MLOPS_CONSOLE_URL=<console> \
    -- uvx --from "gcpbricks[notebooks-mcp] @ git+ssh://git@github.com/hankniel/gcpbricks.git" mlops-notebooks-mcp
```

With `MLOPS_CONSOLE_URL`, what Claude reads or creates comes with the notebook's link.

It reads and writes the workspace bucket and calls Colab with your Application Default
Credentials, so it needs the same access as you in the console (your own folder,
`Shared/` and what's shared with you, by the same rules); `MLOPS_PROJECT`, `MLOPS_REGION`
and `MLOPS_WORKSPACE` default to dev's. An edit is saved over the version it read and redone on a newer one, so it doesn't overwrite the
editor's autosave. Starting or making a runtime bills while it runs: Claude Code asks before
each tool call unless you allow it.

## Using it in a project

1. Add `mlops.yaml` at the project root (see [`tests/fixtures/bundle/`](../tests/fixtures/bundle)).
2. Run on the latest platform: `bundle.mlops_version: latest` in `mlops.yaml`, and
   ```yaml
   include:
     - component: $CI_SERVER_FQDN/YOUR_GROUP/gcpbricks/project@main
   ```
   Don't lock `gcpbricks` in the project's requirements: install it at job start.
   Each platform release then redeploys the bundle itself (below), with no commit or
   pipeline in the project. To pin a release instead, include `project@v<version>` with
   `inputs: {platform_version: "<version>"}` and set `mlops_version` to it; a pinned bundle
   moves by its own MR. Either way, add the project to gcpbricks's CI/CD job token
   allowlist, so its jobs can install the package with `CI_JOB_TOKEN`.
3. That's all the project carries: no terraform. `mlops bundle deploy -t <target>` applies
   the jobs' infrastructure (Workflows, the Cloud Run jobs, schedulers, triggers, alerts)
   with the terraform module this version ships, as Databricks' CLI does, keeping the state
   at `gs://<bucket>/<target prefix>.bundle/<bundle>/terraform/` under terraform's lock;
   `mlops bundle destroy -t <target>` removes it. The CI component's deploy job installs
   terraform; to deploy from a laptop, have terraform >= 1.6 on PATH (dev only: prod
   deploys from CI).
4. Grant the run_as service account `roles/workflows.invoker` and `roles/run.developer` (and,
   for a `file_arrival` trigger, `roles/eventarc.eventReceiver`, with `roles/pubsub.publisher`
   for the project's GCS service agent; for a `table_update` on a lakehouse table,
   `biglake.tables.get` on its catalog). The deploying account (`GCP_SERVICE_ACCOUNT_KEY` in
   CI) needs `roles/workflows.editor`, `roles/cloudscheduler.admin`, `roles/run.admin`,
   `roles/monitoring.alertPolicyEditor` and `roles/monitoring.notificationChannelEditor`,
   for environments `roles/cloudbuild.builds.editor` and `roles/artifactregistry.writer` on
   the image repository (Cloud Build's own account pushes, so it needs the writer role too),
   object admin on the bundle's bucket, and `roles/iam.serviceAccountUser` on the run_as
   account (the module manages no IAM).

`mlops bundle run <job> -t dev --params k=v` starts a run by hand with the scheduler's
argument and these job parameters; `mlops bundle runs <job>` lists runs with their ids, and
`mlops bundle repair <job> --run <id>` repairs a failed run (`--params k=v,k2=` overrides its
parameters, an empty value drops an override; `--tasks` adds tasks that succeeded; a
batch_serve_task runs only its failed models again, and `--models` the ones to run again in
full, see Repairing a serve, above). `mlops bundle summary` shows what a target deploys.

**Debugging a run from the terminal.** `why`, `task` and `logs` run the console's own reads
on your gcloud credentials, so they need no console URL or IAP token:

- `mlops bundle why <job> --run <id> [--task K]`: a failed run followed down to its failed
  step's last log lines (the console's "Why it failed"). A run that succeeded but skipped
  steps (a gate's rejection skips promote, and the run still SUCCEEDS) lists each pipeline
  condition that didn't hold and the Markdown verdicts its steps wrote.
- `mlops bundle task <job> <task> --run <id>`: the task's state; for a `vertex_pipeline_task`,
  every step with its state, condition, cache use, output values and artifacts' metadata (a
  gate's metrics), then its Markdown verdicts.
- `mlops bundle logs <job> <task> --run <id> [--step S]`: what the task, or one pipeline step
  (`--step gate-op`), printed. KFP's executor_input dump and the runtime's warnings are
  dropped; `--raw` keeps them.

**Repairs are attempts of one run**, as in Databricks. A repair is a new execution of the job's
Workflow from the run's latest attempt: the tasks that succeeded are reused, the rest run
again. Its argument names the run (`repair_of`) and its number (`repair_id`), so every
attempt has the run's `{{job.run_id}}`, and `{{job.repair_count}}` says which it is. Parameter
overrides stay for later repairs until dropped. A repair always builds on the latest attempt:
`--latest-repair-id` (the console's dialog sends it) refuses one made from a stale view. The
run's record, `jobs/<job>.runs/<run id>.json` beside the job's definition, lists its repairs
(who, when, overrides, tasks, models, execution), since the first execution can't point at
them; whoever repairs writes it, so the console's service account and anyone running
`mlops bundle repair` need to create and overwrite objects in the bundle's bucket (`roles/storage.objectUser`). The console lists a run once,
with its latest attempt's state; its page picks the attempt (Original run, Repair 1, ...), a
reused task opens in the attempt that ran it, and a task's page lists it across attempts. A
failure email names the run and the repair that failed. A Workflow deployed with an earlier
platform version ignores `repair_of`: its repairs still group in the console, but each has
its own `{{job.run_id}}`.

**A repair runs the tasks picked**, as Databricks' "Select tasks to rerun" does. The tasks that
didn't succeed start picked, and any task can be picked or dropped on the graph. A dropped
task that didn't succeed keeps how it ended (logged `KEPT`, so its failure doesn't alert again),
and its dependents' `run_if` still sees it failed: drop a failed `dbt build` and pick only its
`dbt retry` task. **A run fails only when a leaf task failed** (one no task depends on, FAILED or
UPSTREAM_FAILED), as in Databricks. A run whose leaf tasks all succeeded while another task
failed reads "Succeeded with failures" in the console, and sends no failure email or alert. It
can still be repaired, from its page, its tasks' pages or `mlops bundle repair`: its failed
tasks start picked, and the tasks that ran on without them can be picked to run again.

**A cancelled run is repaired from the console too.** Its execution returns no task results,
so the console reads them from the run's `mlops_task` logs, where each task that succeeded
logs its result (outcome, child run). Those tasks are reused, and the ones the cancel stopped
or never started run again. A task that succeeded under a Workflow deployed before this
logged no result, so it runs again. `mlops bundle repair` doesn't read logs and refuses a
cancelled run.

**A pipeline task's repair reruns its pipeline in Vertex.** A `vertex_pipeline_task` that
failed is repaired as a Vertex rerun of its pipeline run (Vertex AI Pipelines' rerun, a
Preview feature, through the v1beta1 API): the steps that succeeded are skipped and keep
their outputs, the rest run on the pipeline as deployed now, so a fix deployed since runs.
`--steps TASK=STEP` (or ticking a step in the console's repair dialog) runs a step that
succeeded again too; `--fresh-pipelines TASK` (the dialog's "Start a new pipeline instead")
starts a new pipeline. Should Vertex refuse the rerun, the runner logs
`mlops_rerun_refused` with why and starts a new pipeline instead.

Each task logs the Cloud Run execution it started (`mlops_task_execution`),
so its logs and a cancel reach exactly that container.

**Why it failed.** A failed run's and a failed task's console pages, and its failure email,
follow the failure down to where it happened: the task, its child run or runner, a Vertex
pipeline's failed step and that step's custom job, or the task's container. They show that
leaf's own error, its last 40 log lines at every severity (training libraries often log the
real reason without one), and the code that ran: deploy records the git commit and branch
(CI's, or the checkout's, marked when it had uncommitted changes) in `deployment.json`.

## Versions and install

One tag `vX.Y.Z` is one platform version: the package, the CI component and the terraform
module; `pyproject.toml`'s version must equal the tag. On the tag, CI:

1. builds the dbt, runtime and console images at the version;
2. **gates** it: deploys `e2e/` (the `mlops-gate` bundle) to dev on the new runtime and runs
   its job to the end; a failure stops the release here;
3. publishes the package to this project's GitLab PyPI registry and creates the GitLab Release;
4. **rolls it out**: `mlops bundle redeploy -t dev` redeploys every bundle on `latest` from the
   files its last dev deploy saved (`.bundle/<bundle>/files/`), with this release, so no
   project runs a pipeline; then `rollout-prod`, started by hand once dev looks right, does
   the same for prod. A redeploy keeps the bundle's published Vertex pipelines (recompiled on
   the project's next deploy) and rebuilds its environments on the new runtime. Bundles
   pinned to a release are skipped. Every deploy records the release it ran
   (`deployment.json`), which the console's Bundles page shows.

The rollout runs as `GCP_SERVICE_ACCOUNT_KEY`'s account, so that account needs the roles a
bundle's deploy needs (step 4 of "Using it in a project").

The package installs from this project's PyPI index (project PROJECT_ID; the group-level index
refuses accounts that are members of the project only). It keeps sha256 hashes, so projects
that install with `--require-hashes` can lock it (a `git+https` dependency cannot be hashed):

```bash
# CI: the component's default; on a laptop, a personal access token with read_api
pip install --index-url "https://__token__:<token>@git.example.com/api/v4/projects/PROJECT_ID/packages/pypi/simple" \
    --extra-index-url https://pypi.org/simple gcpbricks
```

## Known problems

Open as of 2026-10-19; remove each once fixed.

- **The notebook editor rests on behaviour Google doesn't document.** Colab's proxy taking the
  runtime owner's bearer token with its tunnel cookie (for REST and the kernel's WebSocket) was
  verified by hand on 2026-10-19; if Google changes it, cells stop running in the console
  (opening, editing and saving keep working, and Colab's own UI runs the same runtimes).
- **Run records and heartbeats haven't run on a real runtime.** `mlops-notebooks-mcp` ran cells
  on a T4 runtime through the proxy with an Application Default Credentials token on
  2026-10-28, before runs moved to threads with records; the records, the heartbeat while a cell
  is silent, the liveness states and the child-process credentials are tested offline only.
- **`notebook_task` hasn't run end to end on a runtime template.** Its runner, the upload and
  the console's view are tested offline; a run with a custom machine failed on Colab's disk
  size before tasks moved to runtime templates, and the template path hasn't run since. The
  release gate (`e2e/`) now runs a notebook task, so the next release tag exercises it.

- `continuous`, the beta `triggers` list, task values, `sql_task`,
  `pipeline_task`, serverless `environments`, `git_source`, `permissions` and `lifecycle`;
  streaming and maintenance notifications; task-level `health`.
- `pipelines`, `quality_monitors`, `apps` and the other resources; experiment `tags` and
  `artifact_location`; registered model `schema_name` and `grants`; endpoint `ai_gateway`,
  `environment_vars`, provisioned concurrency and throughput.
- Discord: a webhook notification is a Cloud Monitoring channel, which can't post to Discord
  without a relay.
- Spark: `spark_python_task` runs plain Python where no Spark cluster is reachable.
- A generic `plan`: demo_model keeps its own (`python3 -m ci.mlops plan`), which compares
  training code with prod `@champion`.
