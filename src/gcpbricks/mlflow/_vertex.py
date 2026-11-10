"""The registry and tracking backend over Vertex AI (google-cloud-aiplatform, the `[ml]`
extra). A version is a Vertex model version: its artifactUri is the MLflow model directory,
its labels the version's tags, its aliases the version aliases. Nothing is ever deployed,
so a version carries a placeholder serving container, which Model.upload() demands."""
from __future__ import annotations

from datetime import datetime

#: Not a Vertex prebuilt container: for those, upload insists the directory hold exactly one
#: of model.pkl, model.joblib or model.bst, and an MLmodel directory holds none.
PLACEHOLDER_CONTAINER = "us-docker.pkg.dev/cloudrun/container/hello"


def _version(model) -> dict:
    """model: a gapic Model, as a version listing returns it (an SDK Model's .gca_resource)."""
    created = model.version_create_time
    # "default" is Vertex's own pointer, which every model's first version takes.
    return {"version": str(model.version_id),
            "aliases": [a for a in model.version_aliases or [] if a != "default"],
            "labels": dict(model.labels or {}), "uri": model.artifact_uri or "",
            "description": model.version_description or "",
            "created_ms": int(created.timestamp() * 1000) if isinstance(created, datetime) else 0}


class VertexBackend:
    def __init__(self, project: str, location: str):
        import logging

        from google.cloud import aiplatform

        self.ai, self.project, self.location = aiplatform, project, location
        aiplatform.init(project=project, location=location)
        # The SDK logs INFO to stdout from every call.
        for name in list(logging.Logger.manager.loggerDict):
            if name.startswith("google.cloud.aiplatform"):
                logging.getLogger(name).setLevel(logging.WARNING)

    # --- tracking --------------------------------------------------------------------------

    def log_run(self, experiment: str, run_id: str, params: dict, metrics: dict) -> None:
        from google.api_core.exceptions import NotFound

        self.ai.init(experiment=experiment)
        # resume: a retried task writes into the same run.
        try:
            run = self.ai.start_run(run=run_id, resume=True)
        except NotFound:
            run = self.ai.start_run(run=run_id, resume=False)
        with run:
            if params:
                self.ai.log_params(params)
            if metrics:
                self.ai.log_metrics(metrics)

    def get_run(self, experiment: str, run_id: str) -> dict | None:
        from google.api_core.exceptions import NotFound

        try:
            run = self.ai.ExperimentRun(run_name=run_id, experiment=experiment)
        except NotFound:
            return None
        return {"params": run.get_params(), "metrics": run.get_metrics()}

    # --- registry --------------------------------------------------------------------------

    def _models(self):
        from google.cloud import aiplatform_v1

        return aiplatform_v1.ModelServiceClient(
            client_options={"api_endpoint": f"{self.location}-aiplatform.googleapis.com"})

    def _parent(self, name: str) -> str | None:
        hits = self.ai.Model.list(filter=f'display_name="{name}"', order_by="create_time desc")
        return hits[0].resource_name if hits else None

    def model(self, name: str) -> dict | None:
        parent = self._parent(name)
        if parent is None:
            return None
        m = self.ai.Model(parent)
        return {"description": m.description or ""}

    def upload_version(self, name: str, uri: str, labels: dict, description: str) -> dict:
        m = self.ai.Model.upload(
            display_name=name, artifact_uri=uri, serving_container_image_uri=PLACEHOLDER_CONTAINER,
            parent_model=self._parent(name), is_default_version=False, labels=labels,
            version_description=description or None, sync=True)
        return _version(m.gca_resource)

    def versions(self, name: str, labels: dict | None = None) -> list[dict]:
        parent = self._parent(name)
        if parent is None:
            return []
        flt = " AND ".join(f'labels.{k}="{v}"' for k, v in (labels or {}).items())
        # The SDK's ModelRegistry.list_versions() drops labels and uri, which cost a get per
        # version (~1.5s each); the API's own listing carries every field in one call.
        return [_version(m) for m in self._models().list_model_versions(
            request={"name": parent, "filter": flt, "page_size": 100})]

    def version(self, name: str, version: str, parent: str | None = None) -> dict | None:
        parent = parent or self._parent(name)
        if parent is None:
            return None
        from google.api_core.exceptions import NotFound

        try:
            return _version(self.ai.Model(model_name=parent, version=str(version)).gca_resource)
        except NotFound:
            return None

    def set_labels(self, name: str, version: str, labels: dict) -> None:
        # The SDK's Model.update() writes model-level labels; this is the version's own.
        from google.cloud import aiplatform_v1
        from google.protobuf import field_mask_pb2

        self._models().update_model(
            model=aiplatform_v1.Model(name=f"{self._parent(name)}@{version}", labels=labels),
            update_mask=field_mask_pb2.FieldMask(paths=["labels"]))

    def add_alias(self, name: str, version: str, alias: str) -> None:
        from google.cloud.aiplatform.models import ModelRegistry

        ModelRegistry(model=self._parent(name)).add_version_aliases(new_aliases=[alias], version=version)

    def remove_alias(self, name: str, version: str, alias: str) -> None:
        from google.cloud.aiplatform.models import ModelRegistry

        ModelRegistry(model=self._parent(name)).remove_version_aliases(target_aliases=[alias], version=version)
