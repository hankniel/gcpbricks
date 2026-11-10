"""Labels the platform's BigQuery jobs with the execution that ran them, so the console can
show a task run's bytes and cost: a Cloud Run execution's name, or a Vertex AI custom job's id.
A project's own client gets the same with bigquery_client()."""
from __future__ import annotations

import os
import re

LABEL = "mlops_exec"


def execution() -> str | None:
    """The Cloud Run execution or Vertex AI custom job this process runs in, as a label value."""
    ex = os.environ.get("CLOUD_RUN_EXECUTION") or os.environ.get("CLOUD_ML_JOB_ID")
    return re.sub(r"[^a-z0-9_-]", "-", ex.lower())[:63] if ex else None


def bigquery_client(**kwargs):
    """A google.cloud.bigquery Client whose query and load jobs carry LABEL (outside an
    execution, a plain Client)."""
    from google.cloud import bigquery

    ex = execution()
    if ex:
        kwargs.setdefault("default_query_job_config", bigquery.QueryJobConfig(labels={LABEL: ex}))
        kwargs.setdefault("default_load_job_config", bigquery.LoadJobConfig(labels={LABEL: ex}))
    return bigquery.Client(**kwargs)
