"""The console's HTTP API and its web app, served behind Identity-Aware Proxy.

  uvicorn gcpbricks.console.app:app

Env:
  MLOPS_PROJECT, MLOPS_REGION   where the bundles' Workflows run
  MLOPS_IAP_AUDIENCE            IAP's audience for this service: every request's signed IAP
                                assertion is verified against it. Unset only for local
                                development (MLOPS_CONSOLE_DEV=1), where the user is "local".
  MLOPS_CONSOLE_STATIC          the built web app (console/web/dist) to serve at /
  MLOPS_OAUTH_CLIENT_ID,        a Google OAuth web client: "Allow GCP access" gets each user's
  MLOPS_OAUTH_CLIENT_SECRET     own token, which their run, repair, cancel, pause, alias and
                                delete calls and their Colab runtime (notebooks) go through
  MLOPS_WORKSPACE               where workspace notebooks live (gs://<bucket>/workspace)
  MLOPS_BLOG                    where the blog's posts and images live (gs://<bucket>/blog)
  MLOPS_PLATFORM_GIT            this repo, which "Connect Claude" installs mlops-notebooks-mcp from
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from gcpbricks.console import batch as B
from gcpbricks.console import blog as BL
from gcpbricks.console import bundles as BN
from gcpbricks.console import catalog as C
from gcpbricks.console import costs as COSTS
from gcpbricks.console import dashboards as D
from gcpbricks.console import failure as F
from gcpbricks.console import ml as M
from gcpbricks.console import notebooks as NB
from gcpbricks.console import pipeline_view as PV
from gcpbricks.console import runs as RUNS
from gcpbricks.console import service as S
from gcpbricks.console import stats as ST
from gcpbricks.console.gcp import Gcp

IAP_CERTS = "https://www.gstatic.com/iap/verify/public_key-jwk"

app = FastAPI(title="gcpbricks console")
# The web app is ~3 MB of JS and CSS, and the API's JSON compresses well too.
app.add_middleware(GZipMiddleware, minimum_size=1024)


class _Assets(StaticFiles):
    """The build's assets: their names carry a content hash, so a browser keeps them for good."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


@lru_cache(maxsize=1)
def gcp() -> Gcp:
    return Gcp(os.environ["MLOPS_PROJECT"], os.environ.get("MLOPS_REGION", "us-central1"))


PLATFORM_GIT = os.environ.get("MLOPS_PLATFORM_GIT",
                              "git+ssh://git@github.com/hankniel/gcpbricks.git")

#: Who may delete what deploy retired: comma-separated emails.
ADMINS = {e.strip() for e in os.environ.get("MLOPS_CONSOLE_ADMINS", "").split(",") if e.strip()}


def is_admin(who: str) -> bool:
    return who in ADMINS or (who == "local" and os.environ.get("MLOPS_CONSOLE_DEV") == "1")


def user(request: Request) -> str:
    """The signed-in user, from IAP's signed assertion: the plain email header alone could be
    forged by anything that reaches the service around IAP."""
    audience = os.environ.get("MLOPS_IAP_AUDIENCE")
    if not audience:
        if os.environ.get("MLOPS_CONSOLE_DEV") == "1":
            return "local"
        raise HTTPException(500, "MLOPS_IAP_AUDIENCE is not set")
    token = request.headers.get("x-goog-iap-jwt-assertion")
    if not token:
        raise HTTPException(401, "not signed in through IAP")
    from google.auth.transport import requests as greq
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_token(token, greq.Request(), audience=audience,
                                       certs_url=IAP_CERTS)
    except Exception as e:  # noqa: BLE001 - any failure to verify means not signed in
        raise HTTPException(401, f"invalid IAP assertion: {type(e).__name__}") from None
    return claims["email"]


def admin(who: str = Depends(user)) -> str:
    if not is_admin(who):
        raise HTTPException(403, "only the console's admins may delete")
    return who


# "Allow GCP access": Google's OAuth for the user's own token, kept in an HttpOnly cookie
# scoped to the API and bound to the IAP user it was granted for. The redirect stays under the
# notebook API, where the OAuth client registers it.
OAUTH_ID = os.environ.get("MLOPS_OAUTH_CLIENT_ID", "")
OAUTH_SECRET = os.environ.get("MLOPS_OAUTH_CLIENT_SECRET", "")
TOKEN_COOKIE, STATE_COOKIE = "mlops_gcp", "mlops_nb_state"
#: A 401 whose detail starts with this sends the web app through the OAuth start.
NEEDS_ACCESS = "gcp_access"
NOTEBOOK_API = "/api/notebooks"


def _redirect_uri(request: Request) -> str:
    return os.environ.get("MLOPS_OAUTH_REDIRECT") or f"https://{request.headers.get('host')}{NOTEBOOK_API}/auth/callback"


def _granted(request: Request, who: str) -> str | None:
    import time

    email, _, rest = (request.cookies.get(TOKEN_COOKIE) or "").partition("|")
    expires, _, token = rest.partition("|")
    if email != who or not token or not expires.isdigit() or int(expires) < time.time() + 60:
        return None
    return token


def notebook_token(request: Request, who: str = Depends(user)) -> str:
    token = _granted(request, who)
    if token is None:
        raise HTTPException(401, f"{NEEDS_ACCESS}: GCP access not allowed yet, or expired: allow it again")
    return token


def gcp_as_user(request: Request, who: str = Depends(user)) -> Gcp:
    """The console's client, its writes as the signed-in user: what they may change is what
    their own GCP roles let them. An admin (MLOPS_CONSOLE_ADMINS, and local development)
    writes as the console's own service account, needing no sign-in of their own."""
    if is_admin(who):
        return gcp()
    return gcp().as_user(notebook_token(request, who))


#: A write endpoint's client: its calls as the signed-in user.
AS_USER = Depends(gcp_as_user)


def _call(fn, *args, g: Gcp | None = None):
    try:
        return fn(g or gcp(), *args)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\"")) from None
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise HTTPException(404, "not found") from None
        if e.response is not None and e.response.status_code == 403:
            raise HTTPException(403, f"GCP denied it: {_gcp_message(e.response)}") from None
        if e.response is not None and e.response.status_code == 429:
            raise HTTPException(503, "GCP is rate-limiting the console's reads (Cloud Logging allows 60 a minute): "
                                     "this page tries again on its next refresh") from None
        raise


def _gcp_message(response) -> str:
    try:
        return response.json()["error"]["message"]
    except Exception:  # noqa: BLE001 - not GCP's error shape
        return response.text[:300]


class RunBody(BaseModel):
    params: dict[str, str] = {}
    #: Run these tasks only; empty: all of them.
    only: list[str] = []


class RepairBody(BaseModel):
    #: A batch_serve_task's models to run again in full; its failed ones resume anyway.
    models: list[str] = []
    #: Tasks that succeeded, to run again too.
    tasks: list[str] = []
    #: Tasks that didn't succeed, not to run again: they end as they did.
    keep: list[str] = []
    #: Parameter overrides, kept by later repairs: a value overrides the run's, null drops one.
    params: dict[str, str | None] | None = None
    #: The run's latest repair as the caller saw it (0: none); a newer one refuses.
    latest_repair_id: int | None = None
    #: A vertex_pipeline_task's pipeline steps to run again although they succeeded.
    steps: dict[str, list[str]] = {}
    #: vertex_pipeline_tasks to start as a new pipeline rather than a Vertex rerun.
    fresh: list[str] = []


@app.get("/api/me")
def me(who: str = Depends(user)):
    return {"email": who, "project": gcp().project, "region": gcp().region, "admin": is_admin(who),
            "workspace": NB.WORKSPACE, "platform_git": PLATFORM_GIT}


@app.get("/api/jobs")
def jobs(target: str | None = None, who: str = Depends(user)):
    return _call(S.list_jobs, target, 10)


@app.get("/api/jobs/{workflow}")
def job(workflow: str, who: str = Depends(user)):
    return _call(S.job_detail, workflow)


@app.get("/api/jobs/{workflow}/runs/{execution}")
def run(workflow: str, execution: str, repair_id: int | None = None, who: str = Depends(user)):
    return _call(S.run_detail, workflow, execution, repair_id)


@app.get("/api/jobs/{workflow}/runs/{execution}/tasks/{task}")
def task_run(workflow: str, execution: str, task: str, who: str = Depends(user)):
    return _call(S.task_run, workflow, execution, task)


@app.get("/api/jobs/{workflow}/runs/{execution}/tasks/{task}/notebook")
def task_notebook(workflow: str, execution: str, task: str, who: str = Depends(user)):
    return _call(S.notebook_run, workflow, execution, task)


@app.get("/api/jobs/{workflow}/runs/{execution}/tasks/{task}/pipeline")
def task_pipeline(workflow: str, execution: str, task: str, who: str = Depends(user)):
    return _call(S.pipeline_run, workflow, execution, task)


@app.get("/api/jobs/{workflow}/runs/{execution}/tasks/{task}/logs")
def logs(workflow: str, execution: str, task: str, who: str = Depends(user)):
    return _call(S.task_logs, workflow, execution, task)


@app.get("/api/jobs/{workflow}/runs/{execution}/tasks/{task}/stats")
def task_stats(workflow: str, execution: str, task: str, who: str = Depends(user)):
    return _call(ST.task_stats, workflow, execution, task)


@app.get("/api/jobs/{workflow}/tasks/{task}/pipeline")
def pipeline_definition(workflow: str, task: str, who: str = Depends(user)):
    return _call(PV.definition, workflow, task)


@app.get("/api/jobs/{workflow}/runs/{execution}/tasks/{task}/pipeline/steps/{step}")
def pipeline_step(workflow: str, execution: str, task: str, step: str, who: str = Depends(user)):
    return _call(PV.step, workflow, execution, task, step)


@app.get("/api/jobs/{workflow}/runs/{execution}/tasks/{task}/pipeline/steps/{step}/artifacts/{name}")
def pipeline_artifact(workflow: str, execution: str, task: str, step: str, name: str, who: str = Depends(user)):
    return _call(PV.artifact_text, workflow, execution, task, step, name)


@app.get("/api/bigquery/jobs/{project}/{location}/{job_id}")
def bigquery_job(project: str, location: str, job_id: str, who: str = Depends(user)):
    return _call(ST.bigquery_job, project, location, job_id)


@app.post("/api/jobs/{workflow}/run")
def run_now(workflow: str, body: RunBody, who: str = Depends(user), g: Gcp = AS_USER):
    return _call(S.run_now, who, workflow, body.params, body.only, g=g)


@app.get("/api/jobs/{workflow}/runs/{execution}/why")
def why_failed(workflow: str, execution: str, task: str | None = None, who: str = Depends(user)):
    return _call(F.why, workflow, execution, task)


@app.get("/api/jobs/{workflow}/runs/{execution}/repair")
def repair_plan(workflow: str, execution: str, who: str = Depends(user)):
    return _call(S.repair_plan, workflow, execution)


@app.post("/api/jobs/{workflow}/runs/{execution}/repair")
def repair(workflow: str, execution: str, body: RepairBody | None = None, who: str = Depends(user), g: Gcp = AS_USER):
    body = body or RepairBody()
    return _call(S.repair, who, workflow, execution, body.models, body.tasks, body.params, body.latest_repair_id,
                 body.steps, body.fresh, body.keep, g=g)


@app.post("/api/jobs/{workflow}/runs/{execution}/cancel")
def cancel(workflow: str, execution: str, who: str = Depends(user), g: Gcp = AS_USER):
    return _call(S.cancel, who, workflow, execution, g=g)


@app.delete("/api/jobs/{workflow}")
def delete_job(workflow: str, who: str = Depends(admin), g: Gcp = AS_USER):
    return _call(S.delete_job, who, workflow, g=g)


@app.post("/api/jobs/{workflow}/pause")
def pause(workflow: str, who: str = Depends(user), g: Gcp = AS_USER):
    return _call(S.set_paused, who, workflow, True, g=g)


@app.post("/api/jobs/{workflow}/resume")
def resume(workflow: str, who: str = Depends(user), g: Gcp = AS_USER):
    return _call(S.set_paused, who, workflow, False, g=g)


class AliasBody(BaseModel):
    alias: str
    #: None removes the alias.
    version: str | None = None


@app.get("/api/experiments")
def experiments(who: str = Depends(user)):
    return _call(M.experiments)


@app.get("/api/experiments/{experiment}")
def experiment(experiment: str, who: str = Depends(user)):
    return _call(M.experiment, experiment)


@app.delete("/api/experiments/{experiment}")
def delete_experiment(experiment: str, who: str = Depends(admin), g: Gcp = AS_USER):
    return _call(M.delete_experiment, who, experiment, g=g)


@app.get("/api/models")
def models(who: str = Depends(user)):
    return _call(M.models)


@app.get("/api/costs")
def costs(days: int = 30, who: str = Depends(user)):
    """What the platform's queries and compute cost over the last `days` days, and what runs
    now, at list price (console/costs.py); kept ten minutes per window."""
    if days not in (7, 30, 90):
        raise HTTPException(400, "a cost window is 7, 30 or 90 days")
    g = gcp()
    # K_SERVICE: the console's own Cloud Run service name, set by Cloud Run.
    return g._cached(f"costs:{days}", 600, lambda: COSTS.report(g, days, bq_location=os.environ.get("MLOPS_BQ_LOCATION", "us-central1"),
                                                                console=os.environ.get("K_SERVICE")))


@app.get("/api/models/{model}")
def model(model: str, who: str = Depends(user)):
    return _call(M.model, model)


@app.delete("/api/models/{model}")
def delete_model(model: str, who: str = Depends(admin), g: Gcp = AS_USER):
    return _call(M.delete_model, who, model, g=g)


@app.post("/api/models/{model}/aliases")
def set_alias(model: str, body: AliasBody, who: str = Depends(user), g: Gcp = AS_USER):
    return _call(M.set_alias, who, model, body.alias, body.version, g=g)


@app.get("/api/serving")
def endpoints(who: str = Depends(user)):
    return _call(M.endpoints)


@app.get("/api/serving/{endpoint}")
def endpoint(endpoint: str, who: str = Depends(user)):
    return _call(M.endpoint, endpoint)


@app.delete("/api/serving/{endpoint}")
def delete_endpoint(endpoint: str, who: str = Depends(admin), g: Gcp = AS_USER):
    return _call(M.delete_endpoint, who, endpoint, g=g)


@app.get("/api/features")
def features(who: str = Depends(user)):
    return _call(M.feature_groups)


@app.get("/api/features/{name}")
def feature_group(name: str, who: str = Depends(user)):
    return _call(M.feature_group, name)


@app.get("/api/models/{model}/versions/{version}/features")
def version_features(model: str, version: str, who: str = Depends(user)):
    return _call(M.version_features, model, version)


@app.get("/api/models/{model}/versions/{version}/training")
def version_training(model: str, version: str, who: str = Depends(user)):
    return _call(M.version_training, model, version)


@app.get("/api/models/{model}/versions/{version}/lineage")
def version_lineage(model: str, version: str, who: str = Depends(user)):
    return _call(C.version_lineage, model, version)


@app.get("/api/batch-serving")
def batch_tasks(who: str = Depends(user)):
    return _call(B.tasks)


@app.get("/api/batch-serving/{workflow}/{task}")
def batch_task(workflow: str, task: str, who: str = Depends(user)):
    return _call(B.task, workflow, task)


@app.get("/api/dashboards")
def dashboards(who: str = Depends(user)):
    return _call(D.list_dashboards)


@app.get("/api/dashboards/{bundle}/{target}/{key}")
def dashboard(bundle: str, target: str, key: str, who: str = Depends(user)):
    return _call(D.dashboard, bundle, target, key)


@app.get("/api/dashboards/{bundle}/{target}/{key}/widgets/{index}")
def dashboard_widget(bundle: str, target: str, key: str, index: int, request: Request, window: str | None = None,
                     who: str = Depends(user)):
    picked = {k[2:]: v for k, v in request.query_params.items() if k.startswith("f.")}
    return _call(D.widget, bundle, target, key, index, window, picked)


@app.get("/api/dashboards/{bundle}/{target}/{key}/filters/{name}")
def dashboard_filter(bundle: str, target: str, key: str, name: str, window: str | None = None,
                     who: str = Depends(user)):
    return _call(D.filter_values, bundle, target, key, name, window)


@app.get("/api/bundles")
def bundles(who: str = Depends(user)):
    return _call(BN.list_bundles)


@app.get("/api/bundles/{name}")
def bundle(name: str, who: str = Depends(user)):
    return _call(BN.bundle, name)


@app.get("/api/catalog")
def datasets(who: str = Depends(user)):
    return _call(C.datasets)


@app.get("/api/catalog/{dataset}")
def dataset(dataset: str, who: str = Depends(user)):
    return _call(C.dataset, dataset)


@app.get("/api/catalog/{dataset}/{table}")
def table(dataset: str, table: str, project: str | None = None, who: str = Depends(user)):
    return _call(C.table, dataset, table, project)


@app.get("/api/catalog/{dataset}/{table}/preview")
def preview(dataset: str, table: str, project: str | None = None, who: str = Depends(user)):
    return _call(C.preview, dataset, table, project)


@app.post("/api/catalog/{dataset}/{table}/query")
def query_preview(dataset: str, table: str, project: str | None = None, who: str = Depends(user)):
    return _call(C.query_preview, who, dataset, table, project)


@app.get("/api/lineage")
def lineage(node: str, who: str = Depends(user)):
    return _call(C.lineage, node)


# --- notebooks: the workspace, Colab compute, and cells run on the user's runtime ------------

class NotebookBody(BaseModel):
    notebook: dict
    #: The generation the editor read; "0" creates.
    generation: str | None = None


class PathBody(BaseModel):
    path: str


class ShareBody(BaseModel):
    #: "view": everyone in the console views it; "private": only the people added.
    general: str = "view"
    #: email -> "view" | "edit"
    people: dict[str, str] = {}


class TemplateBody(BaseModel):
    template: str
    #: The runtime's name; empty: <user>-<UTC time>.
    name: str = ""


class CodeBody(BaseModel):
    code: str
    # The notebook cell the code is, when the editor runs one: the run then keeps a run record,
    # so a page opened later (or Claude) sees the cell running, and how it ended.
    path: str | None = None
    cell_id: str | None = None
    index: int | None = None


@app.get("/api/workspace")
def workspace(path: str = "", who: str = Depends(user)):
    return _call(NB.list_folder, who, path, is_admin(who))


@app.get("/api/workspace/notebook")
def workspace_notebook(path: str, who: str = Depends(user)):
    return _call(NB.read_notebook, who, path, is_admin(who))


@app.get("/api/workspace/notebook/generation")
def workspace_notebook_generation(path: str, who: str = Depends(user)):
    return _call(NB.notebook_generation, who, path, is_admin(who))


@app.get("/api/workspace/notebook/runs")
def workspace_notebook_runs(path: str, request: Request, who: str = Depends(user)):
    """The notebook's runs of the last day, whoever made them (this editor, another tab,
    Claude), each with how it stands now: its record next to the runtime's state and the
    kernel's own (runs.liveness). The kernel is read with your notebook access; without it,
    its state is unknown."""
    import time

    _call(NB.notebook_generation, who, path, is_admin(who))  # may view it
    token = _granted(request, who)

    def runtime_for(rid):
        try:
            return NB.runtime(gcp(), rid)
        except Exception:  # noqa: BLE001 - deleted: the run's runtime is gone
            return None

    def kernel_for(rid, kid):
        if token is None:
            raise PermissionError("no notebook access")
        return NB.kernel_for(gcp(), who, rid, token).status(kid)
    return {"runs": RUNS.with_liveness(RUNS.list_runs(gcp(), path), runtime_for, kernel_for), "now": time.time()}


@app.get("/api/workspace/notebook/share")
def workspace_notebook_share(path: str, who: str = Depends(user)):
    return _call(NB.get_share, who, path, is_admin(who))


@app.put("/api/workspace/notebook/share")
def share_workspace_notebook(path: str, body: ShareBody, who: str = Depends(user)):
    return _call(NB.set_share, who, path, body.general, body.people, is_admin(who))


@app.put("/api/workspace/notebook")
def save_workspace_notebook(path: str, body: NotebookBody, who: str = Depends(user)):
    return _call(NB.save_notebook, who, path, body.notebook, body.generation, is_admin(who))


@app.post("/api/workspace/notebooks")
def new_workspace_notebook(body: PathBody, who: str = Depends(user)):
    return _call(NB.create_notebook, who, body.path, is_admin(who))


@app.post("/api/workspace/folders")
def new_workspace_folder(body: PathBody, who: str = Depends(user)):
    return _call(NB.create_folder, who, body.path, is_admin(who))


@app.delete("/api/workspace/notebook")
def delete_workspace_notebook(path: str, who: str = Depends(user)):
    return _call(NB.delete_notebook, who, path, is_admin(who))


# --- the blog: posts in Markdown and the images pasted into them ------------------------------

class NewPostBody(BaseModel):
    title: str


class PostBody(BaseModel):
    title: str
    tags: list[str] = []
    summary: str = ""
    body: str = ""
    #: The generation the editor read.
    generation: str | None = None


@app.get("/api/blog/posts")
def blog_posts(who: str = Depends(user)):
    return _call(BL.list_posts)


@app.post("/api/blog/posts")
def new_blog_post(body: NewPostBody, who: str = Depends(user)):
    return _call(BL.create_post, who, body.title)


@app.get("/api/blog/posts/{slug}")
def blog_post(slug: str, who: str = Depends(user)):
    return _call(BL.read_post, who, slug, is_admin(who))


@app.put("/api/blog/posts/{slug}")
def save_blog_post(slug: str, body: PostBody, who: str = Depends(user)):
    return _call(BL.save_post, who, slug, body.title, body.tags, body.summary, body.body, body.generation, is_admin(who))


@app.delete("/api/blog/posts/{slug}")
def delete_blog_post(slug: str, who: str = Depends(user)):
    return _call(BL.delete_post, who, slug, is_admin(who))


@app.post("/api/blog/posts/{slug}/images")
async def upload_blog_image(slug: str, request: Request, who: str = Depends(user)):
    data = await request.body()
    return await run_in_threadpool(_call, BL.upload_image, who, slug, data, request.headers.get("content-type", ""), is_admin(who))


@app.get("/api/blog/assets/{slug}/{name}")
def blog_image(slug: str, name: str, who: str = Depends(user)):
    data, content_type = _call(BL.read_image, slug, name)
    # Its name is random and never rewritten.
    return Response(data, media_type=content_type,
                    headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


@app.get("/api/compute/templates")
def runtime_templates(who: str = Depends(user)):
    return _call(NB.templates)


@app.post("/api/compute/templates")
def create_runtime_template(body: dict, who: str = Depends(user)):
    return _call(NB.create_template, who, body, is_admin(who))


@app.patch("/api/compute/templates/{template}")
def update_runtime_template(template: str, body: dict, who: str = Depends(user)):
    return _call(NB.update_template, who, template, body, is_admin(who))


@app.delete("/api/compute/templates/{template}")
def delete_runtime_template(template: str, who: str = Depends(user)):
    return _call(NB.delete_template, who, template, is_admin(who))


@app.get("/api/compute/runtimes")
def notebook_runtimes(who: str = Depends(user)):
    return _call(NB.runtimes)


@app.post("/api/compute/runtimes")
def create_notebook_runtime(body: TemplateBody, who: str = Depends(user)):
    return _call(NB.create_runtime, who, body.template, body.name)


@app.post("/api/compute/runtimes/{runtime}/{action}")
def notebook_runtime_action(runtime: str, action: str, who: str = Depends(user)):
    return _call(NB.set_runtime, who, runtime, action, is_admin(who))


@app.delete("/api/compute/runtimes/{runtime}")
def delete_notebook_runtime(runtime: str, who: str = Depends(user)):
    return _call(NB.set_runtime, who, runtime, "delete", is_admin(who))


@app.get(f"{NOTEBOOK_API}/auth")
def notebook_auth(request: Request, who: str = Depends(user)):
    return {"configured": bool(OAUTH_ID and OAUTH_SECRET), "allowed": _granted(request, who) is not None, "user": who}


@app.get(f"{NOTEBOOK_API}/auth/start")
def notebook_auth_start(request: Request, next: str = "/workspace", who: str = Depends(user)):
    import secrets
    import urllib.parse

    from fastapi.responses import RedirectResponse

    if not (OAUTH_ID and OAUTH_SECRET):
        raise HTTPException(503, "GCP access needs MLOPS_OAUTH_CLIENT_ID and MLOPS_OAUTH_CLIENT_SECRET")
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode({
        "client_id": OAUTH_ID, "redirect_uri": _redirect_uri(request), "response_type": "code",
        "scope": "openid email https://www.googleapis.com/auth/cloud-platform", "access_type": "online",
        "include_granted_scopes": "true", "login_hint": who, "state": state})
    r = RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{query}")
    r.set_cookie(STATE_COOKIE, f"{state}|{next if next.startswith('/') else '/workspace'}", max_age=600,
                 httponly=True, secure=True, samesite="lax", path=NOTEBOOK_API)
    return r


@app.get(f"{NOTEBOOK_API}/auth/callback")
def notebook_auth_callback(request: Request, code: str = "", state: str = "", error: str = "", who: str = Depends(user)):
    import time

    from fastapi.responses import RedirectResponse

    expected, _, next_page = (request.cookies.get(STATE_COOKIE) or "").partition("|")
    if error or not code or not state or state != expected:
        raise HTTPException(400, f"GCP access wasn't allowed: {error or 'the sign-in expired, try again'}")
    got = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "code": code, "client_id": OAUTH_ID, "client_secret": OAUTH_SECRET,
        "redirect_uri": _redirect_uri(request), "grant_type": "authorization_code"})
    got.raise_for_status()
    token = got.json()
    info = requests.get("https://oauth2.googleapis.com/tokeninfo", params={"access_token": token["access_token"]},
                        timeout=30).json()
    if info.get("email") != who:
        raise HTTPException(403, f"you allowed access as {info.get('email')}, but you're signed in as {who}")
    expires = int(time.time()) + int(token.get("expires_in", 3600))
    r = RedirectResponse(next_page or "/workspace")
    r.set_cookie(TOKEN_COOKIE, f"{who}|{expires}|{token['access_token']}", max_age=int(token.get("expires_in", 3600)),
                 httponly=True, secure=True, samesite="lax", path="/api")
    r.delete_cookie(STATE_COOKIE, path=NOTEBOOK_API)
    gcp().audit({"event": "mlops_console_action", "user": who, "action": "allow_gcp_access"})
    return r


def _kernel(who: str, runtime: str, token: str) -> NB.Kernel:
    try:
        return NB.kernel_for(gcp(), who, runtime, token)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


def _relay(fn):
    try:
        return fn()
    except PermissionError as e:
        raise HTTPException(401, str(e)) from None


@app.get(f"{NOTEBOOK_API}/runtimes/{{runtime}}/kernels")
def kernels(runtime: str, who: str = Depends(user), token: str = Depends(notebook_token)):
    return _relay(lambda: _kernel(who, runtime, token).kernels())


@app.post(f"{NOTEBOOK_API}/runtimes/{{runtime}}/kernels")
def start_kernel(runtime: str, who: str = Depends(user), token: str = Depends(notebook_token)):
    return _relay(lambda: _kernel(who, runtime, token).start())


# Before the kernel actions' route, which would take "execute" for an action.
@app.post(f"{NOTEBOOK_API}/runtimes/{{runtime}}/kernels/{{kernel}}/execute")
def execute(runtime: str, kernel: str, body: CodeBody, who: str = Depends(user),
            token: str = Depends(notebook_token)):
    """Runs a cell; its outputs stream back as they come, one JSON object per line, with a
    {"output_type": "heartbeat"} line while the cell is silent. A cell of a notebook (`path`,
    `cell_id`, `index`) keeps a run record: when this page goes away mid-run the record goes
    stale, and the next page shows the cell as still running on the kernel, uncaptured."""
    import json as _json

    from fastapi.responses import StreamingResponse

    k = _kernel(who, runtime, token)
    record = None
    if body.path and body.cell_id and body.index is not None:
        if NB.access(gcp(), who, body.path, is_admin(who)) not in ("edit", "manage"):
            raise HTTPException(403, f"{who} may not run {body.path}'s cells")
        record = RUNS.RunRecord(gcp(), who, body.path, "editor", runtime, kernel, [(body.index, body.cell_id)])

    def lines():
        outputs, at = [], body.index
        try:
            if record:
                record.start_cell(at)
            for out in k.execute(kernel, body.code):
                kind = out["output_type"]
                if record and kind == "done":
                    ok = out.get("status") == "ok"
                    record.end_cell(at, "ok" if ok else "error", out.get("execution_count"), outputs)
                    record.finish("done" if ok else "failed", None if ok else f"cell {at} raised")
                elif record and kind == "heartbeat":
                    record.progress(at, outputs, force=True)
                elif kind not in ("done", "heartbeat"):
                    outputs = RUNS.merge(outputs, out)
                    if record:
                        record.progress(at, outputs)
                yield _json.dumps(out) + "\n"
        except Exception as e:  # noqa: BLE001 - the editor shows it under the cell
            why = str(e)
            if isinstance(e, NB.KernelConnectionLost):
                try:
                    rt = NB.runtime(gcp(), runtime)
                    why = RUNS.explain_loss(rt, k.status(kernel) if rt["state"] == "RUNNING" else None)
                except Exception as probe:  # noqa: BLE001 - the connection's own message, and why it stays unexplained
                    why = f"{e}; asking the runtime why failed too: {probe}"
            if record:
                record.finish("failed", why)
            yield _json.dumps({"output_type": "error", "ename": type(e).__name__, "evalue": why,
                               "traceback": []}) + "\n"
            yield _json.dumps({"output_type": "done", "execution_count": None, "status": "error"}) + "\n"
    return StreamingResponse(lines(), media_type="application/x-ndjson")


@app.get(f"{NOTEBOOK_API}/runtimes/{{runtime}}/kernels/{{kernel}}/status")
def kernel_status(runtime: str, kernel: str, who: str = Depends(user), token: str = Depends(notebook_token)):
    """The kernel's execution_state (idle, busy, starting, dead), last_activity and connections;
    execution_state null when it is no longer on the runtime."""
    return _relay(lambda: _kernel(who, runtime, token).status(kernel)) or {"execution_state": None}


class CursorBody(BaseModel):
    code: str
    cursor_pos: int


@app.post(f"{NOTEBOOK_API}/runtimes/{{runtime}}/kernels/{{kernel}}/complete")
def complete(runtime: str, kernel: str, body: CursorBody, who: str = Depends(user),
             token: str = Depends(notebook_token)):
    k = _kernel(who, runtime, token)
    return _relay(lambda: k.complete(kernel, body.code, body.cursor_pos))


@app.post(f"{NOTEBOOK_API}/runtimes/{{runtime}}/kernels/{{kernel}}/inspect")
def inspect_name(runtime: str, kernel: str, body: CursorBody, who: str = Depends(user),
                 token: str = Depends(notebook_token)):
    k = _kernel(who, runtime, token)
    return _relay(lambda: k.inspect(kernel, body.code, body.cursor_pos))


@app.get(f"{NOTEBOOK_API}/runtimes/{{runtime}}/kernels/{{kernel}}/variables")
def variables(runtime: str, kernel: str, who: str = Depends(user), token: str = Depends(notebook_token)):
    k = _kernel(who, runtime, token)
    try:
        return _relay(lambda: k.variables(kernel))
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


@app.post(f"{NOTEBOOK_API}/runtimes/{{runtime}}/kernels/{{kernel}}/{{action}}")
def kernel_action(runtime: str, kernel: str, action: str, who: str = Depends(user),
                  token: str = Depends(notebook_token)):
    k = _kernel(who, runtime, token)
    if action == "interrupt":
        return _relay(lambda: k.interrupt(kernel)) or {"interrupted": kernel}
    if action == "restart":
        return _relay(lambda: k.restart(kernel))
    if action == "shutdown":
        return _relay(lambda: k.shutdown(kernel)) or {"shutdown": kernel}
    raise HTTPException(400, f"a kernel is interrupted, restarted or shut down, not {action!r}")




_static = os.environ.get("MLOPS_CONSOLE_STATIC")
if _static and Path(_static).is_dir():
    app.mount("/assets", _Assets(directory=Path(_static) / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def web(path: str):
        # The web app routes in the browser: every non-API path serves its index.
        # Never cached: it names the release's assets.
        return FileResponse(Path(_static) / "index.html", headers={"Cache-Control": "no-cache"})
