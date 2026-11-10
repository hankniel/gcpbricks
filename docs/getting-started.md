# Getting started

Two levels: **locally** (no GCP: load, validate, render, and write what a deploy would
produce) and **on GCP** (deploy, run, repair).

## 1. Install

```bash
git clone https://github.com/hankniel/gcpbricks && cd gcpbricks
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
mlops bundle --help
```

Python 3.11 or newer. Extras: `[ml]` (Vertex AI resources, MLflow API), `[features]` (feature
store SDK), `[console]` (the console's API), `[spark]` (the SDK on Spark), `[tuning]`.

## 2. Write a bundle

```
my-project/
  mlops.yaml
  resources/
    nightly.yml
```

`mlops.yaml` names the bundle, the GCP project, the bucket, the service account jobs run as,
and the targets:

```yaml
bundle: {name: demo-app, mlops_version: "0.4.0"}
include: [resources/*.yml]
gcp: {project: my-gcp-project, region: us-central1, bucket: my-ml-bucket}
run_as: {service_principal_name: bundle-runner@my-gcp-project.iam.gserviceaccount.com}
variables:
  image: {default: us-central1-docker.pkg.dev/my-gcp-project/ml/model-serving:latest}
  schema: {default: demo_app_dev}
targets:
  dev:  {mode: development, default: true}
  prod: {mode: production, variables: {schema: demo_app}}
```

Jobs go under `resources.jobs` (see the README for an example). A complete bundle is in
[`tests/fixtures/bundle`](../tests/fixtures/bundle).

## 3. Check it locally

From the bundle's directory:

```bash
mlops bundle validate -t dev      # loads, resolves variables, checks every key
mlops bundle summary  -t dev      # the Workflow and Cloud Run jobs it would create
mlops bundle render   -t dev      # the plan as JSON, the terraform module's input
mlops bundle deploy   -t dev --version local --out-dir ./out
```

The last command writes what a deploy would put in GCS (the bundle's files, a deployment
record) and the Terraform root it would apply, then stops. Look at `out/terraform/plan.json`
and `out/terraform/modules/bundle/workflows/job.yaml` to see the Workflow that runs a job.

## 4. Deploy to GCP

You need:

- a GCP project and a GCS bucket (`gcp.project`, `gcp.bucket`),
- a service account for jobs (`run_as`) with `roles/workflows.invoker` and
  `roles/run.developer`,
- the deploying identity with `roles/workflows.editor`, `roles/cloudscheduler.admin`,
  `roles/run.admin`, `roles/monitoring.alertPolicyEditor`,
  `roles/monitoring.notificationChannelEditor`, object admin on the bucket and
  `roles/iam.serviceAccountUser` on `run_as`,
- Terraform 1.6 or newer on `PATH`, and `gcloud auth application-default login`.

Extra roles apply to `file_arrival` / `table_update` triggers and image builds; the full list
is in [Reference](reference.md#using-it-in-a-project).

```bash
mlops bundle deploy -t dev        # uploads files, applies the terraform module
```

Schedules are paused in `development` mode unless a schedule says `pause_status: UNPAUSED`. The state is kept in the bucket under the target's
prefix, so the CLI needs no other backend. `mlops bundle destroy -t dev` removes it.

## 5. Run, inspect, repair

```bash
mlops bundle run nightly -t dev --params run_date=2026-01-15
mlops bundle runs nightly -t dev                  # run ids and states
mlops bundle why nightly -t dev --run <id>        # follow a failure down to its last log lines
mlops bundle logs nightly build_tables -t dev --run <id>
mlops bundle repair nightly -t dev --run <id>     # rerun only what didn't succeed
```

`run` sends the same argument the scheduler sends, plus your parameters. `repair` is another
attempt of the same run: tasks that succeeded are reused, the others run again, and
`--tasks`, `--params` and `--steps` change what is rerun.

## 6. The console (optional)

```bash
uv pip install -e ".[console]"
cd console/web && npm ci && npm run build
```

The API server is `gcpbricks.console.app:app` (FastAPI); on GCP it runs as a Cloud Run service
behind IAP (`images/console/Dockerfile`). It reads Workflows executions, Cloud Logging, Vertex
AI and BigQuery as the signed-in user. See [Reference](reference.md) for its pages.

## Next

- [Idea](idea.md): why it is built this way.
- [Architecture](architecture.md): how the pieces fit.
- [Reference](reference.md): every job kind, the feature store SDK, CI and release flow.
