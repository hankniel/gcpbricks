"""mlops.yaml: a Databricks Asset Bundle, run on GCP.

    bundle: {name: demo-app, mlops_version: "0.4.0"}
    include: [resources/*.yml]
    gcp: {project: ..., region: us-central1, bucket: my-ml-bucket}
    run_as: {service_principal_name: sa@....iam.gserviceaccount.com}
    variables: {schema: {default: demo_app}}
    targets:
      dev: {mode: development, default: true}
      prod: {mode: production}
    resources:
      jobs:
        nightly:
          parameters: [{name: run_date, default: "{{job.start_time.iso_date}}"}]
          schedule: {quartz_cron_expression: "0 0 2 * * ?", timezone_id: UTC}
          job_clusters:
            - job_cluster_key: main
              new_cluster: {docker_image: {url: "${var.image}"}, node_type_id: e2-standard-4}
          tasks:
            - task_key: features
              dbt_task: {project_directory: dbt, schema: "${var.schema}",
                         commands: ["dbt build -s tag:demo_app"]}
            - task_key: score
              depends_on: [{task_key: features}]
              job_cluster_key: main
              python_wheel_task: {package_name: model_inference, entry_point: nightly,
                                  parameters: ["--date", "{{job.parameters.run_date}}"]}

Keys are Databricks' own. The extensions are `bundle.mlops_version`, `gcp`, and the task
types `vertex_pipeline_task` and `batch_serve_task`; `notebook_task` runs on Colab Enterprise; a Databricks key the platform does not
run is refused, never ignored.

load() resolves includes, the target's overrides and `${...}` substitutions, then
validates. render() turns the result into the terraform module's `plan`: each job's
Workflow argument is built here, in Python, so the scheduler and `mlops bundle run` send
the same thing and terraform never re-derives it.
"""
from __future__ import annotations

import copy
import glob
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gcpbricks.ml import Experiment, ModelServingEndpoint, RegisteredModel
from gcpbricks.targets import PROD, name_suffix, storage_prefix

NAME = r"^[a-z][a-z0-9-]{0,40}$"
KEY = r"^[a-z][a-z0-9_-]{0,40}$"
#: A task's default and maximum wait: Cloud Run jobs stop at 24 hours.
MAX_TASK_SECONDS = 86400
#: Workflows runs at most this many parallel branches and iterations at once.
MAX_CONCURRENCY = 20
RUN_IF = Literal["ALL_SUCCESS", "AT_LEAST_ONE_SUCCESS", "NONE_FAILED", "ALL_DONE",
                 "AT_LEAST_ONE_FAILED", "ALL_FAILED"]
#: `{{...}}` references a Workflow resolves at run time (workflows/job.yaml).
RUN_VALUES = {"job.id", "job.name", "job.run_id", "job.repair_count", "job.start_time.iso_date",
              "job.start_time.iso_datetime", "job.start_time.timestamp_ms", "task.name"}
#: What a table_update trigger tells the run it starts about each table: the commit that fired
#: it. BigQuery: lastModifiedTime and no version; Iceberg: the current snapshot's time and id.
TRIGGER_TABLE_FIELDS = ("commit_timestamp.iso_datetime", "commit_timestamp.timestamp_ms", "version")
#: `mlops bundle run`-style options a batch_serve_task's `parameters` may set.
BATCH_SERVE_PARAMS = {"from_date", "to_date", "only_models"}

# Runs a wheel's entry point the way Databricks' python_wheel_task does: by name, from the
# installed distribution's metadata, with the parameters as argv.
WHEEL_LAUNCHER = (
    "import sys\n"
    "from importlib.metadata import distribution\n"
    "_, package, name, *argv = sys.argv\n"
    "ep = next(e for e in distribution(package).entry_points if e.name == name)\n"
    "sys.argv = [name, *argv]\n"
    "sys.exit(ep.load()())\n"
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- the bundle's own settings ------------------------------------------------------------

class BundleInfo(_Strict):
    name: str = Field(pattern=NAME)
    #: The platform version this project pins (package, CI component and terraform module),
    #: or "latest": whichever release deploys it, and a platform release redeploys it.
    mlops_version: str


class Gcp(_Strict):
    project: str
    region: str = "us-central1"
    #: Artifacts bucket, without gs://: the bundle's files, pipeline specs, staged runs.
    bucket: str
    #: Artifact Registry repository the jobs' environments are built into (a GCP extension).
    #: Absent: <region>-docker.pkg.dev/<project>/mlops.
    image_repository: str | None = None
    #: A failed run's email is the platform's own, laid out as Databricks'
    #: (gcpbricks.notify, sent by the project's mlops-notify job, which this repo's CI
    #: deploys beside the console), to the job's on_failure emails, else DEFAULT_FAILURE_EMAILS.
    #: false: Cloud Monitoring's email, to on_failure only.
    mail: bool = True


class RunAs(_Strict):
    #: The GCP service account every Workflow, scheduler and Cloud Run job runs as.
    service_principal_name: str


class VariableLookup(_Strict):
    #: Another bundle's job, by its Workflow name on prod: "<bundle>-<job>". The variable
    #: resolves to it, for a run_job_task's job_id; the target's suffix is added there.
    job: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")


class Variable(_Strict):
    description: str | None = None
    default: str | int | float | bool | list | dict | None = None
    lookup: VariableLookup | None = None

    @model_validator(mode="after")
    def _one_source(self):
        if self.lookup and self.default is not None:
            raise ValueError("a variable has a default or a lookup, not both")
        return self


class Sync(_Strict):
    include: list[str] = []
    exclude: list[str] = []


class Target(_Strict):
    default: bool = False
    mode: Literal["development", "production"] | None = None
    variables: dict[str, str | int | float | bool | list | dict] = {}
    gcp: dict = {}
    run_as: RunAs | None = None
    resources: dict = {}


# --- jobs ---------------------------------------------------------------------------------

class Dependency(_Strict):
    task_key: str
    #: Only after a condition_task: run when it evaluated to this.
    outcome: Literal["true", "false"] | None = None


class PythonWheelTask(_Strict):
    package_name: str
    entry_point: str
    parameters: list[str] = []
    named_parameters: dict[str, str] = {}

    @model_validator(mode="after")
    def _one_style(self):
        if self.parameters and self.named_parameters:
            raise ValueError("python_wheel_task takes parameters or named_parameters, not both")
        return self


class SparkPythonTask(_Strict):
    #: Relative: a file of the bundle, run from the deployed files with the bundle's root on
    #: PYTHONPATH (Databricks' meaning; the image needs gcpbricks). Absolute: a file
    #: inside the cluster's image.
    python_file: str
    parameters: list[str] = []


def runs_bundle_code(python_file: str) -> bool:
    return not python_file.startswith("/")


def bundle_python(b: Bundle, target: str, python_file: str, parameters: list[str]) -> list[str]:
    """The command that runs a bundle file from the target's deployed files (mlops-task python)."""
    return ["mlops-task", "python", "--files", files_uri(b, target), "--", python_file, *parameters]


class DbtTask(_Strict):
    commands: list[str] = Field(min_length=1)
    #: Relative to the bundle's root, in the files `mlops bundle deploy` uploads.
    project_directory: str = ""
    #: Absent: the run writes a BigQuery profile from catalog and schema.
    profiles_directory: str | None = None
    #: BigQuery project. Absent: gcp.project.
    catalog: str | None = None
    #: BigQuery dataset.
    schema_: str | None = Field(default=None, alias="schema")
    #: gs:// prefix keeping the last command's run_results.json and manifest.json, restored
    #: into target/ first, so a later `dbt retry` task re-runs only what failed (a GCP
    #: extension: Databricks' dbt_task has none).
    state_location: str | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _runnable(self):
        if any(not c.split() or c.split()[0] != "dbt" for c in self.commands):
            raise ValueError("every dbt_task command starts with dbt")
        if self.state_location is not None and not self.state_location.startswith("gs://"):
            raise ValueError("dbt_task state_location is a gs:// prefix")
        if self.profiles_directory is None and not self.schema_:
            raise ValueError("dbt_task needs schema, or profiles_directory with its own profile")
        return self


class ConditionTask(_Strict):
    op: Literal["EQUAL_TO", "NOT_EQUAL", "GREATER_THAN", "GREATER_THAN_OR_EQUAL", "LESS_THAN",
                "LESS_THAN_OR_EQUAL"]
    left: str
    right: str


class RunJobTask(_Strict):
    #: `${resources.jobs.<name>.id}`, or the job's name.
    job_id: str
    job_parameters: dict[str, str] = {}


class VertexPipelineTask(_Strict):
    """Runs the Vertex pipeline last published for the target: a deploy publishes, a run
    trains. Deploy compiles python_file's function (gcpbricks.pipelines), or runs
    `build`, which writes the spec and its parameters itself."""

    #: A bundle file whose `function` is a @dsl.pipeline, compiled at deploy.
    python_file: str | None = None
    function: str = "pipeline"
    #: The parameter values published with a compiled pipeline.
    base_parameters: dict[str, Any] = {}
    #: Or: writes {out_dir}/<pipeline>_pipeline.json and <pipeline>_params.json for TARGET
    #: (set in its environment). Placeholders: {out_dir}, {image}, {target}, {version}.
    build: str | None = None
    #: Where the published versions and current.json live, under the bucket and the target's
    #: storage prefix. Absent: .bundle/<bundle>/pipelines/<job>-<task_key>.
    spec_prefix: str | None = None
    #: Which entry of current.json this task runs.
    pipeline: str = "train"
    #: Pipeline parameter values over the published ones; `{{...}}` allowed, empty skipped.
    parameters: dict[str, str] = {}

    @model_validator(mode="after")
    def _one_source(self):
        if (self.python_file is None) == (self.build is None):
            raise ValueError("vertex_pipeline_task takes python_file or build, exactly one")
        if self.build and self.spec_prefix is None:
            raise ValueError("vertex_pipeline_task with build needs spec_prefix")
        if self.python_file and not runs_bundle_code(self.python_file):
            raise ValueError(f"vertex_pipeline_task python_file is a path in the bundle, not {self.python_file!r}")
        if self.build and self.base_parameters:
            raise ValueError("base_parameters go with python_file; a build writes its own parameters")
        return self


#: A notebook_task's workspace notebooks: paths under it live under the bucket's WORKSPACE_PREFIX.
WORKSPACE_ROOT = "/Workspace/"
WORKSPACE_PREFIX = "workspace/"
#: The Colab runtime template a notebook_task runs on unless it names another.
NOTEBOOK_TEMPLATE = "Default"


class NotebookTask(_Strict):
    """Databricks' notebook_task, run by Colab Enterprise's notebook execution: the notebook
    (an .ipynb in the bundle, which deploy uploads, or a console workspace one under
    /Workspace/) gets its base_parameters as a cell after the one tagged `parameters`
    (papermill's convention, or first), and its executed copy is the task's output. It runs
    on a Colab runtime template's machine, as the team's notebooks do interactively."""

    notebook_path: str
    #: A Colab Enterprise runtime template's display name (a GCP extension): its machine,
    #: disk and network run the notebook.
    runtime_template: str = NOTEBOOK_TEMPLATE
    #: Python variables the notebook reads; `{{...}}` allowed, empty skipped.
    base_parameters: dict[str, str] = {}
    #: Databricks' source: GIT reads the bundle's file, WORKSPACE a /Workspace/ path; absent:
    #: by the path.
    source: Literal["GIT", "WORKSPACE"] | None = None

    @model_validator(mode="after")
    def _path(self):
        if not self.notebook_path.endswith(".ipynb"):
            raise ValueError(f"notebook_task runs an .ipynb notebook, not {self.notebook_path!r}")
        in_workspace = self.notebook_path.startswith(WORKSPACE_ROOT)
        if self.source and (self.source == "WORKSPACE") != in_workspace:
            raise ValueError(f"notebook_task source {self.source}: a {'/Workspace/ path' if self.source == 'WORKSPACE' else 'bundle path'}, "
                             f"not {self.notebook_path!r}")
        if not in_workspace and self.notebook_path.startswith("/"):
            raise ValueError(f"notebook_task notebook_path is a bundle path or under {WORKSPACE_ROOT}, not {self.notebook_path!r}")
        for name in self.base_parameters:
            if not name.isidentifier():
                raise ValueError(f"notebook_task base_parameters name Python variables, not {name!r}")
        return self

    @property
    def in_workspace(self) -> bool:
        return self.notebook_path.startswith(WORKSPACE_ROOT)


class VertexBatchPrediction(_Strict):
    """A served model scored by a Vertex AI batch prediction job over BigQuery instead of a
    Cloud Run job: input_table's rows in the window in, `prediction_expr` of Vertex's output
    out, into output_table, replaced each run: this run's predictions, for a later task."""

    #: The Vertex registered model's display name, e.g. ${resources.registered_models.x.name}.
    registered_model: str
    alias: str | None = None
    version: str | None = None
    machine_type: str = "n1-standard-4"
    starting_replica_count: int = Field(default=1, ge=1)
    max_replica_count: int = Field(default=1, ge=1)
    #: The date column the window is read on, in input_table and output_table both.
    date_col: str = Field(pattern=r"^\w+$")
    #: "project.dataset.table" of the feature rows, read where date_col is in the window.
    input_table: str = Field(pattern=r"^[\w-]+\.[\w-]+\.[\w-]+$")
    #: "project.dataset.table" this run's predictions replace: the entity columns,
    #: date_col, prediction_col, model_id and scored_at.
    output_table: str = Field(pattern=r"^[\w-]+\.[\w-]+\.[\w-]+$")
    prediction_col: str = Field(pattern=r"^\w+$")
    entity_id_columns: list[str] = Field(min_length=1)
    #: How Vertex sends each row: "object" (column: value) or "array" (included_fields'
    #: values in order, as Vertex's prebuilt containers take them).
    instance_type: Literal["object", "array"] = "object"
    included_fields: list[str] = []
    excluded_fields: list[str] = []
    #: A BigQuery expression over Vertex's output row that yields the prediction.
    prediction_expr: str = "prediction"

    @model_validator(mode="after")
    def _pick_version(self):
        if self.alias and self.version:
            raise ValueError("vertex_batch_prediction takes alias or version, not both")
        if not (self.alias or self.version):
            self.alias = "champion"
        if self.max_replica_count < self.starting_replica_count:
            raise ValueError("vertex_batch_prediction: max_replica_count below starting_replica_count")
        if self.instance_type == "array" and not self.included_fields:
            raise ValueError("vertex_batch_prediction: instance_type array needs included_fields, in order")
        return self


class ServeWindow(_Strict):
    """A served model's date window, days from the run's date (its "today", the
    task's utc_offset_hours), both included: {from_days: -14} is window_days: 14."""

    from_days: int
    to_days: int = 0

    @model_validator(mode="after")
    def _ordered(self):
        if self.from_days > self.to_days:
            raise ValueError(f"window: from_days {self.from_days} is after to_days {self.to_days}")
        return self


class ServeOutput(_Strict):
    """How a served model's job writes its predictions (gcpbricks.batch_serve.write):
    the job reads it as window().output."""

    mode: Literal["replace_window", "append", "insert_new", "merge"] = "replace_window"
    #: What insert_new and merge match a row by.
    keys: list[str] = []
    #: {column: what goes there}, set on every row: run_date is the run's date; data_date is
    #: the data's, as the job tells it to window's caller.
    stamps: dict[str, Literal["run_date", "data_date"]] = {}
    #: append: a scheduled run writes only the rows past their group's latest date in the
    #: table (batch_serve.watermarks), grouped by these columns; a backfill run of the window
    #: writes as insert_new by `keys` instead.
    watermark: list[str] | None = None
    #: The table's partition column (default: the date column) and clustering (default
    #: model_id), set when the first write creates it.
    partition_by: str | None = None
    cluster_by: list[str] | None = None

    @model_validator(mode="after")
    def _keyed(self):
        if self.mode in ("insert_new", "merge") and not self.keys:
            raise ValueError(f"output mode {self.mode}: give the keys it writes by")
        if self.watermark is not None and (self.mode != "append" or not self.keys):
            raise ValueError("output watermark: only for mode append, with the keys a backfill writes by")
        return self


class ServedModel(_Strict):
    model: str
    #: What scores it: one of the job's job_clusters, whose Cloud Run job the platform
    #: creates, as a Databricks task runs on its cluster; or an existing Cloud Run job by name
    #: (a non-prod target runs "<job>-<target>"). It gets the window as env
    #: (gcpbricks.batch_serve.window()) and does what it likes with its predictions.
    job_cluster_key: str | None = None
    job: str | None = None
    #: Or a Vertex AI batch prediction job, sized by machine_type.
    vertex_batch_prediction: VertexBatchPrediction | None = None
    #: With job_cluster_key: the bundle file that scores it, run from the deployed files on
    #: the cluster's image, in a Cloud Run job of its own. Absent: the image's own command.
    python_file: str | None = None
    parameters: list[str] = []
    #: This model's own daily window. Absent -> the task's window_days.
    window_days: int | None = Field(default=None, gt=0)
    #: Or a window offset from the run's date.
    window: ServeWindow | None = None
    #: How its job writes; absent, replace_window.
    output: ServeOutput | None = None

    @model_validator(mode="after")
    def _one_runner(self):
        runners = [k for k in ("job_cluster_key", "job", "vertex_batch_prediction") if getattr(self, k)]
        if len(runners) != 1:
            raise ValueError(f"served model {self.model!r}: give one of job_cluster_key, job or "
                             f"vertex_batch_prediction, not {runners or 'none'}")
        if (self.python_file or self.parameters) and not self.job_cluster_key:
            raise ValueError(f"served model {self.model!r}: python_file runs on a job_cluster_key")
        if self.python_file and not runs_bundle_code(self.python_file):
            raise ValueError(f"served model {self.model!r}: python_file is a path in the bundle, not {self.python_file!r}")
        if self.window and self.window_days:
            raise ValueError(f"served model {self.model!r}: give window or window_days, not both")
        if self.output and self.vertex_batch_prediction:
            raise ValueError(f"served model {self.model!r}: a vertex_batch_prediction writes its output_table")
        return self

    def offsets(self, task_window_days: int) -> tuple[int, int]:
        """(from_days, to_days) of its window from the run's date."""
        if self.window:
            return self.window.from_days, self.window.to_days
        return -(self.window_days or task_window_days), 0


class BatchServeTask(_Strict):
    """Runs every model over its window, in parallel, and fails if any failed: its date in
    [today - window_days, today], or a model's own window. It reads nothing back: what a model
    does with its predictions is its own code (gcpbricks.batch_serve's building blocks)."""

    models: list[ServedModel] = Field(min_length=1)
    #: Every model's window without its own.
    window_days: int | None = Field(default=None, gt=0)
    utc_offset_hours: int = 7
    #: One model's limit.
    max_hours: float = Field(default=2, gt=0)
    #: Run options (from_date, to_date, only_models); `{{...}}` allowed, empty skipped.
    parameters: dict[str, str] = {}

    @model_validator(mode="before")
    @classmethod
    def _removed(cls, data):
        gone = sorted(set(data) & {"feature_table", "runs_table", "wait_hours", "max_window_days",
                                   "min_keep_ratio", "stage_prefix", "claims_prefix"}) if isinstance(data, dict) else []
        if gone:
            raise ValueError(f"batch_serve_task no longer takes {gone}: a model's job writes its own "
                             "predictions (batch_serve.write_window), and a table_update trigger "
                             "waits for features")
        return data

    @model_validator(mode="after")
    def _known_options(self):
        bare = [m.model for m in self.models if not (m.window or m.window_days)]
        if bare and self.window_days is None:
            raise ValueError(f"batch_serve_task: window_days, or a window for {bare}")
        unknown = sorted(set(self.parameters) - BATCH_SERVE_PARAMS)
        if unknown:
            raise ValueError(f"batch_serve_task parameters are {sorted(BATCH_SERVE_PARAMS)}, not {unknown}")
        return self


# --- notifications, health, queue and triggers --------------------------------------------

LABEL_KEY = r"[a-z][a-z0-9_-]{0,62}"
LABEL_VALUE = r"[a-z0-9_-]{0,63}"


class Webhook(_Strict):
    #: A Cloud Monitoring notification channel: "projects/<p>/notificationChannels/<id>",
    #: or the bare <id> in gcp.project.
    id: str


def _no_events(model: BaseModel, names: tuple[str, ...]) -> None:
    used = [n for n in names if getattr(model, n)]
    if used:
        raise ValueError(f"{used}: not supported by gcpbricks (no streaming or "
                         f"maintenance events)")


_NEVER = ("on_streaming_backlog_exceeded", "on_maintenance_start", "on_maintenance_complete")


class TaskEmailNotifications(_Strict):
    on_start: list[str] = []
    on_success: list[str] = []
    on_failure: list[str] = []
    on_streaming_backlog_exceeded: list[str] = []
    on_maintenance_start: list[str] = []
    on_maintenance_complete: list[str] = []
    #: Deprecated in Databricks for notification_settings.no_alert_for_skipped_runs.
    no_alert_for_skipped_runs: bool = False

    @model_validator(mode="after")
    def _supported(self):
        _no_events(self, _NEVER)
        return self


class JobEmailNotifications(TaskEmailNotifications):
    on_duration_warning_threshold_exceeded: list[str] = []


class TaskWebhookNotifications(_Strict):
    on_start: list[Webhook] = []
    on_success: list[Webhook] = []
    on_failure: list[Webhook] = []
    on_streaming_backlog_exceeded: list[Webhook] = []
    on_maintenance_start: list[Webhook] = []
    on_maintenance_complete: list[Webhook] = []

    @model_validator(mode="after")
    def _supported(self):
        _no_events(self, _NEVER)
        return self


class JobWebhookNotifications(TaskWebhookNotifications):
    on_duration_warning_threshold_exceeded: list[Webhook] = []


class JobNotificationSettings(_Strict):
    no_alert_for_skipped_runs: bool = False
    #: Accepted; a cancelled execution logs no outcome, so it never alerts.
    no_alert_for_canceled_runs: bool = False


class TaskNotificationSettings(JobNotificationSettings):
    #: Accepted: a task's on_start and on_failure fire once per task, never per retry.
    alert_on_last_attempt: bool = False


class JobsHealthRule(_Strict):
    metric: Literal["RUN_DURATION_SECONDS"]
    op: Literal["GREATER_THAN"]
    value: int = Field(gt=0)


class JobsHealthRules(_Strict):
    rules: list[JobsHealthRule] = []


class QueueSettings(_Strict):
    enabled: bool


class PeriodicTriggerConfiguration(_Strict):
    interval: int = Field(gt=0)
    unit: Literal["MINUTES", "HOURS", "DAYS", "WEEKS"]


class TableUpdateTriggerConfiguration(_Strict):
    #: Databricks' catalog.schema.table: a BigQuery "project.dataset.table", or a lakehouse
    #: Iceberg table in a BigLake catalog, "project.catalog.namespace.table".
    table_names: list[str] = Field(min_length=1, max_length=10)
    condition: Literal["ANY_UPDATED", "ALL_UPDATED"] = "ANY_UPDATED"
    min_time_between_triggers_seconds: int = Field(default=0, ge=0)
    wait_after_last_change_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _bigquery_names(self):
        bad = [t for t in self.table_names if not re.fullmatch(r"[\w-]+(\.[\w-]+){2,3}", t)]
        if bad:
            raise ValueError(f"table_names {bad}: use project.dataset.table, or "
                             f"project.catalog.namespace.table for a BigLake Iceberg table")
        return self


class FileArrivalTriggerConfiguration(_Strict):
    #: gs://bucket/prefix/: a new or rewritten object under it starts a run.
    url: str = Field(pattern=r"^gs://[\w.-]+/")
    min_time_between_triggers_seconds: int = Field(default=0, ge=0)
    wait_after_last_change_seconds: int = Field(default=0, ge=0)


class TriggerSettings(_Strict):
    pause_status: Literal["PAUSED", "UNPAUSED"] | None = None
    periodic: PeriodicTriggerConfiguration | None = None
    table_update: TableUpdateTriggerConfiguration | None = None
    file_arrival: FileArrivalTriggerConfiguration | None = None

    @model_validator(mode="after")
    def _one(self):
        kinds = [k for k in ("periodic", "table_update", "file_arrival") if getattr(self, k)]
        if len(kinds) != 1:
            raise ValueError(f"trigger sets {kinds or 'nothing'}: exactly one of periodic, "
                             f"table_update, file_arrival")
        return self

    @property
    def kind(self) -> str:
        return next(k for k in ("periodic", "table_update", "file_arrival") if getattr(self, k))


TASK_TYPES = ("python_wheel_task", "spark_python_task", "dbt_task", "condition_task",
              "run_job_task", "for_each_task", "vertex_pipeline_task", "batch_serve_task", "notebook_task")


class _TaskBase(_Strict):
    task_key: str = Field(pattern=KEY)
    description: str | None = None
    job_cluster_key: str | None = None
    #: One of the job's environments. On a vertex_pipeline_task, its image is the build's
    #: {image}; elsewhere a task takes its environment from its job cluster.
    environment_key: str | None = None
    max_retries: int = Field(default=0, ge=0)
    min_retry_interval_millis: int = Field(default=0, ge=0)
    timeout_seconds: int = Field(default=0, ge=0, le=MAX_TASK_SECONDS)
    #: Retry a task that hit timeout_seconds; otherwise a timeout fails it at once.
    retry_on_timeout: bool = False
    python_wheel_task: PythonWheelTask | None = None
    spark_python_task: SparkPythonTask | None = None
    dbt_task: DbtTask | None = None
    condition_task: ConditionTask | None = None
    run_job_task: RunJobTask | None = None
    vertex_pipeline_task: VertexPipelineTask | None = None
    batch_serve_task: BatchServeTask | None = None
    notebook_task: NotebookTask | None = None

    @property
    def kind(self) -> str:
        return next(t for t in TASK_TYPES if getattr(self, t, None) is not None)

    @model_validator(mode="after")
    def _one_type(self):
        set_ = [t for t in TASK_TYPES if getattr(self, t, None) is not None]
        if len(set_) != 1:
            raise ValueError(f"task {self.task_key!r} sets {set_ or 'no task type'}: exactly one of {list(TASK_TYPES)}")
        if self.environment_key and set_ != ["vertex_pipeline_task"]:
            raise ValueError(f"task {self.task_key!r}: environment_key applies to a vertex_pipeline_task; "
                             "other tasks run on a job cluster, which names the environment_key")
        if self.kind == "notebook_task" and self.job_cluster_key:
            raise ValueError(f"task {self.task_key!r}: a notebook_task runs on a Colab runtime template "
                             "(notebook_task.runtime_template), not a job cluster")
        if self.kind in ("python_wheel_task", "spark_python_task") and not self.job_cluster_key:
            raise ValueError(f"task {self.task_key!r}: a {self.kind} needs job_cluster_key")
        return self


class ForEachInner(_TaskBase):
    pass


class ForEachTask(_Strict):
    #: A JSON array, or a `{{...}}` reference resolving to one.
    inputs: str
    concurrency: int = Field(default=1, ge=1, le=MAX_CONCURRENCY)
    task: ForEachInner

    @model_validator(mode="after")
    def _not_nested(self):
        if self.task.kind in ("for_each_task", "condition_task"):
            raise ValueError(f"for_each_task cannot run a {self.task.kind}")
        return self


class Task(_TaskBase):
    depends_on: list[Dependency] = []
    run_if: RUN_IF = "ALL_SUCCESS"
    for_each_task: ForEachTask | None = None
    email_notifications: TaskEmailNotifications | None = None
    webhook_notifications: TaskWebhookNotifications | None = None
    notification_settings: TaskNotificationSettings | None = None


class JobParameter(_Strict):
    name: str = Field(pattern=r"^[\w.-]+$")
    default: str


class CronSchedule(_Strict):
    quartz_cron_expression: str
    timezone_id: str
    pause_status: Literal["PAUSED", "UNPAUSED"] | None = None


class DockerImage(_Strict):
    url: str


class EnvironmentSpec(_Strict):
    #: pip requirement lines: pins, or -r / -c files relative to the bundle's root.
    dependencies: list[str] = []


class JobEnvironment(_Strict):
    """Databricks' job environment: what the job's code needs installed. The platform builds
    it as an image on its runtime (gcpbricks.environments)."""

    environment_key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,20}$")
    spec: EnvironmentSpec


class NewCluster(_Strict):
    #: The image to run, or environment_key: one of the job's environments, built by deploy.
    docker_image: DockerImage | None = None
    environment_key: str | None = None
    #: A GCP machine type, e.g. e2-standard-4: its vCPUs and memory size the Cloud Run job.
    node_type_id: str | None = None
    spark_env_vars: dict[str, str] = {}
    #: The Cloud Run job's parallel tasks (CLOUD_RUN_TASK_INDEX / _COUNT). Absent: one.
    num_workers: int | None = Field(default=None, ge=1, le=10000)
    #: Exact Cloud Run size instead of node_type_id (a GCP extension): vCPUs (1, 2, 4, 6, 8)
    #: and memory ("16Gi", "512Mi").
    cpu: int | None = None
    memory: str | None = Field(default=None, pattern=r"^\d+(Gi|Mi)$")

    @model_validator(mode="after")
    def _one_size(self):
        if (self.docker_image is None) == (self.environment_key is None):
            raise ValueError("new_cluster takes docker_image or environment_key, exactly one")
        if self.node_type_id and (self.cpu or self.memory):
            raise ValueError("new_cluster takes node_type_id, or cpu and memory, not both")
        if (self.cpu is None) != (self.memory is None):
            raise ValueError("new_cluster's cpu and memory go together")
        if self.cpu is not None:
            exact_resources(self.cpu, self.memory)
        return self


class JobCluster(_Strict):
    job_cluster_key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,20}$")
    new_cluster: NewCluster


class Job(_Strict):
    name: str | None = None
    description: str | None = None
    #: GCP labels on the job's Workflow and Cloud Run jobs, so lowercase, digits, - and _.
    tags: dict[str, str] = {}
    parameters: list[JobParameter] = []
    schedule: CronSchedule | None = None
    trigger: TriggerSettings | None = None
    max_concurrent_runs: int = Field(default=1, ge=1)
    #: Enabled: a run over max_concurrent_runs waits its turn (up to 48 hours) instead of
    #: being skipped.
    queue: QueueSettings | None = None
    timeout_seconds: int = Field(default=0, ge=0)
    health: JobsHealthRules | None = None
    email_notifications: JobEmailNotifications | None = None
    webhook_notifications: JobWebhookNotifications | None = None
    notification_settings: JobNotificationSettings | None = None
    job_clusters: list[JobCluster] = []
    environments: list[JobEnvironment] = []
    tasks: list[Task] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_way_to_start(self):
        if self.schedule and self.trigger:
            raise ValueError("a job has a schedule or a trigger, not both")
        for k, v in self.tags.items():
            if not (re.fullmatch(LABEL_KEY, k) and re.fullmatch(LABEL_VALUE, v)):
                raise ValueError(f"tag {k}={v!r}: tags become GCP labels: lowercase letters, "
                                 f"digits, - and _, at most 63 characters, keys start with a letter")
        return self

    @property
    def duration_warning_seconds(self) -> int:
        return min((r.value for r in self.health.rules), default=0) if self.health else 0


class Schema(_Strict):
    """Databricks' schema: a BigQuery dataset, catalog_name being its GCP project."""

    name: str = Field(pattern=r"^[A-Za-z0-9_]+$", max_length=1024)
    catalog_name: str | None = None
    comment: str | None = None
    #: BigQuery location (a GCP extension: Unity Catalog has none).
    location: str = "us-central1"


class Dashboard(_Strict):
    """A dashboard the console renders (gcpbricks.dashboards): file_path is its spec,
    YAML or JSON, relative to the bundle's root."""

    display_name: str
    file_path: str


class Resources(_Strict):
    jobs: dict[str, Job] = {}
    experiments: dict[str, Experiment] = {}
    registered_models: dict[str, RegisteredModel] = {}
    model_serving_endpoints: dict[str, ModelServingEndpoint] = {}
    schemas: dict[str, Schema] = {}
    dashboards: dict[str, Dashboard] = {}


class Bundle(_Strict):
    bundle: BundleInfo
    include: list[str] = []
    gcp: Gcp
    run_as: RunAs
    variables: dict[str, Variable] = {}
    sync: Sync = Sync()
    targets: dict[str, Target] = Field(min_length=1)
    resources: Resources = Resources()

    @model_validator(mode="after")
    def _jobs_hold_together(self):
        slugs = [job_slug(n) for n in self.resources.jobs]
        if len(slugs) != len(set(slugs)):
            raise ValueError(f"job keys collide once _ becomes - in GCP names: {sorted(self.resources.jobs)}")
        for name, job in self.resources.jobs.items():
            if not re.match(KEY, name):
                raise ValueError(f"job {name!r}: names match {KEY}")
            _check_job(self, name, job)
        _check_run_job_cycles(self)
        for key, m in self.resources.registered_models.items():
            if m.catalog_name not in (None, self.gcp.project):
                raise ValueError(f"registered model {key!r}: catalog_name is the GCP project, "
                                 f"{self.gcp.project!r}; models in other projects are not supported")
        for kind in ("experiments", "model_serving_endpoints"):
            seen = [r.name for r in getattr(self.resources, kind).values()]
            if len(seen) != len(set(seen)):
                raise ValueError(f"{kind}: two resources share a name: {sorted(seen)}")
        if sum(t.default for t in self.targets.values()) > 1:
            raise ValueError("at most one target sets default: true")
        return self


class Loaded(BaseModel):
    """A bundle resolved for one target."""

    bundle: Bundle
    target: str
    root: Path
    #: The files version a deploy uploads: every task that reads the bundle's files reads that
    #: one, not whichever deploy is current when it starts. None: the current one.
    files_version: str | None = None
    #: {job key: {"since", "plan"}}: jobs a deploy keeps though the bundle no longer declares
    #: them (retired_jobs), each with the plan fragment its last deploy rendered.
    retired_jobs: dict[str, dict] = {}

    @property
    def mode(self) -> str | None:
        return self.bundle.targets[self.target].mode


# --- checks -------------------------------------------------------------------------------

def _refs(value: str) -> set[str]:
    return set(re.findall(r"\{\{\s*([^}]+?)\s*\}\}", value))


def _strings(task: _TaskBase) -> list[str]:
    spec = getattr(task, task.kind)
    data = spec.model_dump(by_alias=True) if spec else {}
    data.pop("task", None)  # a for_each's inner task is checked on its own
    out: list[str] = []

    def walk(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(data)
    return out


def _check_refs(job_name: str, task: _TaskBase, params: set[str], inside_for_each: bool,
                extra: set[str] = frozenset()):
    allowed = RUN_VALUES | extra | {f"job.parameters.{p}" for p in params}
    if inside_for_each:
        allowed |= {"input"}
    for s in _strings(task):
        unknown = sorted(_refs(s) - allowed)
        if unknown:
            raise ValueError(f"job {job_name!r} task {task.task_key!r}: unsupported value "
                             f"reference(s) {unknown}; supported: {sorted(allowed)}")


def _check_job(b: Bundle, name: str, job: Job):
    keys = [t.task_key for t in job.tasks]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ValueError(f"job {name!r}: task_key repeated: {dupes}")
    by_key = {t.task_key: t for t in job.tasks}
    clusters = {c.job_cluster_key for c in job.job_clusters}
    envs = [e.environment_key for e in job.environments]
    if len(envs) != len(set(envs)):
        raise ValueError(f"job {name!r}: environment_key repeated: {sorted({e for e in envs if envs.count(e) > 1})}")
    for c in job.job_clusters:
        if c.new_cluster.environment_key and c.new_cluster.environment_key not in envs:
            raise ValueError(f"job {name!r} cluster {c.job_cluster_key!r}: no environment_key "
                             f"{c.new_cluster.environment_key!r} in environments")
    params = {p.name for p in job.parameters}
    fired = set(trigger_values(job))
    for p in job.parameters:
        _check_param_default(name, p, fired)
    for t in job.tasks:
        inner = [t.for_each_task.task] if t.for_each_task else []
        for task in [t, *inner]:
            if task.job_cluster_key and task.job_cluster_key not in clusters:
                raise ValueError(f"job {name!r} task {task.task_key!r}: no job_cluster_key "
                                 f"{task.job_cluster_key!r} in job_clusters")
            if task.environment_key and task.environment_key not in envs:
                raise ValueError(f"job {name!r} task {task.task_key!r}: no environment_key "
                                 f"{task.environment_key!r} in environments")
            _check_refs(name, task, params, inside_for_each=task is not t, extra=fired)
            for m in (task.batch_serve_task.models if task.batch_serve_task else []):
                if m.job_cluster_key and m.job_cluster_key not in clusters:
                    raise ValueError(f"job {name!r} task {task.task_key!r}: served model {m.model!r} "
                                     f"names job_cluster_key {m.job_cluster_key!r}, not in job_clusters")
            if (task.run_job_task and local_job(b, task.run_job_task.job_id) is None
                    and not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", task.run_job_task.job_id)):
                raise ValueError(f"job {name!r} task {task.task_key!r}: run_job_task job_id "
                                 f"{task.run_job_task.job_id!r} is neither a job here nor another "
                                 f"bundle's job Workflow name (use a lookup variable)")
        for d in t.depends_on:
            if d.task_key not in by_key:
                raise ValueError(f"job {name!r} task {t.task_key!r} depends on unknown task {d.task_key!r}")
            if d.outcome and not by_key[d.task_key].condition_task:
                raise ValueError(f"job {name!r} task {t.task_key!r}: outcome applies only to a "
                                 f"condition_task dependency")
        if t.run_if != "ALL_SUCCESS" and not t.depends_on:
            raise ValueError(f"job {name!r} task {t.task_key!r}: run_if needs depends_on")
    levels(job)  # raises on a cycle
    if job.trigger and job.trigger.kind == "periodic":
        periodic_cron(job.trigger.periodic.interval, job.trigger.periodic.unit)
    if job.schedule:
        unix_cron(job.schedule.quartz_cron_expression)
    for c in job.job_clusters:
        if c.new_cluster.node_type_id:
            cloud_run_resources(c.new_cluster.node_type_id)
    files = job.trigger is not None and job.trigger.kind == "file_arrival"
    names = [n for tg in b.targets
             for n in [workflow_name(b, name, tg), *[run_job_name(b, name, c, tg) for c in clusters],
                       *([wake_workflow_name(b, name, tg)] if files else [])]]
    for n in names:
        if len(n) > 63:
            raise ValueError(f"job {name!r}: resource name {n!r} is over 63 characters")


def _check_param_default(job: str, p: JobParameter, extra: set[str] = frozenset()):
    unknown = sorted(_refs(p.default) - RUN_VALUES - extra)
    if unknown:
        raise ValueError(f"job {job!r} parameter {p.name!r}: unsupported reference(s) {unknown}")


def _check_run_job_cycles(b: Bundle):
    """Within the bundle; a cycle through another bundle can't be seen from here."""
    calls = {n: {local_job(b, t.run_job_task.job_id) for t in j.tasks if t.run_job_task} - {None}
             for n, j in b.resources.jobs.items()}

    def visit(n: str, path: list[str]):
        if n in path:
            raise ValueError(f"run_job_task cycle: {' -> '.join([*path, n])}")
        for m in calls[n]:
            visit(m, [*path, n])
    for n in calls:
        visit(n, [])


def levels(job: Job) -> list[list[str]]:
    """Tasks grouped so each one's dependencies are in earlier groups: a Workflow runs one
    group at a time, its tasks in parallel."""
    deps = {t.task_key: {d.task_key for d in t.depends_on} for t in job.tasks}
    done: set[str] = set()
    out: list[list[str]] = []
    while len(done) < len(deps):
        ready = sorted(k for k, ds in deps.items() if k not in done and ds <= done)
        if not ready:
            raise ValueError(f"depends_on cycle among {sorted(set(deps) - done)}")
        out.append(ready)
        done |= set(ready)
    return out


# --- loading ------------------------------------------------------------------------------

#: Lists DAB merges by key when a target overrides them, instead of replacing them.
_KEYED_LISTS = {"tasks": "task_key", "job_clusters": "job_cluster_key", "parameters": "name"}


def _merge(base, over, key: str | None = None):
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base[k], v, k) if k in base else copy.deepcopy(v)
        return out
    if isinstance(base, list) and isinstance(over, list) and key in _KEYED_LISTS:
        ident = _KEYED_LISTS[key]
        out = [copy.deepcopy(x) for x in base]
        index = {x.get(ident): i for i, x in enumerate(out) if isinstance(x, dict)}
        for x in over:
            if isinstance(x, dict) and x.get(ident) in index:
                out[index[x[ident]]] = _merge(out[index[x[ident]]], x)
            else:
                out.append(copy.deepcopy(x))
        return out
    return copy.deepcopy(over)


def _read(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise TypeError(f"{path}: not a mapping")
    return data


def _with_includes(root: Path, raw: dict) -> dict:
    out = dict(raw)
    for pattern in raw.get("include", []):
        matched = sorted(glob.glob(str(root / pattern), recursive=True))
        if not matched:
            raise ValueError(f"include {pattern!r} matches no file")
        for f in matched:
            part = _read(Path(f))
            if "bundle" in part or "include" in part:
                raise ValueError(f"{f}: an included file cannot set bundle or include")
            for name in (part.get("resources") or {}).get("jobs") or {}:
                if name in ((out.get("resources") or {}).get("jobs") or {}):
                    raise ValueError(f"{f}: job {name!r} is defined twice")
            out = _merge(out, part)
    return out


def default_target(raw: dict) -> str:
    targets = raw.get("targets") or {}
    chosen = [n for n, t in targets.items() if (t or {}).get("default")]
    if chosen:
        return chosen[0]
    if len(targets) == 1:
        return next(iter(targets))
    raise ValueError("no -t given and no target sets default: true")


_SUB = re.compile(r"\$\{([^}]+)\}")
_RESOURCE_REF = re.compile(r"resources\.(\w+)\.([^.]+)\.(id|name)")


def _substitute(node, ctx: dict[str, object], refs: dict[str, str] | None):
    """refs None: leave ${resources...} for a second pass, once names are known."""
    if isinstance(node, dict):
        return {k: _substitute(v, ctx, refs) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, ctx, refs) for v in node]
    if not isinstance(node, str):
        return node
    whole = _SUB.fullmatch(node)
    if whole:  # a lone reference keeps its value's type: lists, numbers
        return _lookup(whole.group(1), ctx, refs)
    return _SUB.sub(lambda m: str(_lookup(m.group(1), ctx, refs)), node)


def _lookup(ref: str, ctx: dict[str, object], refs: dict[str, str] | None):
    ref = ref.strip()
    if ref in ctx:
        return ctx[ref]
    if _RESOURCE_REF.fullmatch(ref):
        if refs is None:
            return "${" + ref + "}"
        if ref in refs:
            return refs[ref]
    raise ValueError(f"unknown reference ${{{ref}}}")


def resource_names(resources: dict, target: str, bundle: str) -> dict[str, dict[str, str]]:
    """Each resource's name on `target`, by kind and key: what ${resources.<kind>.<key>.name}
    resolves to, and what deploy creates. Models take "_<target>", as demo_model names
    them; the rest "-<target>"."""
    def get(kind: str):
        return (resources.get(kind) or {}).items()
    return {
        "jobs": {k: f"{bundle}-{job_slug(k)}" for k, _ in get("jobs")},
        "experiments": {k: f"{(v or {}).get('name', k)}{name_suffix(target)}" for k, v in get("experiments")},
        "registered_models": {k: f"{(v or {}).get('name') or k}{name_suffix(target, '_')}"
                              for k, v in get("registered_models")},
        "model_serving_endpoints": {k: f"{(v or {}).get('name', k)}{name_suffix(target)}"
                                    for k, v in get("model_serving_endpoints")},
        # BigQuery dataset ids take no "-".
        "schemas": {k: f"{(v or {}).get('name', k)}{name_suffix(target, '_')}" for k, v in get("schemas")},
    }


def load(path: str | Path = "mlops.yaml", target: str | None = None,
         variables: dict[str, str] | None = None) -> Loaded:
    """The bundle at `path` for `target` (default: the target with default: true), with
    variables from, lowest first: defaults, the target, BUNDLE_VAR_<name>, `variables`."""
    path = Path(path)
    root = path.resolve().parent
    raw = _with_includes(root, _read(path))
    target = target or default_target(raw)
    targets = raw.get("targets") or {}
    if target not in targets:
        raise ValueError(f"target {target!r} is not one of {sorted(targets)}")
    t = targets[target] or {}
    merged = dict(raw)
    merged["resources"] = _merge(raw.get("resources") or {}, t.get("resources") or {}, "resources")
    if t.get("gcp"):
        merged["gcp"] = _merge(raw.get("gcp") or {}, t["gcp"])
    if t.get("run_as"):
        merged["run_as"] = t["run_as"]

    declared = raw.get("variables") or {}
    values = {k: (v or {}).get("default") for k, v in declared.items()}
    for k, v in declared.items():
        lookup = (v or {}).get("lookup")
        if lookup:
            unsupported = sorted(set(lookup) - {"job"})
            if unsupported:
                raise ValueError(f"variable {k!r}: lookup {unsupported} is not supported by "
                                 f"gcpbricks; only lookup.job")
            values[k] = job_slug(str(lookup.get("job", "")))
    values |= t.get("variables") or {}
    values |= {k[len("BUNDLE_VAR_"):]: v for k, v in os.environ.items()
               if k.startswith("BUNDLE_VAR_") and k[len("BUNDLE_VAR_"):] in declared}
    values |= variables or {}
    undeclared = sorted(set(values) - set(declared))
    if undeclared:
        raise ValueError(f"variables not declared: {undeclared}")
    missing = sorted(k for k, v in values.items() if v is None)
    if missing:
        raise ValueError(f"variables without a value: {missing}")

    name = (raw.get("bundle") or {}).get("name")
    ctx: dict[str, object] = {f"var.{k}": v for k, v in values.items()}
    ctx |= {"bundle.name": name, "bundle.target": target}
    first = _substitute({k: v for k, v in merged.items() if k != "targets"}, ctx, None)
    names = resource_names(first.get("resources") or {}, target, name)
    refs = {f"resources.{kind}.{key}.{field}": value
            for kind, by_key in names.items() for key, value in by_key.items()
            for field in ("id", "name")}
    resolved = _substitute(first, ctx, refs)
    resolved["targets"] = targets
    return Loaded(bundle=Bundle.model_validate(resolved), target=target, root=root)


# --- names --------------------------------------------------------------------------------

def job_slug(job: str) -> str:
    """A job key in GCP names: Cloud Run and Eventarc take no underscores."""
    return job.replace("_", "-")


def job_ref(b: Bundle, job: str) -> str:
    """A job's id, as ${resources.jobs.<job>.id} and another bundle's lookup.job name it:
    its Workflow name on prod."""
    return f"{b.bundle.name}-{job_slug(job)}"


def local_job(b: Bundle, job_id: str) -> str | None:
    """The key of this bundle's job a run_job_task's job_id names, or None: another
    bundle's."""
    return next((k for k in b.resources.jobs if job_id in (k, job_ref(b, k))), None)


def workflow_name(b: Bundle, job: str, target: str) -> str:
    return f"{job_ref(b, job)}{name_suffix(target)}"


def task_workflow_name(b: Bundle, kind: str, target: str) -> str:
    """The Workflow a vertex_pipeline_task, batch_serve_task or notebook_task calls, one per kind."""
    return f"{b.bundle.name}-{kind.removesuffix('_task').replace('_', '-')}{name_suffix(target)}"


def run_job_name(b: Bundle, job: str, cluster: str, target: str) -> str:
    """The Cloud Run job a job cluster becomes."""
    return f"{job_ref(b, job)}-{cluster}{name_suffix(target)}"


def served_job_name(b: Bundle, job: str, m: ServedModel, target: str) -> str:
    """The Cloud Run job a served model runs: its own when it runs a bundle file, else its
    cluster's, else the existing job it names."""
    if m.python_file:
        return f"{job_ref(b, job)}-{job_slug(m.model)}{name_suffix(target)}"
    if m.job_cluster_key:
        return run_job_name(b, job, m.job_cluster_key, target)
    return m.job + name_suffix(target)


#: The project's one Cloud Run job that sends a failed run's email (deploy/console/dev).
NOTIFY_JOB = "mlops-notify"
#: Who hears of a failed run when its job names no one in email_notifications.on_failure.
DEFAULT_FAILURE_EMAILS = ["admin@example.com"]


def failure_mail(lb: Loaded, name: str) -> dict:
    """{run_job, to}: whom the platform emails itself when the job's run fails, the job's
    on_failure emails or DEFAULT_FAILURE_EMAILS; {} when the bundle's gcp.mail is off."""
    if not lb.bundle.gcp.mail:
        return {}
    em = lb.bundle.resources.jobs[name].email_notifications
    return {"run_job": NOTIFY_JOB, "to": sorted(set((em.on_failure if em else None) or DEFAULT_FAILURE_EMAILS))}


def dbt_job_name(b: Bundle, target: str) -> str:
    """The Cloud Run job, on the platform's dbt image, that runs dbt_tasks without a cluster."""
    return f"{b.bundle.name}-dbt{name_suffix(target)}"


def files_uri(b: Bundle, target: str) -> str:
    """Where `mlops bundle deploy` uploads the bundle's files, for dbt_tasks and bundle
    python files to read, and `mlops bundle redeploy` to start from."""
    return files_uri_of(b.gcp.bucket, target, b.bundle.name)


def files_uri_of(bucket: str, target: str, bundle: str) -> str:
    return f"gs://{bucket}/{storage_prefix(target)}.bundle/{bundle}/files"


def needs_files(job: Job) -> bool:
    """Whether a run reads the bundle's deployed files."""
    for t in _all_tasks(job):
        if t.kind == "dbt_task":
            return True
        if t.spark_python_task and runs_bundle_code(t.spark_python_task.python_file):
            return True
        if t.batch_serve_task and any(m.python_file for m in t.batch_serve_task.models):
            return True
        if t.vertex_pipeline_task and t.vertex_pipeline_task.python_file:
            return True
    return False


def pipeline_params(lb: Loaded, job: str, task: _TaskBase) -> dict:
    """A compiled pipeline's published parameters (gcpbricks.publish): its run's
    display name, labels, root and service account, and base_parameters as its values."""
    b, t = lb.bundle, lb.target
    name = f"{b.bundle.name}-{job_slug(job)}-{task.task_key.replace('_', '-')}"
    return {"display_name": name[:128],
            "labels": bundle_labels(lb) | {"mlops_task": task.task_key.replace("_", "-")[:63]},
            "pipeline_root": f"gs://{b.gcp.bucket}/{storage_prefix(t)}.bundle/{b.bundle.name}/pipeline_root/"
                             f"{job_slug(job)}-{task.task_key}",
            "service_account": b.run_as.service_principal_name,
            "parameter_values": dict(task.vertex_pipeline_task.base_parameters)}


def pipeline_prefix(b: Bundle, job: str, task: _TaskBase) -> str:
    """Where a vertex_pipeline_task's versions and current.json live, under the target prefix."""
    return task.vertex_pipeline_task.spec_prefix or f".bundle/{b.bundle.name}/pipelines/{job_slug(job)}-{task.task_key}"


def definition_object(b: Bundle, job: str, target: str) -> str:
    """Where the module uploads a job's definition (job_definition), in the bucket: the
    Workflow reads it at the start of each run, so its argument stays a pointer under
    Workflows' 32 KB limit."""
    return f"{storage_prefix(target)}.bundle/{b.bundle.name}/jobs/{job}.json"


def trigger_workflow_name(b: Bundle, target: str) -> str:
    """The Workflow that checks every table_update / file_arrival trigger of the bundle."""
    return f"{b.bundle.name}-trigger{name_suffix(target)}"


def trigger_state_object(b: Bundle, job: str, target: str) -> str:
    return f"{storage_prefix(target)}.bundle/{b.bundle.name}/triggers/{job}.json"


def wake_workflow_name(b: Bundle, job: str, target: str) -> str:
    """The Workflow an Eventarc event starts to check a file_arrival trigger at once."""
    return f"{job_ref(b, job)}-wake{name_suffix(target)}"


def watchdog_workflow_name(b: Bundle, target: str) -> str:
    """The Workflow a run starts beside itself to log a duration warning (health rules)."""
    return f"{b.bundle.name}-watchdog{name_suffix(target)}"


# --- schedules and compute ----------------------------------------------------------------

def unix_cron(quartz: str) -> str:
    """Cloud Scheduler's 5-field cron for a Quartz expression (sec min hour dom mon dow
    [year]). Quartz numbers weekdays 1-7 from Sunday; cron 0-6."""
    fields = quartz.split()
    if len(fields) not in (6, 7):
        raise ValueError(f"{quartz!r}: Quartz cron has 6 or 7 fields")
    sec, minute, hour, dom, month, dow = fields[:6]
    if sec != "0":
        raise ValueError(f"{quartz!r}: seconds must be 0, Cloud Scheduler runs per minute")
    if len(fields) == 7 and fields[6] != "*":
        raise ValueError(f"{quartz!r}: a year field is not supported")
    if re.search(r"[LW#]", " ".join(fields)):
        raise ValueError(f"{quartz!r}: L, W and # are not supported")

    def shift(part: str) -> str:
        base, sep, step = part.partition("/")
        return re.sub(r"\d+", lambda m: str(int(m.group()) - 1), base) + sep + step
    dow = ",".join(shift(p) for p in dow.split(","))
    return " ".join("*" if f == "?" else f for f in (minute, hour, dom, month, dow))


def periodic_cron(interval: int, unit: str) -> str:
    """Cloud Scheduler cron for a periodic trigger, on the clock (UTC) rather than from the
    job's creation as in Databricks; intervals cron can't hit exactly are refused."""
    if unit == "MINUTES" and 60 % interval == 0:
        return "0 * * * *" if interval == 60 else f"*/{interval} * * * *"
    if unit == "HOURS" and 24 % interval == 0:
        return "0 0 * * *" if interval == 24 else f"0 */{interval} * * *"
    if (unit, interval) == ("DAYS", 1):
        return "0 0 * * *"
    if (unit, interval) == ("WEEKS", 1):
        return "0 0 * * 1"
    raise ValueError(f"periodic every {interval} {unit.lower()}: cron can run every n minutes "
                     f"(n divides 60), every n hours (n divides 24), daily or weekly")


_MEMORY_PER_CPU = {"standard": 4, "highcpu": 1, "highmem": 8}
#: Cloud Run's per-instance ceiling for each vCPU count, in GiB.
_MAX_MEMORY = {1: 4, 2: 8, 4: 16, 6: 24, 8: 32}


def exact_resources(cpu: int, memory: str) -> dict[str, str]:
    """A new_cluster's own cpu + memory, checked against what a Cloud Run job takes."""
    gib = int(memory[:-2]) / (1 if memory.endswith("Gi") else 1024)
    if cpu not in _MAX_MEMORY or gib > _MAX_MEMORY[cpu]:
        raise ValueError(f"cpu {cpu} with memory {memory} does not fit a Cloud Run job: vCPU in "
                         f"{sorted(_MAX_MEMORY)}, memory up to {_MAX_MEMORY} GiB")
    return {"cpu": str(cpu), "memory": memory}


def cloud_run_resources(node_type_id: str) -> dict[str, str]:
    m = re.fullmatch(r"[a-z0-9]+-(standard|highcpu|highmem)-(\d+)", node_type_id)
    if not m:
        raise ValueError(f"node_type_id {node_type_id!r}: use a GCP machine type like e2-standard-4")
    cpu, gib = int(m.group(2)), int(m.group(2)) * _MEMORY_PER_CPU[m.group(1)]
    if cpu not in _MAX_MEMORY or gib > _MAX_MEMORY[cpu]:
        raise ValueError(f"node_type_id {node_type_id!r} ({cpu} vCPU, {gib} GiB) does not fit a "
                         f"Cloud Run job: vCPU in {sorted(_MAX_MEMORY)}, memory up to {_MAX_MEMORY}")
    return {"cpu": str(cpu), "memory": f"{gib}Gi"}


# --- rendering ----------------------------------------------------------------------------

def _common(task: _TaskBase) -> dict:
    return {"max_retries": task.max_retries,
            "min_retry_interval_millis": task.min_retry_interval_millis,
            "timeout_seconds": task.timeout_seconds or MAX_TASK_SECONDS,
            "retry_on_timeout": task.retry_on_timeout}


#: The files version a task's container reads (task_runner), set per run by workflows/job.yaml.
FILES_VERSION_ENV = "MLOPS_FILES_VERSION"


def files_env(lb: Loaded) -> dict[str, str]:
    """What pins a container that reads the bundle's files to the deploy that rendered it. An
    older image ignores it and reads the current deploy, as it always did."""
    return {FILES_VERSION_ENV: lb.files_version} if lb.files_version else {}


def _cloud_run(run_job: str, args: list[str], env: dict[str, str] | None = None) -> dict:
    return {"type": "cloud_run", "run_job": run_job, "args": args, "env": env or {}}


def _workflow(workflow: str, argument: dict, options: dict[str, str], into: str) -> dict:
    """A task that runs another Workflow: `options` are substituted at run time, the empty
    ones dropped, and merged into `argument[into]` ("" = the argument itself)."""
    return {"type": "workflow", "workflow": workflow, "argument": argument,
            "options": options, "options_into": into}


def _vertex_argument(lb: Loaded, job: str, task: _TaskBase) -> dict:
    v = task.vertex_pipeline_task
    return {"job": f"{job}-{task.task_key}", "target": lb.target, "region": lb.bundle.gcp.region,
            "max_hours": (task.timeout_seconds or MAX_TASK_SECONDS) / 3600,
            "bucket": lb.bundle.gcp.bucket, "pipeline": v.pipeline,
            "spec_prefix": storage_prefix(lb.target) + pipeline_prefix(lb.bundle, job, task)}


def _batch_serve_argument(lb: Loaded, job: str, task: _TaskBase) -> dict:
    s, t, b = task.batch_serve_task, lb.target, lb.bundle
    models = [{"model": m.model, **_served_runner(b, job, m, t)}
              | dict(zip(("from_days", "to_days"), m.offsets(s.window_days)))
              | ({"output": m.output.model_dump_json()} if m.output else {}) for m in s.models]
    return {"job": f"{job}-{task.task_key}", "target": t, "region": b.gcp.region,
            "max_hours": s.max_hours, "models": models, "window_days": s.window_days,
            "utc_offset_hours": s.utc_offset_hours, "service_account": b.run_as.service_principal_name,
            "bucket": b.gcp.bucket,
            "progress_prefix": f"{storage_prefix(t)}.bundle/{b.bundle.name}/serve_progress/{job}-{task.task_key}"}


def _served_runner(b: Bundle, job: str, m: ServedModel, t: str) -> dict:
    """How workflows/batch_serve.yaml scores one model: its Cloud Run job, or a Vertex AI
    batch prediction job (`vertex`)."""
    v = m.vertex_batch_prediction
    if v is None:
        return {"job": served_job_name(b, job, m, t)}
    return {"vertex": {
        "registered_model": v.registered_model, "version": f"@{v.alias}" if v.alias else f"@{v.version}",
        "machine_type": v.machine_type, "starting_replica_count": v.starting_replica_count,
        "max_replica_count": v.max_replica_count, "date_col": v.date_col,
        "input_table": v.input_table, "output_table": v.output_table,
        "prediction_col": v.prediction_col, "entity_id_columns": v.entity_id_columns,
        "instance_type": v.instance_type, "included_fields": v.included_fields,
        "excluded_fields": v.excluded_fields, "prediction_expr": v.prediction_expr}}


def notebook_object(b: Bundle, job: str, task: _TaskBase, target: str) -> str:
    """Where a notebook_task's notebook is read from: a workspace notebook where the console
    keeps it, a bundle one where deploy uploads it beside the job definitions."""
    n = task.notebook_task
    if n.in_workspace:
        return WORKSPACE_PREFIX + n.notebook_path.removeprefix(WORKSPACE_ROOT)
    return f"{storage_prefix(target)}.bundle/{b.bundle.name}/notebooks/{job}-{task.task_key}.ipynb"


def _notebook_argument(lb: Loaded, job: str, task: _TaskBase) -> dict:
    b, t = lb.bundle, lb.target
    return {"job": f"{job}-{task.task_key}", "target": t, "region": b.gcp.region, "bucket": b.gcp.bucket,
            "object": notebook_object(b, job, task, t),
            "output_prefix": f"{storage_prefix(t)}.bundle/{b.bundle.name}/notebook_runs/{job}-{task.task_key}",
            "runtime_template": task.notebook_task.runtime_template,
            "timeout_seconds": task.timeout_seconds or MAX_TASK_SECONDS,
            "service_account": b.run_as.service_principal_name}


def notebooks(lb: Loaded) -> dict[str, dict]:
    """Each bundle notebook a notebook_task runs, as the module uploads it."""
    out = {}
    for name, job in sorted(lb.bundle.resources.jobs.items()):
        for task in _all_tasks(job):
            if task.kind != "notebook_task" or task.notebook_task.in_workspace:
                continue
            path = lb.root / task.notebook_task.notebook_path
            if not path.is_file():
                raise ValueError(f"job {name} task {task.task_key}: no notebook {task.notebook_task.notebook_path}")
            try:
                content = json.loads(path.read_text())
            except ValueError as e:
                raise ValueError(f"job {name} task {task.task_key}: {task.notebook_task.notebook_path} is not a notebook: {e}") from None
            out[f"{name}-{task.task_key}"] = {"object": notebook_object(lb.bundle, name, task, lb.target),
                                              "content": json.dumps(content, sort_keys=True)}
    return out


def _descriptor(lb: Loaded, job: str, task: _TaskBase) -> dict:
    """What terraform/modules/bundle/workflows/job.yaml needs to run one task."""
    b, t = lb.bundle, lb.target
    kind = task.kind
    if kind == "python_wheel_task":
        w = task.python_wheel_task
        params = w.parameters or [f"--{k}={v}" for k, v in w.named_parameters.items()]
        body = _cloud_run(run_job_name(b, job, task.job_cluster_key, t),
                          ["python", "-c", WHEEL_LAUNCHER, w.package_name, w.entry_point, *params])
    elif kind == "spark_python_task":
        s = task.spark_python_task
        bundled = runs_bundle_code(s.python_file)
        body = _cloud_run(run_job_name(b, job, task.job_cluster_key, t),
                          bundle_python(b, t, s.python_file, s.parameters) if bundled
                          else ["python", s.python_file, *s.parameters],
                          files_env(lb) if bundled else None)
    elif kind == "dbt_task":
        d = task.dbt_task
        args = ["mlops-task", "dbt", "--files", files_uri(b, t),
                "--project-directory", d.project_directory or "."]
        if d.profiles_directory is not None:
            args += ["--profiles-directory", d.profiles_directory]
        else:
            args += ["--catalog", d.catalog or b.gcp.project, "--schema", d.schema_,
                     "--location", b.gcp.region]
        if d.state_location:
            args += ["--state-location", d.state_location]
        run_job = (run_job_name(b, job, task.job_cluster_key, t) if task.job_cluster_key
                   else dbt_job_name(b, t))
        body = _cloud_run(run_job, [*args, "--", *d.commands], files_env(lb))
    elif kind == "condition_task":
        c = task.condition_task
        body = {"type": "condition", "op": c.op, "left": c.left, "right": c.right}
    elif kind == "run_job_task":
        r = task.run_job_task
        local = local_job(b, r.job_id)
        child = workflow_name(b, local, t) if local else f"{r.job_id}{name_suffix(t)}"
        body = _workflow(child, {}, r.job_parameters, "params")
    elif kind == "vertex_pipeline_task":
        body = _workflow(task_workflow_name(b, kind, t), _vertex_argument(lb, job, task),
                         task.vertex_pipeline_task.parameters, "overrides")
    elif kind == "batch_serve_task":
        body = _workflow(task_workflow_name(b, kind, t), _batch_serve_argument(lb, job, task),
                         task.batch_serve_task.parameters, "")
    elif kind == "notebook_task":
        body = _workflow(task_workflow_name(b, kind, t), _notebook_argument(lb, job, task),
                         task.notebook_task.base_parameters, "parameters")
    else:  # for_each_task
        f = task.for_each_task
        body = {"type": "for_each", "inputs": f.inputs, "concurrency": f.concurrency,
                "task": _descriptor(lb, job, f.task)}
    return {**body, **_common(task)}


def job_argument(lb: Loaded, name: str) -> dict:
    """What a run of job `name` receives: its scheduler entry's argument, and what
    `mlops bundle run`, a trigger or another bundle's run_job_task sends, plus `params`
    (the run's parameter values). Empty: the job's Workflow finds its definition from its
    own environment (job_env), so anyone can start it knowing only its name."""
    return {}


def job_env(lb: Loaded, name: str) -> dict[str, str]:
    """The job Workflow's environment: where its definition is, and whose it is."""
    b = lb.bundle
    return {"MLOPS_BUCKET": b.gcp.bucket, "MLOPS_DEFINITION": definition_object(b, name, lb.target),
            "MLOPS_JOB": name, "MLOPS_TARGET": lb.target,
            # Its display name, as Databricks' job name: the key stays its identity.
            "MLOPS_JOB_NAME": b.resources.jobs[name].name or name}


def environment_image(lb: Loaded, job: str, key: str) -> str:
    """The image a job's environment is built as (gcpbricks.environments)."""
    from gcpbricks import environments

    env = next(e for e in lb.bundle.resources.jobs[job].environments if e.environment_key == key)
    return environments.image(lb, env)


def mlflow_env(lb: Loaded) -> dict[str, str]:
    """Where gcpbricks.mlflow tracks and stores artifacts, set on every job cluster as
    Databricks sets a job's MLflow: the project's Vertex, the target's artifact prefix."""
    b = lb.bundle
    return {"MLFLOW_TRACKING_URI": f"vertex://{b.gcp.project}/{b.gcp.region}",
            "MLFLOW_ARTIFACT_ROOT": f"gs://{b.gcp.bucket}/{storage_prefix(lb.target)}mlflow"}


def external_jobs(lb: Loaded) -> list[str]:
    """Workflows of other bundles' jobs this bundle's run_job_tasks call, on its target."""
    b = lb.bundle
    return sorted({f"{t.run_job_task.job_id}{name_suffix(lb.target)}"
                   for j in b.resources.jobs.values() for t in _all_tasks(j)
                   if t.run_job_task and local_job(b, t.run_job_task.job_id) is None})


def trigger_values(job: Job) -> dict[str, str]:
    """The {{job.trigger.table_update...}} references a run of the job may use, as Databricks
    names them (one table: without its name segment), and what each resolves to when the run
    wasn't started by the trigger: no tables, empty values."""
    if job.trigger is None or job.trigger.kind != "table_update":
        return {}
    names = job.trigger.table_update.table_names
    out = {"job.trigger.table_update.updated_tables": "[]"}
    for n in names:
        for f in TRIGGER_TABLE_FIELDS:
            out[f"job.trigger.table_update.{n}.{f}"] = ""
            if len(names) == 1:
                out[f"job.trigger.table_update.{f}"] = ""
    return out


def job_definition(lb: Loaded, name: str) -> dict:
    """What workflows/job.yaml runs: the job's tasks, levels and run settings."""
    job = lb.bundle.resources.jobs[name]
    tasks = {}
    for t in job.tasks:
        tasks[t.task_key] = {**_descriptor(lb, name, t), "run_if": t.run_if,
                             "depends_on": [{"task_key": d.task_key, "outcome": d.outcome or ""}
                                            for d in t.depends_on]}
    warn = job.duration_warning_seconds
    return {"region": lb.bundle.gcp.region,
            "parameters": {p.name: p.default for p in job.parameters},
            "trigger_values": trigger_values(job),
            "levels": levels(job), "tasks": tasks,
            "max_concurrent_runs": job.max_concurrent_runs,
            "queue": bool(job.queue and job.queue.enabled),
            "timeout_seconds": job.timeout_seconds,
            "duration_warning_seconds": warn,
            "watchdog": watchdog_workflow_name(lb.bundle, lb.target) if warn else "",
            "notify": failure_mail(lb, name),
            "settings": job_settings(lb, name)}


#: The notification events a job and a task declare, in Databricks' order.
_EVENTS = ("on_start", "on_success", "on_failure", "on_duration_warning_threshold_exceeded")


def _notifications(scope: str, emails, webhooks) -> list[dict]:
    out = []
    for event in _EVENTS:
        to = list(getattr(emails, event, None) or [])
        hooks = [h.id for h in getattr(webhooks, event, None) or []]
        if to or hooks:
            out.append({"scope": scope, "event": event, "emails": to, "webhooks": hooks})
    return out


def job_settings(lb: Loaded, name: str) -> dict:
    """The job as declared, for the console: what job.yaml does not run on but a reader of
    the job wants (Databricks' job details panel), every default resolved. The one key of
    the definition job.yaml does not read."""
    job = lb.bundle.resources.jobs[name]
    trigger = None
    if job.trigger:
        cfg = getattr(job.trigger, job.trigger.kind)
        trigger = {"kind": job.trigger.kind, **cfg.model_dump(exclude_none=True)}
    ns = job.notification_settings
    em = job.email_notifications
    clusters = []
    for c in job.job_clusters:
        nc = c.new_cluster
        size = (cloud_run_resources(nc.node_type_id) if nc.node_type_id
                else exact_resources(nc.cpu, nc.memory) if nc.cpu else {"cpu": None, "memory": None})
        clusters.append({"key": c.job_cluster_key, "node_type_id": nc.node_type_id, **size,
                         "image": nc.docker_image.url if nc.docker_image else None,
                         "environment_key": nc.environment_key, "num_workers": nc.num_workers,
                         "env_vars": dict(nc.spark_env_vars)})
    return {
        "trigger": trigger,
        "notifications": _notifications("job", em, job.webhook_notifications) + [
            n for t in job.tasks
            for n in _notifications(t.task_key, t.email_notifications, t.webhook_notifications)],
        "no_alert_for_skipped_runs": bool((ns and ns.no_alert_for_skipped_runs)
                                          or (em and em.no_alert_for_skipped_runs)),
        "job_clusters": clusters,
        "environments": [{"key": e.environment_key, "dependencies": list(e.spec.dependencies),
                          "image": environment_image(lb, name, e.environment_key)}
                         for e in job.environments],
    }


def _all_tasks(job: Job) -> list[_TaskBase]:
    return [x for t in job.tasks for x in ([t, t.for_each_task.task] if t.for_each_task else [t])]


#: How often the trigger Workflow checks a table_update / file_arrival trigger; a
#: file_arrival trigger is also checked at once on each GCS event (workflows/wake.yaml).
TRIGGER_POLL_CRON = "*/5 * * * *"
#: Workflows' limit on one environment variable: a wake Workflow's trigger argument.
MAX_ENV_BYTES = 4096


def _channel(b: Bundle, webhook_id: str) -> str:
    return (webhook_id if webhook_id.startswith("projects/")
            else f"projects/{b.gcp.project}/notificationChannels/{webhook_id}")


def _log_filter(workflow: str, event: str, field: str, states: list[str], **extra: str) -> str:
    parts = ['resource.type="workflows.googleapis.com/Workflow"',
             f'resource.labels.workflow_id="{workflow}"', f'jsonPayload.event="{event}"',
             *[f'jsonPayload.{k}="{v}"' for k, v in extra.items()],
             f"jsonPayload.{field}=({' OR '.join(chr(34) + s + chr(34) for s in states)})"]
    return " AND ".join(parts)


def _alerts(lb: Loaded, name: str, job: Job) -> list[dict]:
    """One log-based alert per (job or task, event) with recipients; a job's on_failure is
    always listed, since the module adds its notification_channels to every one."""
    b, t = lb.bundle, lb.target
    wf = workflow_name(b, name, t)
    out = []

    def add(key, flt, emails, hooks, job_failure=False):
        if emails or hooks or job_failure:
            out.append({"key": key, "filter": flt, "emails": sorted(set(emails)),
                        "channels": sorted({_channel(b, h.id) for h in hooks}),
                        "job_failure": job_failure})
    em, wh, ns = job.email_notifications, job.webhook_notifications, job.notification_settings
    skipped = not ((ns and ns.no_alert_for_skipped_runs) or (em and em.no_alert_for_skipped_runs))
    # With the platform's own failure mail, Cloud Monitoring emails only a skipped run.
    mailed = bool(failure_mail(lb, name))
    for event, states in [("on_start", ["STARTED"]), ("on_success", ["SUCCEEDED"]),
                          ("on_failure", ["FAILED", *(["SKIPPED"] if skipped else [])])]:
        emails = getattr(em, event, []) if em else []
        add(f"{name}:{event}", _log_filter(wf, "mlops_run", "state", states, job=name),
            [] if mailed and event == "on_failure" else emails, getattr(wh, event, []) if wh else [],
            job_failure=event == "on_failure")
    if mailed and skipped and em and em.on_failure:
        add(f"{name}:on_skipped", _log_filter(wf, "mlops_run", "state", ["SKIPPED"], job=name), em.on_failure, [])
    dw = "on_duration_warning_threshold_exceeded"
    if job.duration_warning_seconds:
        add(f"{name}:{dw}", _log_filter(watchdog_workflow_name(b, t), "mlops_run", "state",
                                        ["DURATION_WARNING"], job=name),
            getattr(em, dw, []) if em else [], getattr(wh, dw, []) if wh else [])
    for task in job.tasks:
        te, tw = task.email_notifications, task.webhook_notifications
        for event, states in [("on_start", ["STARTED"]), ("on_success", ["SUCCESS"]),
                              ("on_failure", ["FAILED"])]:
            add(f"{name}.{task.task_key}:{event}",
                _log_filter(wf, "mlops_task", "status", states, job=name, task=task.task_key),
                getattr(te, event, []) if te else [], getattr(tw, event, []) if tw else [])
    return out


def _paused(pause_status: str | None, mode: str | None) -> bool:
    return pause_status == "PAUSED" or (pause_status is None and mode == "development")


def _watched_table(name: str) -> dict:
    """How workflows/trigger.yaml observes a table: BigQuery's lastModifiedTime, or an Iceberg
    table's current snapshot in its BigLake catalog."""
    parts = name.split(".")
    if len(parts) == 4:
        return {"name": name, "kind": "iceberg", **dict(zip(("project", "catalog", "namespace", "table"), parts))}
    return {"name": name, "kind": "bigquery", **dict(zip(("project", "dataset", "table"), parts))}


def _trigger_argument(lb: Loaded, name: str, trigger: TriggerSettings) -> dict:
    b, t = lb.bundle, lb.target
    cfg = getattr(trigger, trigger.kind)
    if trigger.kind == "table_update":
        watch = {"tables": [_watched_table(n) for n in cfg.table_names], "condition": cfg.condition}
    else:
        bucket, _, prefix = cfg.url.removeprefix("gs://").partition("/")
        watch = {"watch_bucket": bucket, "watch_prefix": prefix}
    return {"job": name, "target": t, "kind": trigger.kind, **watch,
            "workflow": workflow_name(b, name, t), "job_argument": job_argument(lb, name),
            "bucket": b.gcp.bucket, "state_object": trigger_state_object(b, name, t),
            "min_time_between_triggers_seconds": cfg.min_time_between_triggers_seconds,
            "wait_after_last_change_seconds": cfg.wait_after_last_change_seconds}


def ml_plan(lb: Loaded) -> dict:
    """The ML resources `mlops bundle deploy` applies on Vertex AI (gcpbricks.ml)."""
    from gcpbricks import ml

    r = lb.bundle.resources
    names = resource_names(r.model_dump(by_alias=True), lb.target, lb.bundle.bundle.name)
    return ml.plan(r.experiments, r.registered_models, r.model_serving_endpoints, names,
                   bundle_labels(lb))


def bundle_labels(lb: Loaded) -> dict[str, str]:
    return {"mlops_bundle": lb.bundle.bundle.name, "mlops_target": lb.target}


def render(lb: Loaded) -> dict:
    """terraform/modules/bundle's `plan` input for the loaded target: every Workflow,
    scheduler, Cloud Run job, job definition and alert, every default resolved; and each
    retired job's Workflow and what its runs use, kept as its last deploy rendered them."""
    return _render(lb)[0]


def job_plans(lb: Loaded) -> dict[str, dict]:
    """{job key: the plan fragment it owns}: its `jobs` entry, and the Cloud Run jobs and task
    runner Workflows its tasks use. A later deploy that no longer declares the job keeps
    exactly this (retired_jobs), so a run in flight finishes and its history stays."""
    plan, owned = _render(lb)
    return {k: {"job": plan["jobs"][k],
                "run_jobs": {n: plan["run_jobs"][n] for n in sorted(o["run_jobs"])},
                "task_workflows": {n: plan["task_workflows"][n] for n in sorted(o["task_workflows"])}}
            for k, o in owned.items()}


def _render(lb: Loaded) -> tuple[dict, dict[str, dict[str, set]]]:
    from gcpbricks.ml import RETIRED

    b, t = lb.bundle, lb.target
    jobs, run_jobs, task_workflows, triggers, alerts = {}, {}, {}, {}, []
    owned: dict[str, dict[str, set]] = {}
    watchdog = False
    for name, job in sorted(b.resources.jobs.items()):
        mine = owned[name] = {"run_jobs": set(), "task_workflows": set()}
        s, trig = job.schedule, job.trigger
        if s:
            schedule, tz, paused = unix_cron(s.quartz_cron_expression), s.timezone_id, _paused(s.pause_status, lb.mode)
        elif trig and trig.kind == "periodic":
            schedule, tz = periodic_cron(trig.periodic.interval, trig.periodic.unit), "UTC"
            paused = _paused(trig.pause_status, lb.mode)
        else:
            schedule, tz, paused = None, "UTC", False
        if trig and trig.kind != "periodic":
            argument = json.dumps(_trigger_argument(lb, name, trig), sort_keys=True)
            files = trig.kind == "file_arrival"
            if files and len(argument.encode()) > MAX_ENV_BYTES:
                raise ValueError(f"job {name!r}: its trigger argument is over {MAX_ENV_BYTES} bytes, "
                                 f"the most a Workflow environment variable holds")
            triggers[name] = {
                "schedule": TRIGGER_POLL_CRON, "paused": _paused(trig.pause_status, lb.mode),
                "argument": argument,
                "file_bucket": trig.file_arrival.url.removeprefix("gs://").split("/")[0] if files else None,
                "wake_workflow": wake_workflow_name(b, name, t) if files else None}
        labels = bundle_labels(lb) | job.tags
        jobs[name] = {"workflow": workflow_name(b, name, t),
                      "description": job.description or job.name or name,
                      "schedule": schedule, "time_zone": tz, "paused": paused, "labels": labels,
                      "definition_object": definition_object(b, name, t),
                      "env": job_env(lb, name),
                      "definition": json.dumps(job_definition(lb, name), sort_keys=True),
                      "argument": json.dumps(job_argument(lb, name), sort_keys=True)}
        watchdog |= bool(job.duration_warning_seconds)
        alerts += _alerts(lb, name, job)
        clusters = {c.job_cluster_key: c.new_cluster for c in job.job_clusters}

        def cluster_job(nc: NewCluster, args: list[str] | None = None, labels: dict = labels,
                        job: str = name, env: dict | None = None) -> dict:
            image = nc.docker_image.url if nc.docker_image else environment_image(lb, job, nc.environment_key)
            return {"image": image, "env": mlflow_env(lb) | nc.spark_env_vars | (env or {}), "labels": labels,
                    "tasks": nc.num_workers, "args": args,
                    **(cloud_run_resources(nc.node_type_id) if nc.node_type_id
                       else exact_resources(nc.cpu, nc.memory) if nc.cpu
                       else {"cpu": None, "memory": None})}
        for key, nc in clusters.items():
            run_jobs[run_job_name(b, name, key, t)] = cluster_job(nc)
            mine["run_jobs"].add(run_job_name(b, name, key, t))
        for task in _all_tasks(job):
            for m in (task.batch_serve_task.models if task.batch_serve_task else []):
                if m.python_file:
                    # Its args are the Cloud Run job's own, so its files version is too.
                    run_jobs[served_job_name(b, name, m, t)] = cluster_job(
                        clusters[m.job_cluster_key], bundle_python(b, t, m.python_file, m.parameters),
                        env=files_env(lb))
                    mine["run_jobs"].add(served_job_name(b, name, m, t))
        for task in _all_tasks(job):
            if task.kind == "dbt_task" and not task.job_cluster_key:
                run_jobs[dbt_job_name(b, t)] = {"image": None, "env": {}, "cpu": None, "args": None,
                                                "memory": None, "labels": bundle_labels(lb)}
                mine["run_jobs"].add(dbt_job_name(b, t))
            if task.kind in ("vertex_pipeline_task", "batch_serve_task", "notebook_task"):
                task_workflows[task.kind.removesuffix("_task")] = task_workflow_name(b, task.kind, t)
                mine["task_workflows"].add(task.kind.removesuffix("_task"))
    for name, r in sorted(lb.retired_jobs.items()):
        if name in jobs:
            continue
        # As its last deploy rendered it, but nothing starts it: no schedule, no trigger,
        # no alert. What its runs use is kept unless a declared job renders it anew.
        frag = r["plan"]
        jobs[name] = {**frag["job"], "schedule": None, "paused": True,
                      "labels": {**frag["job"]["labels"], RETIRED: r["since"]}}
        for n, rj in frag["run_jobs"].items():
            run_jobs.setdefault(n, rj)
        for n, wf in frag["task_workflows"].items():
            task_workflows.setdefault(n, wf)
    if triggers:
        task_workflows["trigger"] = trigger_workflow_name(b, t)
    if watchdog:
        task_workflows["watchdog"] = watchdog_workflow_name(b, t)
    return {
        "bundle": b.bundle.name,
        "target": t,
        "region": b.gcp.region,
        "service_account": b.run_as.service_principal_name,
        "bucket": b.gcp.bucket,
        "jobs": jobs,
        "run_jobs": run_jobs,
        "task_workflows": dict(sorted(task_workflows.items())),
        "triggers": triggers,
        "alerts": alerts,
        "schemas": schemas(lb),
        "dashboards": dashboards(lb),
        "notebooks": notebooks(lb),
    }, owned


def schemas(lb: Loaded) -> dict[str, dict]:
    """Each schema as the BigQuery dataset the module manages (gcpbricks.infra imports
    one that already exists)."""
    b, t = lb.bundle, lb.target
    names = resource_names(b.resources.model_dump(by_alias=True), t, b.bundle.name)["schemas"]
    return {k: {"project": s.catalog_name or b.gcp.project, "dataset_id": names[k],
                "location": s.location, "description": s.comment or "", "labels": bundle_labels(lb)}
            for k, s in sorted(b.resources.schemas.items())}


LATEST = "latest"


def platform_version(lb: Loaded) -> str:
    """The release the bundle runs on: its pinned mlops_version, or for "latest" the installed
    gcpbricks deploying it, so its images are that release's."""
    if lb.bundle.bundle.mlops_version != LATEST:
        return lb.bundle.bundle.mlops_version
    from importlib.metadata import version

    return version("gcpbricks")


def retired_jobs(lb: Loaded, previous: dict, today: str) -> dict[str, dict]:
    """The jobs this deploy keeps retired: the ones its previous deploy (`previous`, that
    deploy.json) recorded a plan for or kept retired, which the bundle no longer declares.
    Declaring one again brings it back as it is declared."""
    declared = set(lb.bundle.resources.jobs)
    kept = {k: r for k, r in (previous.get("retired_jobs") or {}).items() if k not in declared}
    for k, frag in (previous.get("job_plans") or {}).items():
        if k not in declared and k not in kept:
            kept[k] = {"since": today, "plan": frag}
    return kept


def deployment_object(b: Bundle, target: str) -> str:
    """Where deploy records what it deployed (deployment), as Databricks keeps a bundle's
    deployment.json per target: the console's bundle page reads it."""
    return f"{storage_prefix(target)}.bundle/{b.bundle.name}/deployment.json"


def deployment(lb: Loaded, version: str, mlops_version: str, deployed_by: str, ci_url: str,
               deployed_at: str, source: dict | None = None) -> dict:
    """What a deploy of the bundle to its target put where: the code version (and `source`,
    the git commit it came from), the platform that deployed it, and every resource by key
    with its name on the target."""
    b, t = lb.bundle, lb.target
    names = resource_names(b.resources.model_dump(by_alias=True), t, b.bundle.name)
    return {"bundle": b.bundle.name, "target": t, "mode": lb.mode, "project": b.gcp.project,
            "region": b.gcp.region, "bucket": b.gcp.bucket, "run_as": b.run_as.service_principal_name,
            "version": version, "mlops_version": mlops_version,
            "declared_mlops_version": b.bundle.mlops_version, "deployed_at": deployed_at,
            "deployed_by": deployed_by, "ci_url": ci_url, **(source or {}),
            # What the next deploy keeps of a job it no longer declares (retired_jobs).
            "job_plans": job_plans(lb),
            "retired_jobs": {k: r for k, r in lb.retired_jobs.items() if k not in b.resources.jobs},
            "resources": {
                "jobs": {k: workflow_name(b, k, t) for k in b.resources.jobs},
                "experiments": names["experiments"], "registered_models": names["registered_models"],
                "model_serving_endpoints": names["model_serving_endpoints"],
                "schemas": {k: {"project": s.catalog_name or b.gcp.project, "dataset": names["schemas"][k]}
                            for k, s in b.resources.schemas.items()},
                "dashboards": {k: dashboard_object(b, k, t) for k in b.resources.dashboards}}}


def dashboard_object(b: Bundle, key: str, target: str) -> str:
    """Where the module uploads a dashboard's spec, beside the bundle's job definitions: the
    console finds a bundle's dashboards there."""
    return f"{storage_prefix(target)}.bundle/{b.bundle.name}/dashboards/{key}.json"


def dashboards(lb: Loaded) -> dict[str, dict]:
    """Each dashboard as the module uploads it: its spec, checked, with the bundle's
    display_name ("[<target>] " before it off prod, as Databricks marks dev resources)."""
    from gcpbricks.dashboards import DashboardSpec

    out = {}
    for k, d in sorted(lb.bundle.resources.dashboards.items()):
        path = lb.root / d.file_path
        if not path.is_file():
            raise ValueError(f"dashboards {k!r}: no file {d.file_path}")
        try:
            spec = DashboardSpec.model_validate(yaml.safe_load(path.read_text()))
        except ValueError as e:
            raise ValueError(f"dashboards {k!r} ({d.file_path}): {e}") from None
        title = d.display_name if lb.target == PROD else f"[{lb.target}] {d.display_name}"
        out[k] = {"object": dashboard_object(lb.bundle, k, lb.target),
                  "content": json.dumps({"key": k, "title": title, "bundle": lb.bundle.bundle.name,
                                         "target": lb.target, **spec.model_dump(exclude_none=True)},
                                        sort_keys=True)}
    return out
