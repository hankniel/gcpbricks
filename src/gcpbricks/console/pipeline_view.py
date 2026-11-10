"""A vertex_pipeline_task's pipeline, as the console inspects it: its compiled definition (the
spec deploy last published: every step's component, how its inputs are wired, what it runs on)
and, in a run, each step's actual inputs and outputs, artifacts, logs and compute."""
from __future__ import annotations

from gcpbricks.console import service as S
from gcpbricks.console import stats as ST

#: An artifact the console shows inline: text a step wrote for people to read.
TEXT_ARTIFACTS = {"system.Markdown", "system.HTML"}
MAX_TEXT = 1 << 20


def _gs(uri: str) -> tuple[str, str]:
    bucket, _, name = uri.removeprefix("gs://").partition("/")
    return bucket, name


def _wiring(spec: dict) -> str:
    """Where a step's input comes from: a pipeline parameter, another step's output, a constant."""
    if "componentInputParameter" in spec:
        return f"pipeline parameter {spec['componentInputParameter']}"
    if "componentInputArtifact" in spec:
        return f"pipeline input {spec['componentInputArtifact']}"
    if "taskOutputParameter" in spec:
        o = spec["taskOutputParameter"]
        return f"{o['producerTask']}.{o['outputParameterKey']}"
    if "taskOutputArtifact" in spec:
        o = spec["taskOutputArtifact"]
        return f"{o['producerTask']}.{o['outputArtifactKey']}"
    if "runtimeValue" in spec:
        c = spec["runtimeValue"].get("constant")
        return f"= {c!r}" if c is not None else "runtime value"
    return ""


def _tasks(dag: dict, components: dict, outer: dict | None = None) -> dict[str, tuple[dict, dict]]:
    """Every task of a dag and its sub-dags (a condition's), by key, with what feeds the dag's
    own inputs: a condition's steps read its pipelinechannel-- inputs, which it is wired to."""
    out = {}
    for key, t in (dag.get("tasks") or {}).items():
        out[key] = (t, outer or {})
        sub = components.get(t["componentRef"]["name"], {}).get("dag")
        if sub:
            wired = {**(t.get("inputs") or {}).get("parameters", {}), **(t.get("inputs") or {}).get("artifacts", {})}
            out |= _tasks(sub, components, {n: _resolve(w, outer or {}) for n, w in wired.items()})
    return out


def _resolve(spec: dict, outer: dict) -> dict:
    """A sub-dag input's wiring followed out to what feeds it."""
    name = spec.get("componentInputParameter") or spec.get("componentInputArtifact")
    return outer.get(name, spec) if name else spec


def _source(command: list[str], function: str | None) -> str | None:
    """A lightweight Python component's code: KFP passes it as an argument of its sh -c."""
    return next((c for c in command if function and f"def {function}(" in c), None)


def component(spec: dict, key: str) -> dict:
    """A step of a compiled pipeline: its component's inputs (and what feeds each), outputs,
    what it runs (image, command, resources, or the Google component it calls), and caching."""
    comps = spec.get("components", {})
    t, outer = _tasks((spec.get("root") or {}).get("dag") or {}, comps)[key]
    comp = comps.get(t["componentRef"]["name"], {})
    wired = {n: _resolve(w, outer) for n, w in
             {**(t.get("inputs") or {}).get("parameters", {}), **(t.get("inputs") or {}).get("artifacts", {})}.items()}
    ins = comp.get("inputDefinitions") or {}
    outs = comp.get("outputDefinitions") or {}
    inputs = [{"name": n, "kind": "parameter", "type": d.get("parameterType"), "default": d.get("defaultValue"),
               "optional": d.get("isOptional", False), "from": _wiring(wired.get(n, {}))}
              for n, d in (ins.get("parameters") or {}).items()]
    inputs += [{"name": n, "kind": "artifact", "type": (d.get("artifactType") or {}).get("schemaTitle"),
                "optional": d.get("isOptional", False), "from": _wiring(wired.get(n, {}))}
               for n, d in (ins.get("artifacts") or {}).items()]
    outputs = [{"name": n, "kind": "parameter", "type": d.get("parameterType")} for n, d in (outs.get("parameters") or {}).items()]
    outputs += [{"name": n, "kind": "artifact", "type": (d.get("artifactType") or {}).get("schemaTitle")}
                for n, d in (outs.get("artifacts") or {}).items()]
    ex = ((spec.get("deploymentSpec") or {}).get("executors") or {}).get(comp.get("executorLabel", ""), {})
    runs: dict = {"kind": "dag" if comp.get("dag") else next(iter(ex), None)}
    if "container" in ex:
        c = ex["container"]
        args = c.get("args") or []
        fn = args[args.index("--function_to_execute") + 1] if "--function_to_execute" in args else None
        res = c.get("resources") or {}
        runs |= {"image": c.get("image"), "command": c.get("command") or [], "args": args, "function": fn,
                 "source": _source(c.get("command") or [], fn),
                 "resources": {"cpu": res.get("resourceCpuLimit") or res.get("cpuLimit"),
                               "memory": res.get("resourceMemoryLimit") or res.get("memoryLimit"),
                               "accelerator": (res.get("accelerator") or {}).get("resourceType") or (res.get("accelerator") or {}).get("type"),
                               "accelerators": (res.get("accelerator") or {}).get("resourceCount") or (res.get("accelerator") or {}).get("count")}}
    elif "importer" in ex:
        runs |= {"artifact_uri": _wiring(ex["importer"].get("artifactUri") or {}) or ex["importer"].get("artifactUri"),
                 "type": (ex["importer"].get("typeSchema") or {}).get("schemaTitle")}
    return {"key": key, "label": (t.get("taskInfo") or {}).get("name") or key, "component": t["componentRef"]["name"],
            "description": comp.get("description") or "", "inputs": inputs, "outputs": outputs, "runs": runs,
            "cache": bool((t.get("cachingOptions") or {}).get("enableCache")),
            "condition": S._condition_text((t.get("triggerPolicy") or {}).get("condition", "")),
            "depends_on": t.get("dependentTasks") or []}


def _published(gcp, argument: dict) -> tuple[dict, dict]:
    """The pipeline deploy last published for a task: current.json and its spec."""
    current = gcp.read_json(argument["bucket"], f'{argument["spec_prefix"]}/current.json')
    entry = current.get(argument["pipeline"])
    if not entry:
        raise KeyError(f'{argument["spec_prefix"]} has no pipeline {argument["pipeline"]} published')
    return current, gcp.read_json(*_gs(entry["spec"]))


def definition(gcp, workflow: str, task: str) -> dict:
    """A vertex_pipeline_task's pipeline as deploy last published it: its steps' graph, each
    step's component, and the pipeline's parameters with the values the task passes."""
    job = S._find(gcp, workflow)
    spec_t = gcp.read_json(job["bucket"], job["definition"])["tasks"].get(task)
    if spec_t is None:
        raise KeyError(f"{job['job']} has no task {task}")
    if S.task_kind(spec_t) != "vertex_pipeline_task":
        raise ValueError(f"{task} is a {S.task_kind(spec_t)}, not a vertex_pipeline_task")
    current, spec = _published(gcp, spec_t["argument"])
    entry = current[spec_t["argument"]["pipeline"]]
    params = gcp.read_json(*_gs(entry["params"])) if entry.get("params") else {}
    values = params.get("parameter_values") or {}
    graph = S.pipeline_graph({"pipelineSpec": spec})
    for n in graph["nodes"]:
        n["state"] = None
    keys = [n["key"] for n in graph["nodes"]]
    return {"version": current.get("version"), "image": current.get("image"), "spec_uri": entry["spec"],
            "name": (spec.get("pipelineInfo") or {}).get("name"), "sdk": spec.get("sdkVersion"),
            "parameters": [{"name": n, "type": d.get("parameterType"), "default": d.get("defaultValue"),
                            "value": values.get(n)}
                           for n, d in (((spec.get("root") or {}).get("inputDefinitions") or {}).get("parameters") or {}).items()],
            **graph, "steps": {k: component(spec, k) for k in keys}}


def _job(gcp, workflow: str, execution_id: str, task: str) -> dict:
    run = S.pipeline_run(gcp, workflow, execution_id, task)["run"]
    if not run:
        raise KeyError(f"{task} started no pipeline run")
    return gcp.ai_get(run["name"])


def _artifacts(side: dict | None) -> list[dict]:
    return [{"name": name, "type": a.get("schemaTitle"), "uri": a.get("uri"), "metadata": a.get("metadata") or {},
             "resource": a.get("name"), "text": a.get("schemaTitle") in TEXT_ARTIFACTS}
            for name, v in (side or {}).items() for a in v.get("artifacts") or []]


def _detail(job: dict, key: str, unstarted: bool = False) -> dict:
    """A step's task detail. Vertex lists a step only once it starts: with `unstarted`, a step of
    the run's spec not listed yet is one with no detail, in the state the run's graph gives it."""
    d = next((t for t in (job.get("jobDetail") or {}).get("taskDetails", []) if t.get("taskName") == key), None)
    if d is None and unstarted:
        d = next(({"state": n["state"]} for n in S.pipeline_graph(job)["nodes"] if n["key"] == key), None)
    if d is None:
        raise KeyError(f"the pipeline run has no step {key}")
    return d


def step(gcp, workflow: str, execution_id: str, task: str, key: str) -> dict:
    """One step of a run: its component, its actual input and output values and artifacts,
    whether its result came from cache, and its custom job's compute and logs."""
    job = _job(gcp, workflow, execution_id, task)
    d = _detail(job, key, unstarted=True)
    meta = (d.get("execution") or {}).get("metadata") or {}
    ex = d.get("executorDetail") or {}
    main = (ex.get("containerDetail") or {}).get("mainJob") or (ex.get("customJobDetail") or {}).get("job")
    compute = ST.custom_job(gcp, main, key) if main else None
    logs = []
    if main and d.get("startTime"):
        a, b = ST._window(d["startTime"], d.get("endTime"))
        logs = [{"time": e.get("timestamp"), "severity": e.get("severity", "DEFAULT"),
                 "source": e["resource"]["labels"].get("task_name"),
                 "text": e.get("textPayload") or str((e.get("jsonPayload") or {}).get("message", ""))}
                for e in gcp.log_entries(f'resource.type="ml_job" AND resource.labels.job_id="{main.rsplit("/", 1)[-1]}" '
                                         f'AND timestamp>="{a}" AND timestamp<="{b}"', n=1000)]
    return {"key": key, "state": d.get("state"), "start": d.get("startTime"), "end": d.get("endTime"),
            "cached": (d.get("execution") or {}).get("state") == "CACHED",
            "error": (d.get("error") or {}).get("message", ""),
            "component": component(job.get("pipelineSpec") or {}, key) if (job.get("pipelineSpec") or {}).get("root") else None,
            "inputs": {k.removeprefix("input:"): v for k, v in meta.items() if k.startswith("input:")},
            "outputs": {k.removeprefix("output:"): v for k, v in meta.items() if k.startswith("output:")},
            "input_artifacts": _artifacts(d.get("inputs")), "output_artifacts": _artifacts(d.get("outputs")),
            "custom_job": main, "compute": compute, "logs": logs}


def artifact_text(gcp, workflow: str, execution_id: str, task: str, key: str, name: str) -> dict:
    """A step's Markdown or HTML output artifact, by name: only what that step wrote."""
    d = _detail(_job(gcp, workflow, execution_id, task), key)
    a = next((a for a in _artifacts(d.get("outputs")) if a["name"] == name and a["text"]), None)
    if a is None or not (a["uri"] or "").startswith("gs://"):
        raise KeyError(f"{key} wrote no text artifact {name}")
    got = gcp.read_object(*_gs(a["uri"]))
    if got is None:
        raise KeyError(f"{a['uri']} is gone")
    body = got[0][:MAX_TEXT].decode("utf-8", "replace")
    return {"name": name, "type": a["type"], "uri": a["uri"], "text": body, "truncated": len(got[0]) > MAX_TEXT}
