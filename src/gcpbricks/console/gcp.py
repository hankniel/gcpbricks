"""The GCP calls the console makes, in one place: the service logic talks to this, tests to
a fake with the same methods."""
from __future__ import annotations

import copy
import json
import threading
import time
import urllib.parse

WORKFLOWS = "https://workflows.googleapis.com/v1"
EXECUTIONS = "https://workflowexecutions.googleapis.com/v1"
SCHEDULER = "https://cloudscheduler.googleapis.com/v1"
RUN = "https://run.googleapis.com/v2"
LOGGING = "https://logging.googleapis.com/v2"
STORAGE = "https://storage.googleapis.com/storage/v1"
BIGQUERY = "https://bigquery.googleapis.com/bigquery/v2"
LINEAGE = "https://datalineage.googleapis.com/v1"
MONITORING = "https://monitoring.googleapis.com/v3"
CATALOG = "https://cloudbilling.googleapis.com/v1"
COMPUTE = "https://compute.googleapis.com/compute/v1"


class Gcp:
    def __init__(self, project: str, region: str, session=None):
        if session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            if hasattr(creds, "with_quota_project"):
                creds = creds.with_quota_project(project)
            session = AuthorizedSession(creds)
        self.s = session
        #: What run, cancel, pause, alias and delete calls go through: the console's own
        #: session, or a user's (as_user).
        self.w = session
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self.project = project
        self.region = region
        self.parent = f"projects/{project}/locations/{region}"

    def _get(self, url: str, **params) -> dict:
        r = self.s.get(url, params=params)
        r.raise_for_status()
        return r.json()

    def as_user(self, token: str) -> Gcp:
        """This client, writing with a user's own access token: GCP's IAM decides whether they
        may, and its audit logs name them. Reads stay the console's."""
        import requests

        g = copy.copy(self)
        g.w = requests.Session()
        g.w.headers["Authorization"] = f"Bearer {token}"
        return g

    def _post(self, url: str, body: dict | None = None, write: bool = False) -> dict:
        r = (self.w if write else self.s).post(url, json=body or {})
        r.raise_for_status()
        return r.json() if r.content else {}

    def _cached(self, key: str, ttl: float, read):
        """`read()`, kept `ttl` seconds: the lists every page asks for again."""
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        out = read()
        with self._lock:
            self._cache[key] = (time.monotonic(), out)
        return out

    def forget(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def _pages(self, url: str, key: str, **params) -> list[dict]:
        out, token = [], None
        while True:
            page = self._get(url, **params, **({"pageToken": token} if token else {}))
            out += page.get(key, [])
            token = page.get("nextPageToken")
            if not token:
                return out

    # --- Workflows --------------------------------------------------------------------

    def workflows(self) -> list[dict]:
        """Every Workflow, kept 30s: which ones are bundle jobs changes only on a deploy."""
        return self._cached("workflows", 30, lambda: self._pages(
            f"{WORKFLOWS}/{self.parent}/workflows", "workflows", pageSize=500))

    def workflow(self, name: str) -> dict:
        return self._get(f"{WORKFLOWS}/{self.parent}/workflows/{name}")

    def executions(self, workflow: str, n: int) -> list[dict]:
        return self._get(f"{EXECUTIONS}/{self.parent}/workflows/{workflow}/executions",
                         view="FULL", pageSize=n).get("executions", [])

    def execution(self, workflow: str, execution_id: str) -> dict:
        return self._get(f"{EXECUTIONS}/{self.parent}/workflows/{workflow}/executions/{execution_id}")

    def start(self, workflow: str, argument: dict) -> dict:
        return self._post(f"{EXECUTIONS}/{self.parent}/workflows/{workflow}/executions",
                          {"argument": json.dumps(argument)}, write=True)

    def delete_workflow(self, name: str) -> None:
        self.forget("workflows")
        r = self.w.delete(f"{WORKFLOWS}/{self.parent}/workflows/{name}")
        if r.status_code != 404:
            r.raise_for_status()

    def cancel(self, workflow: str, execution_id: str) -> dict:
        return self._post(f"{EXECUTIONS}/{self.parent}/workflows/{workflow}/executions/{execution_id}:cancel", write=True)

    # --- Cloud Run, Scheduler, Storage ------------------------------------------------

    def run_execution(self, name: str) -> dict:
        return self._get(f"{RUN}/{name}")

    def cancel_run_execution(self, name: str) -> dict:
        return self._post(f"{RUN}/{name}:cancel", write=True)

    def schedulers(self) -> list[dict]:
        """Every Cloud Scheduler job, kept 30s; a pause or resume here drops the copy."""
        return self._cached("schedulers", 30, lambda: self._pages(
            f"{SCHEDULER}/{self.parent}/jobs", "jobs", pageSize=500))

    def set_scheduler(self, name: str, paused: bool) -> dict:
        self.forget("schedulers")
        return self._post(f"{SCHEDULER}/{self.parent}/jobs/{name}:{'pause' if paused else 'resume'}", write=True)

    def read_json(self, bucket: str, obj: str) -> dict:
        r = self.s.get(f"{STORAGE}/b/{bucket}/o/{urllib.parse.quote(obj, safe='')}",
                       params={"alt": "media"})
        r.raise_for_status()
        return r.json()

    def list_objects(self, bucket: str, prefix: str, match_glob: str | None = None) -> list[dict]:
        """Objects under `prefix` (matching `match_glob` when given). Always scoped by a
        prefix: a glob alone scans the whole bucket, seconds on one this size."""
        return self._pages(f"{STORAGE}/b/{bucket}/o", "items", prefix=prefix,
                           **({"matchGlob": match_glob} if match_glob else {}))

    def read_json_or_none(self, bucket: str, obj: str) -> dict | None:
        r = self.s.get(f"{STORAGE}/b/{bucket}/o/{urllib.parse.quote(obj, safe='')}",
                       params={"alt": "media"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def list_dir(self, bucket: str, prefix: str) -> tuple[list[str], list[dict]]:
        """One level under `prefix`, as a folder: its sub-prefixes and its objects."""
        prefixes, items, token = [], [], None
        while True:
            r = self._get(f"{STORAGE}/b/{bucket}/o", prefix=prefix, delimiter="/", pageSize=1000,
                          **({"pageToken": token} if token else {}))
            prefixes += r.get("prefixes", [])
            items += r.get("items", [])
            token = r.get("nextPageToken")
            if not token:
                return prefixes, items

    def read_object(self, bucket: str, obj: str) -> tuple[bytes, str] | None:
        """An object's bytes and generation; None when there is none."""
        r = self.s.get(f"{STORAGE}/b/{bucket}/o/{urllib.parse.quote(obj, safe='')}", params={"alt": "media"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content, r.headers.get("x-goog-generation", "")

    def object_generation(self, bucket: str, obj: str) -> str | None:
        r = self.s.get(f"{STORAGE}/b/{bucket}/o/{urllib.parse.quote(obj, safe='')}", params={"fields": "generation"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return str(r.json().get("generation", ""))

    def write_object(self, bucket: str, obj: str, data: bytes, content_type: str,
                     if_generation: str | None = None) -> str:
        """Writes `obj`, only over generation `if_generation` when given ("0": only if absent);
        returns the new generation. A stale generation raises HTTPError 412."""
        params = {"uploadType": "media", "name": obj}
        if if_generation is not None:
            params["ifGenerationMatch"] = if_generation
        r = self.s.post(f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o", params=params,
                        data=data, headers={"Content-Type": content_type})
        r.raise_for_status()
        return str(r.json().get("generation", ""))

    def delete_object(self, bucket: str, obj: str) -> None:
        r = self.s.delete(f"{STORAGE}/b/{bucket}/o/{urllib.parse.quote(obj, safe='')}")
        if r.status_code != 404:
            r.raise_for_status()

    def run_jobs(self) -> list[dict]:
        return self._pages(f"{RUN}/{self.parent}/jobs", "jobs")

    def run_services(self) -> list[dict]:
        return self._pages(f"{RUN}/{self.parent}/services", "services")

    def run_executions(self, job: str, n: int) -> list[dict]:
        """A Cloud Run job's latest executions, newest first."""
        return self._get(f"{RUN}/{self.parent}/jobs/{job}/executions", pageSize=n).get("executions", [])

    # --- BigQuery and its lineage -----------------------------------------------------

    def bq_get(self, path: str, project: str | None = None, **params) -> dict:
        """GET under a project (default the console's): "datasets", "datasets/<d>/tables/<t>/data"..."""
        return self._get(f"{BIGQUERY}/projects/{project or self.project}/{path}", **params)

    def bq_list(self, path: str, key: str, **params) -> list[dict]:
        return self._pages(f"{BIGQUERY}/projects/{self.project}/{path}", key, **params)

    def bq_query(self, body: dict) -> dict:
        r = self._post(f"{BIGQUERY}/projects/{self.project}/queries", body)
        for _ in range(6):
            if r.get("jobComplete"):
                break
            ref = r["jobReference"]
            r = self._get(f"{BIGQUERY}/projects/{self.project}/queries/{ref['jobId']}",
                          location=ref.get("location"), timeoutMs=10000)
        if not r.get("jobComplete"):
            raise TimeoutError("the query didn't finish within a minute")
        return r

    def lineage_links(self, location: str, end: str, fqn: str) -> list[dict]:
        """Data Lineage links with `fqn` as their `end` ("source" or "target")."""
        url = f"{LINEAGE}/projects/{self.project}/locations/{location}:searchLinks"
        out, token = [], None
        while True:
            page = self._post(url, {end: {"fullyQualifiedName": fqn}, "pageSize": 100,
                                    **({"pageToken": token} if token else {})})
            out += page.get("links", [])
            token = page.get("nextPageToken")
            if not token:
                return out

    # --- Vertex AI --------------------------------------------------------------------

    @property
    def _ai(self) -> str:
        return f"https://{self.region}-aiplatform.googleapis.com/v1"

    def ai_get(self, path: str, **params) -> dict:
        """GET a Vertex AI resource; `path` is relative to the project's location or, when it
        starts with "projects/", absolute."""
        base = path if path.startswith("projects/") else f"{self.parent}/{path}"
        return self._get(f"{self._ai}/{base}", **params)

    def ai_list(self, path: str, key: str, **params) -> list[dict]:
        base = path if path.startswith("projects/") else f"{self.parent}/{path}"
        return self._pages(f"{self._ai}/{base}", key, **params)

    def ai_post(self, path: str, body: dict) -> dict:
        return self._post(f"{self._ai}/{path}", body, write=True)

    def ai_patch(self, path: str, body: dict, update_mask: str) -> dict:
        """PATCH a Vertex AI resource, relative to the project's location, on `update_mask`."""
        r = self.s.patch(f"{self._ai}/{self.parent}/{path}", params={"updateMask": update_mask}, json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    def ai_delete(self, path: str) -> dict:
        """DELETE a Vertex AI resource, relative to the project's location; returns the operation."""
        r = self.w.delete(f"{self._ai}/{self.parent}/{path}")
        r.raise_for_status()
        return r.json() if r.content else {}

    # --- Monitoring -------------------------------------------------------------------

    def time_series(self, flt: str, start: str, end: str, aligner: str, period: str = "60s") -> list[dict]:
        return self._pages(f"{MONITORING}/projects/{self.project}/timeSeries", "timeSeries", filter=flt,
                           **{"interval.startTime": start, "interval.endTime": end,
                              "aggregation.alignmentPeriod": period, "aggregation.perSeriesAligner": aligner})

    def open_alerts(self) -> list[dict]:
        return self._pages(f"{MONITORING}/projects/{self.project}/alerts", "alerts", filter='state="OPEN"')

    # --- Logging ----------------------------------------------------------------------

    def log_entries(self, flt: str, n: int = 500, newest_first: bool = False) -> list[dict]:
        """Matching entries; a read over Logging's quota (60 a minute per project) waits and
        tries once more."""
        body = {"resourceNames": [f"projects/{self.project}"], "filter": flt,
                "orderBy": "timestamp desc" if newest_first else "timestamp asc", "pageSize": n}
        r = self.s.post(f"{LOGGING}/entries:list", json=body)
        if r.status_code == 429:
            time.sleep(3)
            r = self.s.post(f"{LOGGING}/entries:list", json=body)
        r.raise_for_status()
        return r.json().get("entries", [])

    def log_entries_all(self, flt: str, limit: int = 5000) -> list[dict]:
        """Matching entries, oldest first, page after page up to `limit`."""
        out, token = [], None
        while len(out) < limit:
            body = {"resourceNames": [f"projects/{self.project}"], "filter": flt, "orderBy": "timestamp asc",
                    "pageSize": min(1000, limit - len(out)), **({"pageToken": token} if token else {})}
            r = self.s.post(f"{LOGGING}/entries:list", json=body)
            if r.status_code == 429:
                time.sleep(3)
                r = self.s.post(f"{LOGGING}/entries:list", json=body)
            r.raise_for_status()
            page = r.json()
            out += page.get("entries", [])
            token = page.get("nextPageToken")
            if not token:
                break
        return out

    # --- prices and machine shapes ------------------------------------------------------

    def catalog_skus(self, service_id: str) -> list[dict]:
        """A Cloud Billing Catalog service's SKUs (list prices, USD), kept a day."""
        return self._cached(f"skus:{service_id}", 24 * 3600, lambda: self._pages(
            f"{CATALOG}/services/{service_id}/skus", "skus", pageSize=5000, currencyCode="USD"))

    def machine_type(self, zone: str, machine_type: str) -> dict:
        """A Compute Engine machine type (guestCpus, memoryMb), kept a day."""
        return self._cached(f"mt:{zone}/{machine_type}", 24 * 3600, lambda: self._get(
            f"{COMPUTE}/projects/{self.project}/zones/{zone}/machineTypes/{machine_type}"))

    def audit(self, payload: dict) -> None:
        self._post(f"{LOGGING}/entries:write", {"entries": [{
            "logName": f"projects/{self.project}/logs/mlops-console",
            "resource": {"type": "global", "labels": {"project_id": self.project}},
            "severity": "NOTICE", "jsonPayload": payload}]})

