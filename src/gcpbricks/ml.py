"""A bundle's ML resources, Databricks' keys on Vertex AI, applied by `mlops bundle deploy`:

    experiments.<key>              a Vertex AI Experiment named `name` (+ "-<target>")
    registered_models.<key>        a Vertex Model Registry model, display name `name` or the
                                   key (+ "_<target>"), as demo_model names them. Vertex
                                   has no empty model: the first upload creates it, deploy
                                   sets its description and declared aliases.
    model_serving_endpoints.<key>  a Vertex AI Endpoint, display name `name` (+ "-<target>"):
                                   one deployed model per served entity, traffic per route

Deploy creates and updates; it never deletes an experiment, model or endpoint. One the bundle
stops declaring (named by the target's last deployment.json, or an endpoint carrying the
bundle's labels) is retired: labelled RETIRED with the date, and a retired endpoint's models
undeployed. Declaring it again lifts the label; deleting it is a console admin's step. Inside
a declared endpoint, deploy undeploys what no served entity names, after traffic has moved
off it.

plan() is the desired state, in plain dicts; apply() walks a Vertex client towards it.
VertexSdk is that client over google-cloud-aiplatform (the `[ml]` extra).
"""
from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The label a retired experiment, model or endpoint carries: the date it was retired.
RETIRED = "mlops_retired"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- schema -------------------------------------------------------------------------------

class Experiment(_Strict):
    #: The Vertex experiment id: lowercase letters, digits and -.
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,110}$")


class RegisteredModelAlias(_Strict):
    alias_name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,127}$")
    #: A Vertex version id.
    version_num: int = Field(gt=0)


class RegisteredModel(_Strict):
    #: Display name. Absent: the resource's key.
    name: str | None = None
    #: The model's description.
    comment: str | None = None
    #: The GCP project: must be gcp.project when set.
    catalog_name: str | None = None
    #: Moved onto their version on every deploy. A pipeline that promotes (@champion) owns
    #: its alias: don't declare it here too.
    aliases: list[RegisteredModelAlias] = []


#: workload_type -> (machine type, accelerator, accelerators per replica).
WORKLOAD_TYPES = {
    "CPU": ("n1-standard-4", "", 0),
    "GPU_SMALL": ("n1-standard-8", "NVIDIA_TESLA_T4", 1),
    "GPU_MEDIUM": ("g2-standard-12", "NVIDIA_L4", 1),
    "GPU_LARGE": ("a2-highgpu-1g", "NVIDIA_TESLA_A100", 1),
}
#: workload_size -> (min replicas, max replicas). Vertex keeps at least one replica.
WORKLOAD_SIZES = {"Small": (1, 1), "Medium": (1, 4), "Large": (2, 16)}


class ServedEntityInput(_Strict):
    #: Unique in the endpoint. Absent: "<entity_name>-<entity_version>".
    name: str | None = None
    #: A registered model's display name, e.g. ${resources.registered_models.forecast.name}.
    entity_name: str
    #: Its Vertex version id.
    entity_version: str = Field(pattern=r"^\d+$")
    workload_size: Literal["Small", "Medium", "Large"] = "Small"
    workload_type: Literal["CPU", "GPU_SMALL", "GPU_MEDIUM", "GPU_LARGE"] = "CPU"
    scale_to_zero_enabled: bool = False

    @model_validator(mode="after")
    def _vertex_keeps_a_replica(self):
        if self.scale_to_zero_enabled:
            raise ValueError("scale_to_zero_enabled: Vertex AI endpoints keep at least one replica")
        return self

    @property
    def served_name(self) -> str:
        return self.name or f"{self.entity_name}-{self.entity_version}"


class Route(_Strict):
    served_entity_name: str
    traffic_percentage: int = Field(ge=0, le=100)


class TrafficConfig(_Strict):
    routes: list[Route] = Field(min_length=1)


class EndpointCoreConfigInput(_Strict):
    served_entities: list[ServedEntityInput] = Field(min_length=1)
    traffic_config: TrafficConfig | None = None

    @model_validator(mode="after")
    def _routes(self):
        names = [e.served_name for e in self.served_entities]
        if len(names) != len(set(names)):
            raise ValueError(f"served entity names repeat: {names}")
        if self.traffic_config is None:
            if len(names) > 1:
                raise ValueError("several served_entities need traffic_config")
            return self
        routed = [r.served_entity_name for r in self.traffic_config.routes]
        unknown = sorted(set(routed) - set(names))
        if unknown:
            raise ValueError(f"traffic_config routes name no served entity: {unknown}")
        total = sum(r.traffic_percentage for r in self.traffic_config.routes)
        if total != 100:
            raise ValueError(f"traffic_config routes add up to {total}, not 100")
        return self

    def traffic(self) -> dict[str, int]:
        if self.traffic_config is None:
            return {self.served_entities[0].served_name: 100}
        split = {r.served_entity_name: r.traffic_percentage for r in self.traffic_config.routes}
        return {e.served_name: split.get(e.served_name, 0) for e in self.served_entities}


class EndpointTag(_Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    value: str = Field(default="", pattern=r"^[a-z0-9_-]{0,63}$")


class ModelServingEndpoint(_Strict):
    #: Display name of the endpoint.
    name: str
    description: str | None = None
    config: EndpointCoreConfigInput
    #: Labels on the endpoint.
    tags: list[EndpointTag] = []


# --- desired state ------------------------------------------------------------------------

def deployment_spec(entity: ServedEntityInput) -> dict:
    machine, accelerator, count = WORKLOAD_TYPES[entity.workload_type]
    lo, hi = WORKLOAD_SIZES[entity.workload_size]
    return {"machine_type": machine, "accelerator_type": accelerator, "accelerator_count": count,
            "min_replica_count": lo, "max_replica_count": hi}


def plan(experiments: dict[str, Experiment], models: dict[str, RegisteredModel],
         endpoints: dict[str, ModelServingEndpoint], names: dict[str, dict[str, str]],
         labels: dict[str, str]) -> dict:
    """What deploy makes true. `names` holds each resource's target-resolved name by kind
    and key (bundle.ml_names); `labels` marks the bundle's endpoints."""
    return {
        "experiments": [names["experiments"][k] for k in sorted(experiments)],
        "registered_models": [
            {"display_name": names["registered_models"][k], "description": m.comment,
             "aliases": {a.alias_name: str(a.version_num) for a in m.aliases}}
            for k, m in sorted(models.items())],
        "model_serving_endpoints": [
            {"display_name": names["model_serving_endpoints"][k], "description": e.description or "",
             "labels": labels | {t.key: t.value for t in e.tags},
             "deployments": [{"name": s.served_name, "model": s.entity_name,
                              "version": s.entity_version, **deployment_spec(s)}
                             for s in e.config.served_entities],
             "traffic": e.config.traffic()}
            for k, e in sorted(endpoints.items())],
    }


# --- applying it --------------------------------------------------------------------------

class Vertex(Protocol):
    def ensure_experiment(self, name: str) -> None: ...
    def find_model(self, display_name: str) -> str | None: ...
    def update_model(self, model: str, description: str) -> None: ...
    def version_aliases(self, model: str) -> dict[str, list[str]]: ...
    def move_alias(self, model: str, alias: str, version: str) -> None: ...
    def find_endpoint(self, display_name: str) -> str | None: ...
    def create_endpoint(self, display_name: str, description: str, labels: dict) -> str: ...
    def update_endpoint(self, endpoint: str, description: str, labels: dict) -> None: ...
    def deployed(self, endpoint: str) -> list[dict]: ...
    def deploy(self, endpoint: str, model: str, version: str, name: str, spec: dict,
               traffic_percentage: int) -> str: ...
    def set_traffic(self, endpoint: str, split: dict[str, int]) -> None: ...
    def undeploy(self, endpoint: str, deployed_id: str) -> None: ...
    def bundle_endpoints(self, labels: dict) -> list[str]: ...
    def set_retired(self, kind: str, name: str, since: str | None) -> bool: ...


_SPEC_KEYS = ("machine_type", "accelerator_type", "accelerator_count", "min_replica_count",
              "max_replica_count")


def _matches(current: dict, want: dict, model: str) -> bool:
    return (current["display_name"] == want["name"] and current["model"] == model
            and current["version"] == want["version"]
            and all(current[k] == want[k] for k in _SPEC_KEYS))


def apply(desired: dict, vx: Vertex, labels: dict, previous: dict | None = None, today: str = "",
          log=print) -> list[str]:
    """Walks Vertex to `desired`, then retires what the bundle no longer declares: what
    `previous` (the last deployment.json's resources) named, and endpoints labelled with
    `labels`. Returns the retired ones as "<kind> <name>"."""
    for name in desired["experiments"]:
        vx.ensure_experiment(name)
        vx.set_retired("experiment", name, None)
        log(f"ok   experiment {name}")
    for m in desired["registered_models"]:
        model = vx.find_model(m["display_name"])
        if model is None:
            if m["aliases"]:
                raise RuntimeError(f"registered model {m['display_name']}: aliases declared, "
                                   f"but nothing is registered yet")
            log(f"ok   model {m['display_name']}: not registered yet, the first upload creates it")
            continue
        if m["description"] is not None:
            vx.update_model(model, m["description"])
        vx.set_retired("model", m["display_name"], None)
        held = {a: v for v, aliases in vx.version_aliases(model).items() for a in aliases}
        for alias, version in sorted(m["aliases"].items()):
            if held.get(alias) != version:
                vx.move_alias(model, alias, version)
                log(f"ok   model {m['display_name']}: @{alias} -> version {version}")
        log(f"ok   model {m['display_name']}")
    for e in desired["model_serving_endpoints"]:
        _apply_endpoint(e, vx, log)
    previous = previous or {}
    gone = {
        "experiment": set(previous.get("experiments", {}).values()) - set(desired["experiments"]),
        "model": (set(previous.get("registered_models", {}).values())
                  - {m["display_name"] for m in desired["registered_models"]}),
        "endpoint": ((set(previous.get("model_serving_endpoints", {}).values()) | set(vx.bundle_endpoints(labels)))
                     - {e["display_name"] for e in desired["model_serving_endpoints"]}),
    }
    retired = []
    for kind, names in gone.items():
        for name in sorted(names):
            if kind == "endpoint":
                _undeploy_all(name, vx, log)
            if vx.set_retired(kind, name, today):
                retired.append(f"{kind} {name}")
                log(f"ok   {kind} {name}: no longer declared; retired ({RETIRED}={today})")
    return retired


def _undeploy_all(name: str, vx: Vertex, log) -> None:
    """Stops a retired endpoint serving: traffic onto one deployed model, the rest undeployed,
    then that one."""
    endpoint = vx.find_endpoint(name)
    current = vx.deployed(endpoint) if endpoint else []
    if not current:
        return
    keep, rest = current[0]["id"], [c["id"] for c in current[1:]]
    if rest:
        vx.set_traffic(endpoint, {keep: 100, **{r: 0 for r in rest}})
    for d in [*rest, keep]:
        vx.undeploy(endpoint, d)
    log(f"ok   endpoint {name}: undeployed {len(current)} model(s)")


def _apply_endpoint(e: dict, vx: Vertex, log) -> None:
    name = e["display_name"]
    endpoint = vx.find_endpoint(name)
    if endpoint is None:
        endpoint = vx.create_endpoint(name, e["description"], e["labels"])
        log(f"ok   endpoint {name}: created")
    else:
        vx.update_endpoint(endpoint, e["description"], e["labels"])
    current = vx.deployed(endpoint)
    ids: dict[str, str] = {}
    for want in e["deployments"]:
        model = vx.find_model(want["model"])
        if model is None:
            raise RuntimeError(f"endpoint {name}: served entity {want['name']} names model "
                               f"{want['model']!r}, which is not registered")
        same = next((c for c in current if _matches(c, want, model)), None)
        if same:
            ids[want["name"]] = same["id"]
            continue
        # A new copy starts without traffic unless the endpoint has nothing deployed yet.
        first = not current and not ids
        ids[want["name"]] = vx.deploy(endpoint, model, want["version"], want["name"],
                                      {k: want[k] for k in _SPEC_KEYS}, 100 if first else 0)
        log(f"ok   endpoint {name}: deployed {want['name']} (version {want['version']})")
    vx.set_traffic(endpoint, {ids[n]: pct for n, pct in e["traffic"].items()})
    for c in current:
        if c["id"] not in ids.values():
            vx.undeploy(endpoint, c["id"])
            log(f"ok   endpoint {name}: undeployed {c['display_name']}")
    log(f"ok   endpoint {name}: traffic {e['traffic']}")


class VertexSdk:
    """Vertex over google-cloud-aiplatform."""

    def __init__(self, project: str, location: str):
        from google.cloud import aiplatform

        self.ai = aiplatform
        self.project, self.location = project, location
        aiplatform.init(project=project, location=location)
        self._session = None

    def ensure_experiment(self, name: str) -> None:
        self.ai.Experiment.get_or_create(name)

    def find_model(self, display_name: str) -> str | None:
        hits = self.ai.Model.list(filter=f'display_name="{display_name}"', order_by="create_time desc")
        return hits[0].resource_name if hits else None

    def update_model(self, model: str, description: str) -> None:
        self.ai.Model(model).update(description=description)

    def _registry(self, model: str):
        from google.cloud.aiplatform.models import ModelRegistry

        return ModelRegistry(model=model)

    def version_aliases(self, model: str) -> dict[str, list[str]]:
        return {v.version_id: list(v.version_aliases or [])
                for v in self._registry(model).list_versions()}

    def move_alias(self, model: str, alias: str, version: str) -> None:
        registry = self._registry(model)
        for v, aliases in self.version_aliases(model).items():
            if alias in aliases:
                registry.remove_version_aliases(target_aliases=[alias], version=v)
        registry.add_version_aliases(new_aliases=[alias], version=version)

    def find_endpoint(self, display_name: str) -> str | None:
        hits = self.ai.Endpoint.list(filter=f'display_name="{display_name}"')
        return hits[0].resource_name if hits else None

    def create_endpoint(self, display_name: str, description: str, labels: dict) -> str:
        return self.ai.Endpoint.create(display_name=display_name, description=description,
                                       labels=labels).resource_name

    def update_endpoint(self, endpoint: str, description: str, labels: dict) -> None:
        self.ai.Endpoint(endpoint).update(description=description, labels=labels)

    def deployed(self, endpoint: str) -> list[dict]:
        out = []
        for dm in self.ai.Endpoint(endpoint).gca_resource.deployed_models:
            ms, dr = dm.dedicated_resources.machine_spec, dm.dedicated_resources
            accelerator = ms.accelerator_type.name if ms.accelerator_count else ""
            out.append({"id": dm.id, "display_name": dm.display_name, "model": dm.model.split("@")[0],
                        "version": dm.model_version_id, "machine_type": ms.machine_type,
                        "accelerator_type": accelerator, "accelerator_count": ms.accelerator_count,
                        "min_replica_count": dr.min_replica_count,
                        "max_replica_count": dr.max_replica_count})
        return out

    def deploy(self, endpoint: str, model: str, version: str, name: str, spec: dict,
               traffic_percentage: int) -> str:
        before = {d["id"] for d in self.deployed(endpoint)}
        extra = ({"accelerator_type": spec["accelerator_type"],
                  "accelerator_count": spec["accelerator_count"]} if spec["accelerator_count"] else {})
        self.ai.Model(f"{model}@{version}").deploy(
            endpoint=self.ai.Endpoint(endpoint), deployed_model_display_name=name,
            machine_type=spec["machine_type"], min_replica_count=spec["min_replica_count"],
            max_replica_count=spec["max_replica_count"], traffic_percentage=traffic_percentage,
            sync=True, **extra)
        return next(d["id"] for d in self.deployed(endpoint) if d["id"] not in before)

    def set_traffic(self, endpoint: str, split: dict[str, int]) -> None:
        self.ai.Endpoint(endpoint).update(traffic_split=split)

    def undeploy(self, endpoint: str, deployed_id: str) -> None:
        self.ai.Endpoint(endpoint).undeploy(deployed_model_id=deployed_id)

    def bundle_endpoints(self, labels: dict) -> list[str]:
        query = " AND ".join(f"labels.{k}={v}" for k, v in sorted(labels.items()))
        return [e.display_name for e in self.ai.Endpoint.list(filter=query)]

    def _rest(self, method: str, resource: str, **kw):
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(creds)
        return self._session.request(method, f"https://{self.location}-aiplatform.googleapis.com/v1/{resource}", **kw)

    def set_retired(self, kind: str, name: str, since: str | None) -> bool:
        """Labels the experiment, model or endpoint `name` retired since `since`, or lifts the
        label (None). Its other labels stay; an earlier retirement keeps its date. False when
        there is no such resource."""
        if kind == "experiment":
            resource = f"projects/{self.project}/locations/{self.location}/metadataStores/default/contexts/{name}"
        else:
            resource = self.find_model(name) if kind == "model" else self.find_endpoint(name)
            if resource is None:
                return False
        r = self._rest("GET", resource)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        labels = r.json().get("labels") or {}
        if (RETIRED in labels) == (since is not None):
            return True
        labels = {**labels, RETIRED: since} if since is not None else {k: v for k, v in labels.items() if k != RETIRED}
        self._rest("PATCH", resource, params={"updateMask": "labels"}, json={"labels": labels}).raise_for_status()
        return True
