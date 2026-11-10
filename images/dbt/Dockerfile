# The platform's dbt image: a dbt_task without a job cluster runs `mlops-task dbt` in it,
# over the bundle's files (gcpbricks.task_runner). Built from this repo at its tag.
# dbt v2 bundles its adapters: BigQuery now, Spark where a cluster is reachable.
FROM python:3.11-slim
ARG DBT_VERSION=2.0.8
# dbt deps clones git packages.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md /src/
COPY src /src/src
# The wheel force-includes the bundle module (pyproject.toml).
COPY terraform/modules/bundle /src/terraform/modules/bundle
RUN pip install --no-cache-dir /src "dbt==${DBT_VERSION}"
# No WORKDIR /workspace: kaniko never snapshots that path, and Cloud Run can't start in a
# missing working directory. mlops-task works in a temp dir of its own.
CMD ["mlops-task", "--help"]
