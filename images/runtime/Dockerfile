# The platform runtime, Databricks Runtime's place: the base every job environment builds on
# (gcpbricks.environments), built from this repo at its tag. Python, the system
# libraries ML wheels link at import (libgomp1: lightgbm, xgboost), and this version's
# gcpbricks with [ml,features], so a job's code finds mlops-task,
# gcpbricks.mlflow and the feature store's engine whatever its own lock pins.
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md /src/
COPY src /src/src
# The wheel force-includes the bundle module (pyproject.toml).
COPY terraform/modules/bundle /src/terraform/modules/bundle
RUN pip install --no-cache-dir "/src[ml,features]" && rm -rf /src
# A pipeline step's bundle code on sys.path (gcpbricks.pipelines).
COPY images/runtime/sitecustomize.py /usr/local/lib/python3.11/site-packages/sitecustomize.py
# No WORKDIR /workspace: kaniko never snapshots that path, and Cloud Run can't start in a
# missing working directory.
CMD ["mlops-task", "--help"]
