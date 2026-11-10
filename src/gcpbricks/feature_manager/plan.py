"""Compile a `features` spec into a Plan: plain data, no computation.

Compiling expands every for_each template, parses each expr, infers each value's shape,
and resolves what every entry reads -- an earlier feature, or a column the input frame
must carry. Entries run in declaration order, and a read of a feature declared further
down is an error rather than a silent read of a same-named frame column.
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib
import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .expr import Expr, ExprError, infer_shape, parse
from .ops import MATRIX, OPS, SCALAR, VECTOR

EXPR, FIT, LOOKUP, CUSTOM, AGG = "expr", "fit", "lookup", "custom", "aggregate"
#: Where a node runs: SQL (a transform), the feature store's aggregation, or Python.
SQL, AGGREGATE, PYTHON = "sql", "agg", "python"
TARGET = "@target"
#: Columns `landmarks` adds to each long row: what a transform may read, never a raw column.
LANDMARK_COLUMNS = ("decision_day", "horizon_day")


@dataclass(frozen=True)
class Node:
    name: str
    kind: str
    keep: bool
    #: Names this node reads: earlier features or frame columns ("@target" for a fit on y).
    reads: tuple[str, ...]
    #: What it adds to the frame: [name] for expr/fit, the declared outputs for custom.
    produces: tuple[str, ...]
    shape: str = VECTOR
    #: expr: the expression itself. fit: its `of` (None for "@target").
    expr: Expr | None = None
    #: fit: stat/by/q/smoothing/min_count/default/missing_key/side (code: stat/column);
    #: lookup: column/table/otherwise; custom: fn/batch_dependent; aggregate: the spec.
    #: A SQL or aggregate node's `sql_column`: what the feature store names its value.
    params: dict[str, Any] = field(default_factory=dict)
    stage: str = PYTHON


@dataclass(frozen=True)
class Submodel:
    name: str
    #: The row rule, evaluated on transform()'s output.
    when: Expr
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    nodes: tuple[Node, ...]
    #: Columns the input frame must carry, in first-read order.
    raw_columns: tuple[str, ...]
    #: The raw columns an expr or a fit's `of` reads as numbers (a datetime reads as
    #: days); the rest are only group keys or custom inputs, of any type.
    numeric_columns: tuple[str, ...]
    #: Feature columns transform() adds, in declaration order.
    outputs: tuple[str, ...]
    #: keep: false features, materialized for later steps and dropped at the end.
    intermediates: tuple[str, ...]
    #: Some custom step's output for a row depends on the rest of its batch.
    batch_dependent: bool
    #: Each define `group`, resolved to its kept features in declaration order.
    groups: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Each `inputs` set, resolved to its ordered column names.
    inputs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: What the feature table read must select: raw_columns, the raw columns an input set
    #: or a submodel rule reads, and `fetch`.
    fetch_columns: tuple[str, ...] = ()
    #: The declared submodels, in fit order; each one's columns are inputs[name].
    submodels: dict[str, Submodel] = field(default_factory=dict)
    #: A hash of the spec's SQL part (vars, aggregations, transforms); "" without one.
    sql_hash: str = ""
    #: Raw columns read past the features: passed through by an `inputs` set, read by a
    #: submodel's rule, or `fetch` (what model.py itself reads).
    extra_columns: tuple[str, ...] = ()
    #: {"at": [...], "horizons": [...]}: long training rows; None: one row per entity.
    landmarks: dict | None = None
    #: The `labels` entries, aggregations at each horizon (never features).
    labels: tuple[Node, ...] = ()
    #: The spec's `spine` view and `decision.at_days`, when it states them.
    spine: str | None = None
    at_days: int | None = None

    @property
    def sql_prefix(self) -> str:
        """The feature store's columns for this spec's SQL values: a frame carrying them
        for another spec (another version's) is never read as this one's."""
        return f"__sql_{self.sql_hash}__" if self.sql_hash else ""

    def sql_stage(self) -> SqlStage:
        """What the feature store computes for this plan, and what Python then reads."""
        sql = [n for n in self.nodes if n.stage != PYTHON]
        names = {n.name for n in sql}
        everything = {name for n in self.nodes for name in n.produces}
        python = [n for n in self.nodes if n.stage == PYTHON]
        read_by_python = {r for n in python for r in n.reads if r in names}
        needed = [n.name for n in sql if (n.keep and n.shape == VECTOR) or n.name in read_by_python]
        generated = {TARGET, *(LANDMARK_COLUMNS if self.landmarks else ())}
        python_raw = [r for n in python for r in n.reads if r not in everything and r not in generated]
        return SqlStage(self.sql_prefix, tuple(sql), tuple(needed),
                        tuple(dict.fromkeys([*python_raw, *self.extra_columns])),
                        self.landmarks, self.labels, self.spine, self.at_days)


@dataclass(frozen=True)
class SqlStage:
    """A plan's part the feature store runs: `nodes` (aggregations, then transforms) in
    order; `needed`, those whose values Python reads (each as prefix + name, a matrix as
    prefix + name + "__<i>"); `python_columns`, the raw columns Python reads too."""

    prefix: str
    nodes: tuple[Node, ...]
    needed: tuple[str, ...]
    python_columns: tuple[str, ...]
    #: Plan.landmarks and Plan.labels: long rows, and their targets.
    landmarks: dict | None = None
    labels: tuple[Node, ...] = ()
    #: Plan.spine and Plan.at_days.
    spine: str | None = None
    at_days: int | None = None


def compile_spec(spec: dict, *, resolve_custom: bool = True) -> Plan:
    """resolve_custom=False leaves each custom step's function unimported -- a check of
    the spec alone, where model.py's ML stack may not be installed."""
    # Imported here, not at module level: a pipeline pickled by value would otherwise
    # carry the pydantic model in compile_spec's globals, which does not unpickle.
    from .spec import FeaturesConfig
    cfg = FeaturesConfig(**spec)
    clash = sorted(set(cfg.vars) & set(OPS))
    if clash:
        raise ExprError(f"vars {clash} shadow ops of the same name")
    if cfg.define:
        entries = [e for d in cfg.define for e in _expand(d.model_dump(), cfg.vars)]
    else:
        entries = ([e for a in cfg.aggregations for e in _agg_entries(a, cfg.vars)]
                   + [{**e, "stage": SQL} for t in cfg.transforms
                      for e in _expand(_as_define(t), cfg.vars)]
                   + [e for d in cfg.preprocessing for e in _expand(d.model_dump(), cfg.vars)])
    declared = [n for e in entries for n in (e["outputs"] if e["custom"] else [e["name"]])]
    dupes = sorted({n for n in declared if declared.count(n) > 1})
    if dupes:
        raise ExprError(f"features declared more than once: {dupes}")
    position = {n: i for i, n in enumerate(declared)}

    nodes: list[Node] = []
    shapes: dict[str, str] = {}
    for entry in entries:
        if entry.get("agg") is not None:
            node = _agg_node(entry, cfg.vars)
        else:
            node = _node(entry, cfg.vars, shapes, resolve_custom)
        if entry.get("stage") == SQL:
            from .sql import unsupported
            bad = unsupported(node.expr)
            if bad:
                raise ExprError(f"transforms {node.name!r}: {bad} have no SQL form; move it "
                                f"to preprocessing")
            node = dataclasses.replace(node, stage=SQL)
        first = position[node.produces[0]]
        late = [r for r in node.reads if position.get(r, -1) >= first]
        if late:
            raise ExprError(f"feature {node.name!r} reads {late} before they are declared")
        if node.keep and node.shape == MATRIX:
            raise ExprError(f"feature {node.name!r} is a matrix; reduce it per row (row_*) "
                            f"or mark it keep: false")
        shapes.update({n: node.shape for n in node.produces})
        nodes.append(node)

    generated = {TARGET, *(LANDMARK_COLUMNS if cfg.landmarks else ())}
    raw = [r for n in nodes for r in n.reads if r not in position and r not in generated]
    numeric = [r for n in nodes if n.expr is not None for r in n.expr.names
               if r not in position and r not in generated]
    vectors = [(n, name) for n in nodes if n.shape == VECTOR for name in n.produces]
    intermediates = {name for n, name in vectors if not n.keep}
    kept = tuple(name for n, name in vectors if n.keep)
    group_of = {name: e["group"] for e in entries if e["group"]
                for name in (e["outputs"] if e["custom"] else [e["name"]])}
    groups: dict[str, tuple[str, ...]] = {}
    for name in kept:
        if name in group_of:
            groups[group_of[name]] = (*groups.get(group_of[name], ()), name)
    clash = sorted(set(cfg.submodels) & set(cfg.inputs))
    if clash:
        raise ExprError(f"submodels {clash} share a name with an inputs set")
    sets = {**cfg.inputs, **{name: sub.inputs for name, sub in cfg.submodels.items()}}
    inputs = _resolve_inputs(sets, cfg.vars, kept, groups)
    matrices = {name for name, shape in shapes.items() if shape == MATRIX}
    dropped = sorted({c for cols in inputs.values() for c in cols
                      if c in intermediates or c in matrices})
    if dropped:
        raise ExprError(f"inputs read keep: false features {dropped}, which transform() drops")
    passthrough = [c for cols in inputs.values() for c in cols if c not in position]

    submodels: dict[str, Submodel] = {}
    for name, sub in cfg.submodels.items():
        when = parse(sub.when, cfg.vars)
        if infer_shape(when, lambda n: shapes.get(n, VECTOR)) != VECTOR:
            raise ExprError(f"submodel {name!r}: `when` must give one value per row")
        hidden = sorted(r for r in when.names if r in intermediates)
        if hidden:
            raise ExprError(f"submodel {name!r}: `when` reads keep: false features {hidden}, "
                            f"which transform() drops")
        submodels[name] = Submodel(name, when, dict(sub.params))
    routed_by = [r for s in submodels.values() for r in s.when.names if r not in position]
    landmarks = cfg.landmarks.model_dump() if cfg.landmarks else None
    labels = tuple(dataclasses.replace(n, params={**n.params, "label": True, "sql_column": n.name})
                   for n in (_agg_node(e, cfg.vars) for a in cfg.labels for e in _agg_entries(a, cfg.vars)))
    if landmarks:
        _check_landmarks(landmarks, nodes, labels, raw)
    at_days = cfg.decision.at_days if cfg.decision else None
    check_aggregations([n for n in (*nodes, *labels) if n.kind == AGG], cfg.spine, at_days)
    sql_hash = ""
    if any(n.stage != PYTHON for n in nodes):
        # What changes a SQL value; a field a spec leaves unset is left out, so a spec
        # written before the field existed keeps its hash (and its stored columns).
        part = {"vars": cfg.vars, "aggregations": [_dump_set(a) for a in cfg.aggregations],
                "transforms": [t.model_dump() for t in cfg.transforms]}
        extra = {"landmarks": landmarks, "labels": [_dump_set(a) for a in cfg.labels],
                 "spine": cfg.spine, "at_days": at_days}
        part.update({k: v for k, v in extra.items() if v not in (None, [])})
        sql_hash = hashlib.sha256(json.dumps(part, sort_keys=True).encode()).hexdigest()[:12]
        prefix = f"__sql_{sql_hash}__"
        nodes = [dataclasses.replace(n, params={**n.params, "sql_column": prefix + n.name})
                 if n.stage != PYTHON else n for n in nodes]
    return Plan(
        nodes=tuple(nodes),
        raw_columns=tuple(dict.fromkeys(raw)),
        numeric_columns=tuple(dict.fromkeys([*numeric, *routed_by])),
        outputs=tuple(name for n, name in vectors if n.keep),
        intermediates=tuple(name for n, name in vectors if not n.keep),
        batch_dependent=any(n.params.get("batch_dependent") for n in nodes),
        groups=groups,
        inputs=inputs,
        fetch_columns=tuple(dict.fromkeys([*raw, *passthrough, *routed_by, *cfg.fetch])),
        submodels=submodels,
        sql_hash=sql_hash,
        extra_columns=tuple(dict.fromkeys([*passthrough, *routed_by, *cfg.fetch])),
        landmarks=landmarks,
        labels=labels,
        spine=cfg.spine,
        at_days=at_days,
    )


def _dump_set(a) -> dict:
    d = a.model_dump()
    for k in ("over", "for_each"):
        if d.get(k) is None:
            d.pop(k, None)
    return d


def check_aggregations(nodes, spine: str | None, at_days: int | None) -> None:
    """The two mistakes that compute without failing: an age window past the decision
    day (it reads the row's future), and a source that is the spine itself with `over`
    unstated (each row would read only its own key's rows)."""
    for n in nodes:
        w = n.params["window"]
        if at_days is not None and isinstance(w, dict) and "from_day" in w and w["to_day"] > at_days:
            raise ExprError(f"aggregation {n.name!r}: to_day {w['to_day']} is past the decision "
                            f"day ({at_days}); it would read the row's future")
        if spine is not None and n.params["source"] == spine and not n.params["over_stated"]:
            raise ExprError(f"aggregation {n.name!r}: its source is the spine; state the keys it "
                            f"aggregates across (`over: [age]`), or `over: []` for each row's "
                            f"own key")


def fetch_columns(spec: dict) -> list[str]:
    """The feature-table columns a model with this spec reads -- its feature_columns."""
    return list(compile_spec(spec, resolve_custom=False).fetch_columns)


def _resolve_inputs(sets: dict[str, list], constants: dict, outputs: tuple[str, ...],
                    groups: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """"+outputs" names every kept feature, "+group:<name>" every kept feature of that define
    group; both in declaration order."""
    if "outputs" in sets:
        raise ExprError("inputs: `outputs` is reserved for every kept feature")
    resolved: dict[str, tuple[str, ...]] = {"outputs": outputs}

    def resolve(name: str, including: tuple[str, ...]) -> tuple[str, ...]:
        if name in resolved:
            return resolved[name]
        if name not in sets:
            raise ExprError(f"inputs: no set {name!r} (included by {including[-1]!r})")
        if name in including:
            raise ExprError(f"inputs: set {name!r} includes itself")
        cols: list[str] = []
        for item in sets[name]:
            if isinstance(item, dict):
                axes = dict(item)
                template = axes.pop("each", None)
                if not isinstance(template, str) or not axes:
                    raise ExprError(f"inputs {name!r}: {item!r} needs `each` and an axis")
                expanded = [template.format_map(v) for v in _combos(axes, constants, name)]
                if any("{" in c for c in expanded):
                    raise ExprError(f"inputs {name!r}: {template!r} has a placeholder no axis fills")
                cols += expanded
            elif item.startswith("+group:"):
                group = item[len("+group:"):]
                if group not in groups:
                    raise ExprError(f"inputs {name!r}: no define group {group!r} "
                                    f"(kept-feature groups: {sorted(groups)})")
                cols += groups[group]
            elif item.startswith("+"):
                cols += resolve(item[1:], (*including, name))
            else:
                cols.append(item)
        # An ordered union: a set built from overlapping groups/sets keeps a column once,
        # at its first position.
        resolved[name] = tuple(dict.fromkeys(cols))
        return resolved[name]

    for name in sets:
        resolve(name, ())
    return {name: resolved[name] for name in sets}


def _node(entry: dict, constants: dict, shapes: dict[str, str], resolve_custom: bool) -> Node:
    name = entry["name"]
    if name in constants or name in OPS:
        raise ExprError(f"feature {name!r} shadows a var or op of the same name")
    if entry["expr"] is not None:
        expr = parse(entry["expr"], constants)
        shape = infer_shape(expr, lambda n: shapes.get(n, VECTOR))
        if shape == SCALAR:
            raise ExprError(f"feature {name!r} is a constant; use a var instead")
        return Node(name, EXPR, entry["keep"], expr.names, (name,), shape, expr)
    if entry["lookup"] is not None:
        params = {"column": entry["lookup"], "table": dict(entry["table"]),
                  "otherwise": entry["otherwise"]}
        return Node(name, LOOKUP, entry["keep"], (entry["lookup"],), (name,), params=params)
    if entry["fit"] == "code":
        if not entry["of"].isidentifier():
            raise ExprError(f"feature {name!r}: fit code takes a column name, got {entry['of']!r}")
        return Node(name, FIT, entry["keep"], (entry["of"],), (name,),
                    params={"stat": "code", "column": entry["of"]})
    if entry["fit"] is not None:
        stat, by = entry["fit"], _as_list(entry["by"])
        if entry["of"] == TARGET and stat in ("quantile_bin", "rank", "yeo_johnson"):
            raise ExprError(f"feature {name!r}: {stat} reads `of` at transform too, where "
                            f"there is no {TARGET}")
        expr = None if entry["of"] == TARGET else parse(entry["of"], constants)
        shape = VECTOR if expr is None else infer_shape(expr, lambda n: shapes.get(n, VECTOR))
        if shape == SCALAR or (shape == MATRIX and stat != "quantile"):
            raise ExprError(f"feature {name!r}: fit {stat} needs a per-row `of`, got a {shape}")
        flat = [b for b in by if shapes.get(b, VECTOR) != VECTOR]
        if flat:
            raise ExprError(f"feature {name!r}: group keys {flat} are matrices")
        q = entry["q"]
        params = {"stat": stat, "by": tuple(by), "q": tuple(q) if isinstance(q, list) else q,
                  "smoothing": entry["smoothing"], "min_count": entry["min_count"],
                  "default": entry["default"], "missing_key": entry["missing_key"],
                  "side": entry["side"]}
        reads = (expr.names if expr else ()) + tuple(by)
        return Node(name, FIT, entry["keep"], tuple(dict.fromkeys(reads)), (name,), VECTOR, expr,
                    params)
    params = {"fn": _resolve(entry["custom"], name) if resolve_custom else None,
              "batch_dependent": entry["batch_dependent"]}
    return Node(name, CUSTOM, entry["keep"], tuple(entry["inputs"]), tuple(entry["outputs"]),
                params=params)


def _as_define(t) -> dict:
    """A transform as the define entry it compiles like (an expr)."""
    from .spec import FeatureDefine

    return FeatureDefine(name=t.name, expr=t.formula, for_each=t.for_each, keep=t.keep,
                         group=t.group).model_dump()


def _window_label(w) -> str:
    if isinstance(w, str):
        return w
    if hasattr(w, "from_day"):
        return f"days_{w.from_day}_{w.to_day}"
    if hasattr(w, "offset"):
        return w.window_size + ("" if w.offset == "0" else f"_offset_{w.offset[1:]}")
    return ""


def _agg_entries(a, constants: dict) -> list[dict]:
    """An aggregation as entries of the plan's list: one per for_each combination (its
    templates formatted, an AgeWindow's days as numbers), the name defaulted."""
    combos = _combos(a.for_each, constants, a.name) if a.for_each else [_KeepUnknown()]
    out = []
    for values in combos:
        d = a.model_dump()
        d.pop("for_each")
        d = _format_all(d, values)
        w = d["time_window"]
        if isinstance(w, dict) and "from_day" in w:
            try:
                w["from_day"], w["to_day"] = int(w["from_day"]), int(w["to_day"])
            except ValueError:
                raise ExprError(f"aggregation {d['name']!r}: from_day/to_day {w} are not days") from None
            if not 0 <= w["from_day"] <= w["to_day"]:
                raise ExprError(f"aggregation {d['name']!r}: needs 0 <= from_day <= to_day, got {w}")
        spec = type(a)(**d)
        source = spec.input if isinstance(spec.input, str) else spec.input.name
        name = spec.name or f"{source}_{spec.function}_{_window_label(spec.time_window)}"
        out.append({"name": name, "agg": spec, "keep": spec.keep, "group": spec.group,
                    "custom": None, "outputs": None})
    return out


def _format_all(value, values: dict):
    if isinstance(value, str):
        return value.format_map(values)
    if isinstance(value, dict):
        return {k: _format_all(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_format_all(v, values) for v in value]
    return value


def _check_landmarks(landmarks: dict, nodes, labels, raw) -> None:
    """Long rows see their own decision day's data only: no feature window past the
    earliest decision day, and no raw <stem>_day_<N> column (a wide table's day value,
    the same on every decision day's row)."""
    import re

    first = min(landmarks["at"])
    for n in nodes:
        w = n.params.get("window")
        if n.kind == AGG and isinstance(w, dict) and "from_day" in w and w["to_day"] > first:
            raise ExprError(f"aggregation {n.name!r}: to_day {w['to_day']} is past decision day "
                            f"{first} (landmarks.at); a row would read its future")
    dayed = sorted({r for r in raw if re.search(r"_day_\d+", r)})
    if dayed:
        raise ExprError(f"landmarks: {dayed} are a wide table's values as of a fixed day, "
                        f"past some decision days; read the days with aggregations")
    if labels and not landmarks["horizons"]:
        raise ExprError("labels need landmarks.horizons")


def _agg_node(entry: dict, constants: dict) -> Node:
    from .sql import unsupported

    a = entry["agg"]
    params: dict[str, Any] = {"source": a.source, "function": a.function,
                              "window": a.time_window if isinstance(a.time_window, str)
                              else a.time_window.model_dump(), "over": list(a.over or []),
                              "over_stated": a.over is not None,
                              "since_decision": getattr(a, "since_decision", False)}
    if isinstance(a.input, str):
        params.update(input_column=a.input, input_formula=None)
    else:
        formula = parse(a.input.formula, constants)
        bad = unsupported(formula)
        if bad:
            raise ExprError(f"aggregation {entry['name']!r}: input {bad} have no SQL form")
        params.update(input_column=a.input.name, input_formula=formula)
    series = isinstance(params["window"], dict) and "series_start" in params["window"]
    return Node(entry["name"], AGG, entry["keep"], (), (entry["name"],),
                MATRIX if series else VECTOR, None, params, stage=AGGREGATE)


def _resolve(target: str, name: str) -> Callable:
    module, _, attr = target.partition(":")
    fn = getattr(importlib.import_module(module), attr, None)
    if not callable(fn):
        raise ExprError(f"feature {name!r}: custom {target!r} is not a function")
    return fn


class _KeepUnknown(dict):
    """format_map() leaves placeholders it has no value for, e.g. cols()'s own {d}."""
    def __missing__(self, key):
        return "{" + key + "}"


def _expand(entry: dict, constants: dict) -> list[dict]:
    axes = entry.pop("for_each") or {}
    if not axes:
        return [entry]
    return [_format(entry, values) for values in _combos(axes, constants, entry["name"])]


def _combos(axes: dict, constants: dict, name: str) -> list[dict]:
    """Every combination of the axes' values, first axis outermost; a key "x,y" unpacks
    each value of its axis into x and y."""
    keys = [k.split(",") for k in axes]
    out = []
    for combo in itertools.product(*(_axis(v, constants, name) for v in axes.values())):
        values = _KeepUnknown()
        for names, value in zip(keys, combo):
            parts = list(value) if len(names) > 1 else [value]
            if len(parts) != len(names):
                raise ExprError(f"feature {name!r}: for_each key {','.join(names)!r} "
                                f"unpacks {len(names)} values, got {value!r}")
            values.update(zip(names, parts))
        out.append(values)
    return out


def _as_list(value) -> list:
    return [value] if isinstance(value, str) else list(value or [])


def _format(entry: dict, values: dict) -> dict:
    formatted = dict(entry)
    for key in ("name", "expr", "of", "lookup", "group"):
        if entry[key] is not None:
            formatted[key] = entry[key].format_map(values)
    for key in ("by", "inputs", "outputs"):
        if entry[key]:
            formatted[key] = [s.format_map(values) for s in _as_list(entry[key])]
    return formatted


def _axis(values: Any, constants: dict, name: str) -> list:
    if isinstance(values, str):
        if not isinstance(constants.get(values), list):
            raise ExprError(f"feature {name!r}: for_each refers to {values!r}, not a list var")
        return list(constants[values])
    if isinstance(values, dict):
        if "pairs" in values:
            return list(itertools.combinations(_axis(values["pairs"], constants, name), 2))
        return list(range(*values["range"]))
    return list(values)


#: BigQuery types feature_manager reads as numbers (DATE/DATETIME/TIMESTAMP as days).
NUMERIC_BQ_TYPES = frozenset({"INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC",
                              "BOOLEAN", "BOOL", "DATE", "DATETIME", "TIMESTAMP"})


def schema_problems(plan: Plan, schema: dict[str, str]) -> list[str]:
    """What a live table schema ({column: BigQuery type}) says is wrong with `plan`:
    columns it lacks, and columns read as numbers that hold something else -- each
    named with the feature that reads it."""
    readers: dict[str, str] = {}
    computed_by: dict[str, str] = {}  # only the features that compute with the column
    for node in plan.nodes:
        for read in node.reads:
            readers.setdefault(read, node.name)
        for read in node.expr.names if node.expr is not None else ():
            computed_by.setdefault(read, node.name)
    for sub in plan.submodels.values():
        for read in sub.when.names:
            computed_by.setdefault(read, f"submodel {sub.name}'s when")
    missing = [f"{c} (read by {readers[c]})" for c in plan.raw_columns if c not in schema]
    not_numeric = [f"{c} is {schema[c]} (read by {computed_by[c]})" for c in plan.numeric_columns
                   if c in schema and schema[c] not in NUMERIC_BQ_TYPES]
    return ([f"not in the feature table: {missing}"] if missing else []) + \
           ([f"read as numbers but not numeric: {not_numeric}"] if not_numeric else [])
