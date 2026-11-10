"""Claude (or any MCP client) on the console's notebooks: an MCP server, run on your machine
as you, that reads and edits workspace notebooks and runs their cells on your own Colab
runtime, the same objects and kernel the console's editor uses.

    claude mcp add mlops-notebooks -- mlops-notebooks-mcp

It signs in with your Application Default Credentials (`gcloud auth application-default
login`): a Colab runtime runs code only for its own user, never a service account. Cells run
on the runtime's first Python kernel, the one the editor connects to, so Claude and the
editor share variables; every edit and every cell's outputs are saved to the notebook, and
an open editor reloads them.
"""
from __future__ import annotations

import functools
import mimetypes
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from urllib.parse import quote

from gcpbricks.console import blog as BL
from gcpbricks.console import notebooks as NB
from gcpbricks.console import runs as RUNS

#: The console a notebook's link points to, from its "Connect Claude" setup.
CONSOLE = os.environ.get("MLOPS_CONSOLE_URL", "").rstrip("/")
#: Characters of a cell's outputs a tool returns; the notebook keeps them all.
OUTPUT_LIMIT = int(os.environ.get("MLOPS_MCP_OUTPUT_LIMIT", "6000"))
#: Seconds run_cells and run_code wait for their run before handing back its id: under the
#: 120 s after which Claude Code moves a call to the background.
WAIT = float(os.environ.get("MLOPS_MCP_WAIT", "90"))
#: The longest run_status waits in one call.
MAX_WAIT = 600
#: Finished runs this session remembers for run_status.
JOBS_KEPT = 20
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _text(v) -> str:
    return "".join(v) if isinstance(v, list) else (v or "")


_merge = RUNS.merge


def render_outputs(outputs: list[dict], limit: int = OUTPUT_LIMIT) -> str:
    parts = []
    for o in outputs:
        kind = o.get("output_type")
        if kind == "stream":
            parts.append(_text(o.get("text")))
        elif kind == "error":
            parts.append(_ANSI.sub("", "\n".join(o.get("traceback") or [f"{o.get('ename')}: {o.get('evalue')}"])))
        elif kind in ("execute_result", "display_data"):
            data = o.get("data") or {}
            if "text/plain" in data:
                parts.append(_text(data["text/plain"]))
            parts += [f"[{t} output, shown in the console]" for t in data if t != "text/plain" and not t.startswith("text/")]
    out = "\n".join(p.rstrip("\n") for p in parts if p)
    return out if len(out) <= limit else f"{out[:limit]}\n... ({len(out) - limit} more characters in the notebook)"


def render(nb: dict, path: str, outputs: bool = True) -> str:
    lines = [f"{path}: {len(nb['cells'])} cells"]
    for i, c in enumerate(nb["cells"]):
        tags = (c.get("metadata") or {}).get("tags") or []
        head = f"--- cell {i} [{c['cell_type']}"
        if c["cell_type"] == "code":
            head += f", run {c.get('execution_count') or '-'}"
        lines.append(head + (f", tags {','.join(tags)}" if tags else "") + "]")
        lines.append(_text(c.get("source")))
        if outputs and c["cell_type"] == "code" and c.get("outputs"):
            lines += ["--- outputs", render_outputs(c["outputs"])]
    return "\n".join(lines)


def link(path: str) -> str:
    """Where the user watches the notebook in the console, when the server knows the console."""
    return f"\nopen in the console: {CONSOLE}/workspace/notebook?path={quote(path)}" if CONSOLE else ""


def _cell(source: str, cell_type: str, tags: list[str] | None) -> dict:
    if cell_type not in ("code", "markdown"):
        raise ValueError(f"a cell is code or markdown, not {cell_type!r}")
    cell = {"cell_type": cell_type, "metadata": {"tags": tags} if tags else {}, "source": source}
    return {**cell, "outputs": [], "execution_count": None} if cell_type == "code" else cell


def _span(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}h{s // 60 % 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


class Job:
    """One run_cells or run_code call: its thread fills it in, any other tool call reads it."""

    def __init__(self, kind: str, path: str | None, run_id: str | None = None):
        self.id, self.kind, self.path, self.run_id = uuid.uuid4().hex[:8], kind, path, run_id
        self.started, self.done = time.time(), threading.Event()
        self.state, self.cell, self.cell_started, self.last_output = "queued", None, None, None
        self.outputs: list[dict] = []
        self.report: list[str] = []
        self.error: str | None = None

    @contextmanager
    def waiting_for(self, lock: threading.Lock, beat: Callable[[], None] | None = None):
        """One run at a time on a kernel, as Jupyter would queue them; while it waits, `beat`
        keeps its run record fresh."""
        while not lock.acquire(timeout=RUNS.HEARTBEAT):
            if beat:
                beat()
        try:
            self.state = "running"
            yield
        finally:
            lock.release()

    def begin(self, cell: int | None) -> None:
        self.cell, self.cell_started, self.outputs = cell, time.time(), []

    def saw(self, outputs: list[dict]) -> None:
        self.outputs, self.last_output = outputs, time.time()

    def fail(self, why: str) -> None:
        self.error = why

    def _where(self) -> str:
        return f"cell {self.cell} of {self.path}" if self.kind == "cells" else "code"

    def line(self) -> str:
        if self.done.is_set():
            how = "failed" if self.error or any(r.startswith("stopped at") for r in self.report) else "done"
            return f"{self.id} {self.kind} {self.path or ''}: {how}, took {_span(time.time() - self.started)}".rstrip()
        return f"{self.id} {self.kind}: {self.state} {self._where()} for {_span(time.time() - self.started)}"

    def wait(self, seconds: float) -> str:
        if self.done.wait(seconds):
            return "\n".join([*self.report, *([self.error] if self.error else [])]) or "done, no output"
        now = time.time()
        if self.state == "queued":
            head = f"run {self.id} waits for the kernel: another run is on it ({_span(now - self.started)})"
        else:
            quiet = f"last output {_span(now - self.last_output)} ago" if self.last_output else "no output yet"
            head = (f"run {self.id} still running {self._where()} for {_span(now - (self.cell_started or now))}, "
                    f"{quiet}")
        tail = render_outputs(self.outputs, 1500)
        return "\n".join([*self.report, head, *([f"--- its outputs so far\n{tail}"] if tail else []),
                          f"run_status({self.id!r}, wait=...) follows it; kernel('interrupt') stops it."
                          + (" The console's editor shows it live." if self.kind == "cells" else "")])


class Notebooks:
    """The tools' logic, as `user`: `gcp` is the console's Gcp (or a fake), `token()` the
    user's current access token, `kernel_for` how a runtime's Jupyter server is reached.
    Safe to call from many threads at once: the server runs every tool call on its own."""

    def __init__(self, gcp, user: str, token: Callable[[], str], kernel_for=NB.kernel_for):
        self.gcp, self.user, self.token, self._kernel_for = gcp, user, token, kernel_for
        self._lock = threading.Lock()
        self._kernel_locks: dict[str, threading.Lock] = {}
        self._jobs: dict[str, Job] = {}

    # --- the workspace

    def list(self, path: str = "") -> dict:
        return NB.list_folder(self.gcp, self.user, "" if path == "/" else path or f"Users/{self.user}")

    def read(self, path: str, outputs: bool = True) -> str:
        got = NB.read_notebook(self.gcp, self.user, path)
        return render(got["notebook"], got["path"], outputs) + link(got["path"])

    def create(self, path: str, cells: list[dict] | None = None) -> str:
        made = NB.create_notebook(self.gcp, self.user, path)
        if cells:
            self._change(made["path"], lambda nb: nb.update(
                cells=[_cell(c.get("source", ""), c.get("cell_type", "code"), c.get("tags")) for c in cells]))
        return made["path"] + link(made["path"])

    def _change(self, path: str, change: Callable[[dict], object]) -> dict:
        """Read, change, save over what was read; a save someone made in between (the
        editor's autosave) is read again and the change made on it."""
        for _ in range(3):
            got = NB.read_notebook(self.gcp, self.user, path)
            change(got["notebook"])
            try:
                NB.save_notebook(self.gcp, self.user, got["path"], got["notebook"], got["generation"])
                return got["notebook"]
            except ValueError as e:
                if "changed since" not in str(e):
                    raise
        raise ValueError(f"{path} keeps changing under this edit: try again")

    @staticmethod
    def _at(nb: dict, index: int) -> dict:
        if not 0 <= index < len(nb["cells"]):
            raise IndexError(f"no cell {index}: the notebook has cells 0 to {len(nb['cells']) - 1}")
        return nb["cells"][index]

    def insert(self, path: str, source: str, index: int | None = None, cell_type: str = "code",
               tags: list[str] | None = None) -> int:
        at = []

        def change(nb):
            i = len(nb["cells"]) if index is None or index < 0 else min(index, len(nb["cells"]))
            nb["cells"].insert(i, _cell(source, cell_type, tags))
            at[:] = [i]
        self._change(path, change)
        return at[0]

    def edit(self, path: str, index: int, source: str | None = None, cell_type: str | None = None,
             tags: list[str] | None = None) -> None:
        def change(nb):
            c = dict(self._at(nb, index))
            if cell_type and cell_type != c["cell_type"]:
                c = {**_cell(_text(c.get("source")), cell_type, None), "metadata": c.get("metadata") or {},
                     **({"id": c["id"]} if c.get("id") else {})}
            if source is not None:
                c["source"] = source
                if c["cell_type"] == "code":
                    c["outputs"], c["execution_count"] = [], None
            if tags is not None:
                meta = {k: v for k, v in (c.get("metadata") or {}).items() if k != "tags"}
                c["metadata"] = {**meta, "tags": tags} if tags else meta
            nb["cells"][index] = c
        self._change(path, change)

    def delete(self, path: str, index: int) -> None:
        self._change(path, lambda nb: (self._at(nb, index), nb["cells"].pop(index)))

    def move(self, path: str, index: int, to: int) -> None:
        def change(nb):
            self._at(nb, index), self._at(nb, to)
            nb["cells"].insert(to, nb["cells"].pop(index))
        self._change(path, change)

    # --- compute

    def runtimes(self) -> list[dict]:
        return [r for r in NB.runtimes(self.gcp) if r["user"] == self.user]

    def templates(self) -> list[dict]:
        return NB.templates(self.gcp)

    def create_runtime(self, template_id: str) -> dict:
        return NB.create_runtime(self.gcp, self.user, template_id)

    def set_runtime(self, runtime_id: str, action: str) -> dict:
        return NB.set_runtime(self.gcp, self.user, runtime_id, action)

    def _runtime(self, runtime_id: str = "") -> str:
        if runtime_id:
            return runtime_id
        running = [r for r in self.runtimes() if r["state"] == "RUNNING"]
        if not running:
            raise ValueError("you have no running Colab runtime: start one (start_runtime) or make one (create_runtime)")
        return running[0]["id"]

    def kernel(self, runtime_id: str = ""):
        """The runtime's first Python kernel, as the editor picks it, started when there's none."""
        runtime_id = self._runtime(runtime_id)
        k = self._kernel_for(self.gcp, self.user, runtime_id, self.token())
        with self._lock:  # two calls at once must not start two kernels
            ks = [x for x in k.kernels() if x.get("name", "").startswith("python")]
            return k, (ks[0] if ks else k.start())["id"]

    def kernel_action(self, action: str, runtime_id: str = "") -> str:
        k, kid = self.kernel(runtime_id)
        if action == "interrupt":
            k.interrupt(kid)
        elif action == "restart":
            k.restart(kid)
        elif action == "status":
            s = k.status(kid) or {}
            return (f"kernel {kid}: {s.get('execution_state', 'gone')}, last activity {s.get('last_activity')}, "
                    f"{s.get('connections')} connection(s)")
        else:
            raise ValueError(f"a kernel is interrupted, restarted or asked its status, not {action!r}")
        return f"kernel {kid}: {action}ed"

    def _execute(self, kernel, kid: str, code: str, on_output=None, on_beat=None) -> tuple[list[dict], dict]:
        outputs, done = [], {}
        for m in kernel.execute(kid, code):
            if m["output_type"] == "done":
                done = m
            elif m["output_type"] == "heartbeat":
                if on_beat:
                    on_beat()
            else:
                outputs = _merge(outputs, m)
                if on_output:
                    on_output(outputs)
        return outputs, done

    # --- runs: each on its own thread, so the server answers every other call meanwhile

    def _kernel_lock(self, kid: str) -> threading.Lock:
        with self._lock:
            return self._kernel_locks.setdefault(kid, threading.Lock())

    def _start(self, job: Job, target: Callable[[], None]) -> None:
        def run():
            try:
                target()
            except Exception as e:  # noqa: BLE001 - the job reports it, the server stays up
                job.fail(f"{type(e).__name__}: {e}")
            finally:
                job.done.set()
        with self._lock:
            self._jobs[job.id] = job
            for old in [j for j in self._jobs.values() if j.done.is_set()][:-JOBS_KEPT]:
                self._jobs.pop(old.id, None)
        threading.Thread(target=run, name=f"mlops-run-{job.id}", daemon=True).start()

    def _lost(self, runtime_id: str, k, kid: str) -> str:
        try:
            rt = NB.runtime(self.gcp, runtime_id)
        except Exception:  # noqa: BLE001 - deleted, or the API is down: say what is known
            rt = None
        try:
            status = k.status(kid) if rt and rt["state"] == "RUNNING" else None
        except Exception:  # noqa: BLE001
            status = {"execution_state": "unknown"}
        return RUNS.explain_loss(rt, status)

    def run_code(self, code: str, runtime_id: str = "", wait: float | None = None) -> str:
        runtime_id = self._runtime(runtime_id)
        k, kid = self.kernel(runtime_id)
        job = Job("code", None)

        def target():
            with job.waiting_for(self._kernel_lock(kid)):
                job.begin(None)
                try:
                    outputs, done = self._execute(k, kid, code, on_output=job.saw)
                except NB.KernelConnectionLost:
                    job.fail(self._lost(runtime_id, k, kid))
                    return
                job.report.append(f"[{done.get('status', '?')}]\n{render_outputs(outputs)}")
        self._start(job, target)
        return job.wait(WAIT if wait is None else wait)

    def run_cells(self, path: str, indices: list[int] | None = None, runtime_id: str = "",
                  wait: float | None = None) -> str:
        """Runs the cells in order (every code cell when none are named) on a thread, saving
        each one's outputs to the notebook as it finishes and keeping a run record the editor
        shows; stops at the first that fails. Returns when the run ends or after `wait` seconds,
        whichever is first: a run still going is followed with run_status."""
        nb = NB.read_notebook(self.gcp, self.user, path)["notebook"]
        if not all(c.get("id") for c in nb["cells"]):  # a run record names its cells by id
            nb = self._change(path, NB.ensure_cell_ids)
        order = indices if indices is not None else [i for i, c in enumerate(nb["cells"]) if c["cell_type"] == "code"]
        for i in order:
            self._at(nb, i)
        cells = [(i, nb["cells"][i]["id"], _text(nb["cells"][i].get("source"))) for i in order
                 if nb["cells"][i]["cell_type"] == "code"]
        if not cells:
            return "no code cells to run"
        runtime_id = self._runtime(runtime_id)
        k, kid = self.kernel(runtime_id)
        record = RUNS.RunRecord(self.gcp, self.user, path, "claude", runtime_id, kid, [(i, cid) for i, cid, _ in cells])
        job = Job("cells", path, record.run_id)

        def target():
            with job.waiting_for(self._kernel_lock(kid), record.beat):
                for i, cid, source in cells:
                    job.begin(i)
                    record.start_cell(i)
                    try:
                        outputs, done = self._execute(
                            k, kid, source, on_output=lambda outs, i=i: (job.saw(outs), record.progress(i, outs)),
                            on_beat=lambda i=i: record.progress(i, job.outputs, force=True))
                    except NB.KernelConnectionLost:
                        why = self._lost(runtime_id, k, kid)
                        record.finish("failed", f"cell {i}: {why}")
                        job.fail(f"--- cell {i} [lost]\n{why}")
                        return
                    status = done.get("status", "?")

                    def put(fresh, i=i, cid=cid, outputs=outputs, done=done):
                        c = next((x for x in fresh["cells"] if x.get("id") == cid), None) or self._at(fresh, i)
                        c["outputs"], c["execution_count"] = outputs, done.get("execution_count")
                    self._change(path, put)
                    record.end_cell(i, "ok" if status == "ok" else "error", done.get("execution_count"), outputs)
                    job.report.append(f"--- cell {i} [run {done.get('execution_count') or '-'}, {status}]\n"
                                      f"{render_outputs(outputs)}")
                    if status != "ok":
                        job.report.append(f"stopped at cell {i}")
                        record.finish("failed", f"cell {i} raised")
                        return
                record.finish("done")
        self._start(job, target)
        return job.wait(WAIT if wait is None else wait)

    def run_status(self, job_id: str = "", wait: float = 0, path: str = "") -> str:
        """A run's progress or result (waiting up to `wait` seconds for it to end); with no
        job, this session's runs; with `path`, every run of the notebook, whoever made it,
        and how it stands now."""
        if path:
            return self._notebook_runs(path)
        if not job_id:
            with self._lock:
                jobs = sorted(self._jobs.values(), key=lambda j: j.started, reverse=True)
            return "\n".join(j.line() for j in jobs) or "no runs in this session"
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"no run {job_id} in this session: run_status() lists them")
        return job.wait(min(max(wait, 0), MAX_WAIT))

    def _notebook_runs(self, path: str) -> str:
        def runtime_for(rid):
            try:
                return NB.runtime(self.gcp, rid)
            except Exception:  # noqa: BLE001 - deleted
                return None

        def kernel_for(rid, kid):
            return self._kernel_for(self.gcp, self.user, rid, self.token()).status(kid)
        runs = RUNS.with_liveness(RUNS.list_runs(self.gcp, path), runtime_for, kernel_for)
        lines = []
        for r in runs:
            cells = ", ".join(f"{c['index']} {c['state']}" for c in r["cells"])
            lines.append(f"{r['run_id']} by {r['by']} ({r['user']}): {r['live']}"
                         + (f": {r['why']}" if r.get("why") else "") + f"\n  cells: {cells}")
        return "\n".join(lines) or f"no runs of {path} in the last day"


def post_link(slug: str) -> str:
    return f"\nopen in the console: {CONSOLE}/blog/{slug}" if CONSOLE else ""


class Blog:
    """The console's blog as `user`: posts are written and edited as their author, by the
    console's own rules (gcpbricks.console.blog)."""

    def __init__(self, gcp, user: str):
        self.gcp, self.user = gcp, user

    def list(self) -> list[dict]:
        return BL.list_posts(self.gcp)

    def read(self, slug: str) -> str:
        p = BL.read_post(self.gcp, self.user, slug)
        head = f"{p['title']} ({slug}) by {p['author']}, tags {p['tags']}, updated {p['updated']}"
        return f"{head}\nsummary: {p['summary']}\n---\n{p['body']}" + post_link(slug)

    def create(self, title: str, body: str, tags: list[str] | None = None, summary: str = "") -> str:
        made = BL.create_post(self.gcp, self.user, title)
        BL.save_post(self.gcp, self.user, made["slug"], title, tags or [], summary, body, made["generation"])
        return made["slug"] + post_link(made["slug"])

    def update(self, slug: str, title: str | None = None, body: str | None = None,
               tags: list[str] | None = None, summary: str | None = None) -> str:
        """Saves over the version just read, so an edit made in between (the console's
        editor) fails the save instead of being overwritten."""
        p = BL.read_post(self.gcp, self.user, slug)
        BL.save_post(self.gcp, self.user, slug, p["title"] if title is None else title,
                     p["tags"] if tags is None else tags, p["summary"] if summary is None else summary,
                     p["body"] if body is None else body, p["generation"])
        return slug + post_link(slug)

    def upload_image(self, slug: str, file_path: str) -> str:
        """A local image added to the post; returns the Markdown that shows it."""
        content_type = mimetypes.guess_type(file_path)[0] or ""
        with open(file_path, "rb") as f:
            url = BL.upload_image(self.gcp, self.user, slug, f.read(), content_type)["url"]
        return f"![]({url})"


def _from_environment() -> Notebooks:
    import google.auth
    import requests
    from google.auth.transport.requests import AuthorizedSession, Request

    from gcpbricks.console.gcp import Gcp

    project = os.environ.get("MLOPS_PROJECT", "my-gcp-project")
    creds, _ = google.auth.default(scopes=["openid", "https://www.googleapis.com/auth/userinfo.email",
                                           "https://www.googleapis.com/auth/cloud-platform"])
    if hasattr(creds, "with_quota_project"):
        creds = creds.with_quota_project(project)
    refreshing = threading.Lock()

    def token() -> str:
        with refreshing:  # tool calls run on threads: one refresh at a time
            if not creds.valid:
                creds.refresh(Request())
            return creds.token

    user = os.environ.get("MLOPS_USER") or requests.get(
        "https://oauth2.googleapis.com/tokeninfo", params={"access_token": token()}, timeout=30).json().get("email", "")
    if not user or user.endswith(".gserviceaccount.com"):
        raise SystemExit("sign in as yourself: gcloud auth application-default login "
                         "(a Colab runtime runs code only for its own user)")
    return Notebooks(Gcp(project, os.environ.get("MLOPS_REGION", "us-central1"), AuthorizedSession(creds)), user, token)


def _threaded(fn):
    """A tool as a coroutine running `fn` on a worker thread. FastMCP calls a plain function on
    its event loop, so one slow call (a cell that runs for an hour) held every other call,
    kernel('interrupt') and list_runtimes included, until it returned."""
    from anyio import to_thread

    @functools.wraps(fn)
    async def call(*args, **kwargs):
        return await to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return call


def server(nbs: Callable[[], Notebooks]):
    """The MCP tools, each on `nbs()`: made on the first call, so the server starts without
    touching Google. Every tool runs on its own thread, so calls are answered side by side."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("mlops-notebooks", instructions=(
        "Workspace notebooks of the gcpbricks console (Colab Enterprise). Paths are under "
        "Users/<email>/ (yours) or Shared/, ending in .ipynb. Cells are numbered from 0, as "
        "read_notebook shows them; read a notebook again before editing by index, since its user may "
        "be editing it in the console too. Code runs on the user's own running Colab runtime, in "
        "the kernel the console's editor shares. The console's blog: posts in GitHub-flavored "
        "Markdown (mermaid, GitHub alerts, KaTeX), by slug, written and edited as their author."))
    blog = lambda: Blog(nbs().gcp, nbs().user)
    tool = lambda fn: mcp.tool()(_threaded(fn))

    @tool
    def list_workspace(path: str = "") -> dict:
        """A workspace folder's sub-folders and notebooks; empty: your own folder. "/" lists
        the root (Users/, Shared/)."""
        return nbs().list(path)

    @tool
    def read_notebook(path: str, outputs: bool = True) -> str:
        """A notebook's cells, numbered from 0, with their sources, tags and (unless
        outputs=False) their saved outputs as text."""
        return nbs().read(path, outputs)

    @tool
    def create_notebook(path: str, cells: list[dict] | None = None) -> str:
        """A new notebook at `path` (Users/<you>/... or Shared/...), optionally with cells:
        [{"source": "...", "cell_type": "code"|"markdown", "tags": [...]}]."""
        return nbs().create(path, cells)

    @tool
    def insert_cell(path: str, source: str, index: int | None = None, cell_type: str = "code",
                    tags: list[str] | None = None) -> str:
        """Inserts a cell before cell `index` (omitted or -1: at the end). Tag a code cell
        "parameters" to make it a notebook task's parameters cell."""
        return f"inserted cell {nbs().insert(path, source, index, cell_type, tags)}"

    @tool
    def edit_cell(path: str, index: int, source: str | None = None, cell_type: str | None = None,
                  tags: list[str] | None = None) -> str:
        """Replaces cell `index`'s source, type or tags (tags=[] removes them); a new source
        clears its outputs."""
        nbs().edit(path, index, source, cell_type, tags)
        return f"edited cell {index}"

    @tool
    def delete_cell(path: str, index: int) -> str:
        """Deletes cell `index`."""
        nbs().delete(path, index)
        return f"deleted cell {index}"

    @tool
    def move_cell(path: str, index: int, to: int) -> str:
        """Moves cell `index` to position `to`."""
        nbs().move(path, index, to)
        return f"moved cell {index} to {to}"

    @tool
    def run_cells(path: str, indices: list[int] | None = None, runtime_id: str = "", wait: float = WAIT) -> str:
        """Runs code cells in the given order (omitted: all of them, top to bottom) on your
        running runtime (or `runtime_id`), saves their outputs to the notebook and returns
        them; stops at the first error. Returns within `wait` seconds: a run still going is
        named, and goes on (run_status follows it; the console's editor shows it live)."""
        return nbs().run_cells(path, indices, runtime_id, wait)

    @tool
    def run_code(code: str, runtime_id: str = "", wait: float = WAIT) -> str:
        """Runs code in the notebook kernel without saving it anywhere: to inspect variables,
        data or the environment. Returns within `wait` seconds, as run_cells."""
        return nbs().run_code(code, runtime_id, wait)

    @tool
    def run_status(run_id: str = "", wait: float = 0, path: str = "") -> str:
        """A run's progress, or its result once it ends (waiting up to `wait` seconds, at most
        600). No run_id: this session's runs. `path`: every run of that notebook, whoever made
        it (the editor, Claude), and how each stands now: running, runner_gone (still on the
        kernel, nobody capturing it), lost, kernel_dead, runtime_stopped."""
        return nbs().run_status(run_id, wait, path)

    @tool
    def kernel(action: str, runtime_id: str = "") -> str:
        """interrupt (stop what's running), restart (lose every variable) or status (idle,
        busy or dead, and when it last did something) the kernel."""
        return nbs().kernel_action(action, runtime_id)

    @tool
    def list_runtimes() -> list[dict]:
        """Your Colab runtimes and their state; cells run on a RUNNING one."""
        return nbs().runtimes()

    @tool
    def list_runtime_templates() -> list[dict]:
        """The runtime templates a runtime is made from: machine, GPU, idle shutdown."""
        return nbs().templates()

    @tool
    def start_runtime(runtime_id: str) -> dict:
        """Starts your stopped runtime: it costs while it runs, until it's stopped or idles out."""
        return nbs().set_runtime(runtime_id, "start")

    @tool
    def stop_runtime(runtime_id: str) -> dict:
        """Stops your runtime: its kernel and variables are lost, its disk kept."""
        return nbs().set_runtime(runtime_id, "stop")

    @tool
    def create_runtime(template_id: str) -> dict:
        """Makes and starts a runtime for you from a template (list_runtime_templates): it
        costs while it runs."""
        return nbs().create_runtime(template_id)

    @tool
    def list_blog_posts() -> list[dict]:
        """Every blog post's slug, title, author, tags and summary, newest change first."""
        return blog().list()

    @tool
    def read_blog_post(slug: str) -> str:
        """A post's title, author, tags, summary and Markdown body."""
        return blog().read(slug)

    @tool
    def create_blog_post(title: str, body: str, tags: list[str] | None = None, summary: str = "") -> str:
        """Publishes a new post by you, readable by everyone in the console; returns its slug
        (from the title). `body` is Markdown without front matter; `summary` is the line
        the blog's list shows."""
        return blog().create(title, body, tags, summary)

    @tool
    def update_blog_post(slug: str, title: str | None = None, body: str | None = None,
                         tags: list[str] | None = None, summary: str | None = None) -> str:
        """Replaces the fields given of your own post; the rest stay. Fails if the post
        changed since this call read it: read it again and redo the edit."""
        return blog().update(slug, title, body, tags, summary)

    @tool
    def upload_blog_image(slug: str, file_path: str) -> str:
        """Uploads a local PNG, JPEG, GIF or WebP (at most 10 MB) to your post and returns the
        Markdown image to put in its body."""
        return blog().upload_image(slug, file_path)

    return mcp


def main() -> None:
    made: list[Notebooks] = []
    making = threading.Lock()

    def nbs() -> Notebooks:
        with making:  # the first calls may come side by side: sign in once
            if not made:
                made.append(_from_environment())
            return made[0]
    server(nbs).run()


if __name__ == "__main__":
    main()
