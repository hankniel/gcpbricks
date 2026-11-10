# gcpbricks

**Databricks Asset Bundles for GCP.** Declare a project's jobs once in `mlops.yaml`, using the
keys a `databricks.yml` uses. gcpbricks turns that into Cloud Workflows, Cloud Run jobs,
Cloud Scheduler entries, Vertex AI pipelines and alerts, and gives you a CLI and a web
console to deploy, run, repair and inspect them.

> Compatible with the Databricks Asset Bundles *format*. Not affiliated with or endorsed by
> Databricks.

```yaml
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
          dbt_task: {project_directory: dbt, schema: "${var.schema}", commands: ["dbt build"]}
        - task_key: score
          depends_on: [{task_key: build_tables}]
          job_cluster_key: main
          max_retries: 1
          python_wheel_task: {package_name: model_serving, entry_point: nightly,
                              parameters: ["--date", "{{job.parameters.run_date}}"]}
```

```console
$ mlops bundle summary -t dev
bundle demo-app  target dev  run as bundle-runner@my-gcp-project.iam.gserviceaccount.com

job nightly  workflow demo-app-nightly-dev  0 2 * * * UTC (paused)
  [0] build_tables             dbt_task
  [1] score                    python_wheel_task      after build_tables

cloud run job demo-app-nightly-main-dev  image .../model-serving:latest
cloud run job demo-app-dbt-dev           image (platform dbt image)
```

The example above is `tests/fixtures/bundle`; you can run it without a GCP account
(see [Try it locally](#try-it-locally)).

## Why

Databricks' bundle format is a good way to describe a data or ML job: tasks, dependencies,
retries, `run_if`, job parameters, targets such as `dev` and `prod`. On GCP there is no
equivalent: teams hand-write Workflows YAML, Scheduler entries and Terraform per project.
gcpbricks keeps Databricks' interface and runs it on GCP's own services, with no cluster to
keep alive between runs. The reasoning and trade-offs are in [docs/idea.md](docs/idea.md).

## What it does

- **Bundles**: `mlops.yaml` with `include`, `variables`, `targets` (`dev` / `prod`, merged by
  key), `${var.*}` and `{{job.*}}` values. A key the platform can't run fails validation
  instead of being ignored.
- **Jobs on GCP**: one Cloud Workflow per job, run level by level with `depends_on`, `run_if`,
  `max_retries`, `condition_task`, `for_each_task` and `run_job_task`; one Cloud Run job per
  job cluster; Cloud Scheduler for cron; `table_update` and `file_arrival` triggers; failure
  alerts and a run-duration watchdog.
- **Task kinds**: `python_wheel_task`, `spark_python_task`, `dbt_task` (on a bundled dbt
  image), `vertex_pipeline_task` (a compiled KFP pipeline), `batch_serve_task` (score models
  over a window).
- **CLI**: `mlops bundle validate | summary | render | deploy | run | runs | repair | why |
  task | logs | redeploy | destroy`. A manual run sends exactly what the scheduler sends.
- **Repairs like Databricks**: re-run only the failed (or picked) tasks of a run, as another
  attempt of the same run; a Vertex pipeline task reruns only its failed steps.
- **ML resources**: `experiments`, `registered_models`, `model_serving_endpoints` applied on
  Vertex AI, plus an MLflow tracking and registry API over Vertex AI Experiments and Model
  Registry.
- **Feature store SDK**: feature groups declared in dbt (`meta.feature_store`), read through
  an Ibis-compiled engine for BigQuery, Spark or DuckDB, with point-in-time training sets.
- **Console**: a FastAPI + React app behind IAP: jobs, runs, task graphs, "why it failed",
  repair dialog, notebooks, bundles, costs, models, endpoints and a feature catalog.

## Try it locally

No GCP account is needed to load, validate and render a bundle.

```bash
git clone https://github.com/hankniel/gcpbricks && cd gcpbricks
uv venv && uv pip install -e ".[dev]"      # or: python -m venv .venv && pip install -e ".[dev]"
cd tests/fixtures/bundle
mlops bundle validate -t dev
mlops bundle summary  -t dev
mlops bundle render   -t dev               # the plan the terraform module consumes
```

Run the tests (about 740, no GCP calls) from the repo root: `pytest`.

Deploying to a real project needs a GCP project, a bucket, a runner service account and
Terraform; see [docs/getting-started.md](docs/getting-started.md).

## Documentation

| Page | What is in it |
|---|---|
| [Getting started](docs/getting-started.md) | Install, write a bundle, run it locally, deploy it to GCP, run and repair |
| [Idea](docs/idea.md) | The problem, the design decisions, what is deliberately left out |
| [Architecture](docs/architecture.md) | How the pieces fit: repos, data, jobs, ML resources, a change end to end, the contracts the tests hold |
| [Reference](docs/reference.md) | Every job kind and resource, the feature store SDK, console, CI and release flow, known problems |

## Layout

| Path | What it is |
|---|---|
| `src/gcpbricks/bundle.py` | `mlops.yaml`'s schema and loader; `job_argument()`, the one definition of a run's argument; `render()`, the terraform module's input |
| `src/gcpbricks/cli.py` | The `mlops bundle ...` commands |
| `src/gcpbricks/batch_serve.py`, `pipelines.py`, `publish.py`, `ml.py` | Batch serving, Vertex pipelines, pipeline publishing, ML resources |
| `src/gcpbricks/features.py`, `feature_manager/` | The feature store SDK and features-as-config |
| `src/gcpbricks/mlflow/` | MLflow's API over Vertex AI |
| `src/gcpbricks/console/`, `console/web/` | The console's API and its React app |
| `terraform/modules/bundle` | Per target: Workflows, Cloud Run jobs, schedulers, log sinks and alerts |
| `terraform/modules/bundle/workflows/` | `job`, `vertex_pipeline`, `batch_serve`, `trigger`, `wake`, `watchdog` |
| `images/` | The runtime, dbt and console images |
| `templates/project.yml`, `.gitlab-ci.yml` | A GitLab CI/CD component for projects, and this repo's own pipeline |
| `e2e/` | The release gate: a bundle deployed and run on dev before a release goes out |
| `tests/` | Unit and contract tests, and `tests/fixtures/bundle` |

## Status

Extracted from an internal platform that ran training, batch scoring and feature jobs on GCP.
Names, projects and URLs were replaced with placeholders, so treat the repo as a design and
reference implementation rather than a package to `pip install` from PyPI today. The CI
component and release flow target GitLab; the tests run anywhere.
