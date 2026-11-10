"""The `features` spec schema: a features.yaml, validated before plan.py compiles it.

Two shapes. The blocks, in the order they run:
  aggregations   windowed, over a long table's rows        stateless, in SQL
  transforms     row-wise, over the feature view's columns  stateless, in SQL
  preprocessing  fit on the training rows, frozen; ops only  stateful, in Python
                 numpy has; udf
and the older single `define` list (everything in Python), still read for specs that
haven't moved.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: A for_each axis: a literal list, the name of a list var, {pairs: <list|var>} for every
#: (x, y) with x before y, or {range: [start, stop]}.
ForEachValues = list[Any] | str | dict[Literal["pairs", "range"], Any]


#: An aggregation's window, Tecton's: "3d", "lifetime", {window_size, offset} or a
#: series {series_start, window_size, step_size}. It ends at the decision point (the
#: model's feature_maturity_days), that day included.
_DAYS = r"^[1-9][0-9]*d$"
_OFFSET = r"^(0|-[1-9][0-9]*d)$"


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_size: str = Field(pattern=_DAYS)
    #: Moves the window's end back: -3d ends it three days before the decision point.
    offset: str = Field(default="0", pattern=_OFFSET)


class TimeWindowSeries(BaseModel):
    """step_size-spaced windows of window_size, the last ending at the decision point and
    the first at series_start past it: a matrix, earliest window first."""
    model_config = ConfigDict(extra="forbid")

    series_start: str = Field(pattern=r"^-[1-9][0-9]*d$")
    window_size: str = Field(pattern=_DAYS)
    step_size: str | None = Field(default=None, pattern=_DAYS)


class AgeWindow(BaseModel):
    """Days from_day..to_day (both included) past the entity's anchor (the spine's
    timestamp, e.g. its install day), whatever the decision point: a wide table's
    <stem>_day_<N> is {from_day: N, to_day: N}, its _cum {from_day: 0, to_day: N}. A
    template ("{d}") is formatted by the entry's for_each."""
    model_config = ConfigDict(extra="forbid")

    from_day: int | str
    to_day: int | str


class AggregationInput(BaseModel):
    """A formula over the source's own columns, computed on each of its rows first."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    formula: str = Field(min_length=1)


class AggregationSpec(BaseModel):
    """One `aggregations` entry, Tecton's Aggregate: `function` of `input` over each
    entity's rows of `source` in `time_window`."""
    model_config = ConfigDict(extra="forbid")

    #: Default: "{input}_{function}_{window}", e.g. total_rev_sum_3d.
    name: str | None = None
    #: A feature view whose keys the spine carries and whose timestamp orders its rows.
    source: str = Field(min_length=1)
    input: str | AggregationInput
    function: Literal["sum", "count", "mean", "min", "max", "stddev_samp", "stddev_pop",
                      "var_samp", "var_pop", "count_distinct", "first", "last"]
    time_window: Literal["lifetime"] | str | TimeWindow | TimeWindowSeries | AgeWindow
    #: Source keys aggregated across, not joined on: a long table's own `age`, or a finer
    #: grain rolled up. Any other key the spine lacks is an error, never a silent rollup.
    #: With the source as the spine it must be stated, `[]` included: each row would
    #: otherwise read only its own key's rows.
    over: list[str] | None = None
    #: Template axes, as a define entry's: name/source/input and an AgeWindow's days are
    #: formatted with each combination.
    for_each: dict[str, ForEachValues] | None = None
    keep: bool = True
    group: str | None = None

    @model_validator(mode="after")
    def _window(self) -> AggregationSpec:
        import re

        w = self.time_window
        if isinstance(w, str) and w != "lifetime" and not re.match(_DAYS, w):
            raise ValueError(f"aggregation {self.name or self.input!r}: time_window {w!r} is "
                             f"'<n>d', 'lifetime', {{window_size, offset}} or a series")
        if isinstance(w, TimeWindowSeries) and not self.name:
            raise ValueError("an aggregation over a series needs a `name`")
        if self.for_each and not self.name:
            raise ValueError("an aggregation with for_each needs a `name` template")
        return self


class LabelSpec(AggregationSpec):
    """One `labels` entry: an aggregation evaluated at each horizon H instead of the
    decision point (`lifetime`: everything up to day H). since_decision: from the day after
    the decision point on, what accrues past it."""

    since_decision: bool = False


class Landmarks(BaseModel):
    """Long training rows: each entity at each decision day of `at` (its features as of
    that day) and, with labels, each horizon of `horizons` (its labels as of that day)."""
    model_config = ConfigDict(extra="forbid")

    #: Decision days: a list, or {range: [start, stop(, step)]} as Python's range.
    at: list[int] = Field(min_length=1)
    horizons: list[int] = Field(default_factory=list)

    @field_validator("at", mode="before")
    @classmethod
    def _range(cls, v):
        if isinstance(v, dict):
            if set(v) != {"range"} or not 2 <= len(v["range"]) <= 3:
                raise ValueError("landmarks.at: a list of days or {range: [start, stop(, step)]}")
            return list(range(*v["range"]))
        return v

    @model_validator(mode="after")
    def _days(self) -> Landmarks:
        if any(a < 0 for a in self.at) or len(set(self.at)) != len(self.at):
            raise ValueError("landmarks.at: distinct days >= 0")
        if any(h <= max(self.at) for h in self.horizons):
            raise ValueError("landmarks.horizons: every horizon past every decision day")
        return self


class Decision(BaseModel):
    """The decision point of one row per spine row: `at_days` days past its timestamp,
    that day included (0: the row's own day)."""
    model_config = ConfigDict(extra="forbid")

    at_days: int = Field(ge=0)


class TransformSpec(BaseModel):
    """One `transforms` entry: a row-wise formula, every op of it one SQL states."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    formula: str = Field(min_length=1)
    for_each: dict[str, ForEachValues] | None = None
    keep: bool = True
    group: str | None = None


class FeatureDefine(BaseModel):
    """One `features.define` entry -- exactly one of:
      expr    a row-wise expression over frame columns and earlier features
      fit     a statistic computed across rows at fit time and frozen, looked up after:
              median/mean/iqr (per `by` group, or overall), quantile (one value over
              every cell of `of`), quantile_bin (bin edges), rank (percentile among the
              fit values, per group), code (category index in the fit vocabulary),
              yeo_johnson (a fitted power transform)
      lookup  a column mapped through a declared table, e.g. a string id -> 0/1
      custom  "module:function" for what no expression states; must declare its outputs
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    expr: str | None = None
    fit: Literal["median", "mean", "iqr", "quantile", "quantile_bin", "rank", "code",
                 "yeo_johnson"] | None = None
    #: fit: what it summarises -- an expression like `expr` (a bare name included), or
    #: "@target". Only quantile accepts a matrix; code takes a column of any type.
    of: str | None = None
    #: median/mean/iqr/rank: group key column(s); None = one overall value.
    by: str | list[str] | None = None
    #: quantile: one quantile. quantile_bin: quantiles whose values become the bin edges.
    q: float | list[float] | None = None
    #: mean per group: shrink toward the overall mean as (n*mean + s*overall) / (n + s).
    smoothing: float | None = Field(default=None, gt=0)
    #: Groups with fewer fit rows are left out of the table, so they get `default`.
    min_count: int | None = Field(default=None, gt=0)
    #: A group the table lacks: the overall statistic, NaN, or the mean of the table.
    default: Literal["overall", "nan", "table_mean"] = "overall"
    #: A row whose group key is missing: gets `default`, or is a group of its own ("nan").
    missing_key: Literal["default", "group"] = "default"
    #: rank: a value tied with fit values ranks below them (left) or above (right).
    side: Literal["left", "right"] = "right"
    #: lookup: the column whose values (as text) are looked up in `table`.
    lookup: str | None = None
    table: dict[str, float] | None = None
    #: lookup: the value for one the table lacks; None = NaN (kept out of the manifest
    #: JSON, which has no NaN).
    otherwise: float | None = None
    custom: str | None = None
    #: custom: the columns it reads (feeds the plan's required columns) and adds.
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] | None = None
    #: custom: its output for a row depends on the other rows in the batch.
    batch_dependent: bool = False
    #: Template axes: name/expr/of/by/inputs/outputs are formatted with each combination.
    #: A key "x,y" unpacks each value (e.g. from {pairs: days}) into two names.
    for_each: dict[str, ForEachValues] | None = None
    #: False = an intermediate other features read, dropped from the output.
    keep: bool = True
    #: A family name `inputs` can take whole with "+group:<name>"; formatted like `name`,
    #: so a for_each entry can put each combination in its own group.
    group: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _names(cls, data):
        """`formula` and `udf`, preprocessing's words for expr and custom."""
        if isinstance(data, dict) and ("formula" in data or "udf" in data):
            data = dict(data)
            for new, old in (("formula", "expr"), ("udf", "custom")):
                if new in data:
                    if data.get(old) is not None:
                        raise ValueError(f"feature {data.get('name')!r}: {new} and {old} both given")
                    data[old] = data.pop(new)
        return data

    @model_validator(mode="after")
    def _one_kind_with_its_params(self) -> FeatureDefine:
        kinds = [k for k in ("expr", "fit", "lookup", "custom") if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError(f"feature {self.name!r}: declare exactly one of "
                             f"expr/fit/lookup/custom, got {kinds or 'none'}")
        kind = self.fit or kinds[0]
        grouped = {"of", "by", "min_count", "default", "missing_key"}
        allowed = {
            "expr": set(),
            "median": grouped,
            "iqr": grouped,
            "mean": grouped | {"smoothing"},
            "quantile": {"of", "q"},
            "quantile_bin": {"of", "q"},
            "rank": {"of", "by", "side", "missing_key"},
            "code": {"of"},
            "yeo_johnson": {"of"},
            "lookup": {"table", "otherwise"},
            "custom": {"inputs", "outputs", "batch_dependent"},
        }[kind]
        params = {"of", "by", "q", "min_count", "default", "smoothing", "missing_key", "side",
                  "table", "otherwise", "inputs", "outputs", "batch_dependent"}
        # By value, not model_fields_set: a manifest's model_dump() spells out every
        # default, and that dump is validated again when the pipeline compiles it.
        declared = {f for f in params
                    if getattr(self, f) != type(self).model_fields[f].get_default(call_default_factory=True)}
        stray = sorted(declared - allowed)
        if stray:
            raise ValueError(f"feature {self.name!r}: {stray} do not apply to a {kind} entry")
        if self.fit and not self.of:
            raise ValueError(f"feature {self.name!r}: fit needs `of`")
        if self.fit == "quantile" and not isinstance(self.q, (int, float)):
            raise ValueError(f"feature {self.name!r}: quantile needs one `q`")
        if self.fit == "quantile_bin" and not isinstance(self.q, list):
            raise ValueError(f"feature {self.name!r}: quantile_bin needs a list `q`")
        if self.by is None and ({"min_count", "smoothing", "default", "missing_key"} & declared):
            raise ValueError(f"feature {self.name!r}: min_count/smoothing/default/missing_key "
                             f"need `by`")
        if self.lookup is not None and not self.table:
            raise ValueError(f"feature {self.name!r}: lookup needs a `table`")
        if self.custom and (":" not in self.custom or not self.outputs):
            raise ValueError(f"feature {self.name!r}: custom is 'module:function' and needs `outputs`")
        return self


class SubmodelConfig(BaseModel):
    """One `features.submodels` entry: the rows one estimator owns, the columns it
    reads and its parameters. Routing is declared here so train and predict split rows
    by the same rule."""
    model_config = ConfigDict(extra="forbid")

    #: Row rule: a boolean expression over frame columns and kept features. A row no
    #: rule matches is unrouted; a row two rules match is an error.
    when: str = Field(min_length=1)
    #: Its columns: items as in an `inputs` set (names, "+<set>", "+group:<name>", each).
    inputs: list[str | dict[str, Any]] = Field(min_length=1)
    #: Estimator parameters, handed to model.py as they are. A tuned key's value here is
    #: the default the sweep starts from and an untuned run trains with.
    params: dict[str, Any] = Field(default_factory=dict)


class FeaturesConfig(BaseModel):
    """A model's features.yaml: its feature engineering as data, compiled by plan.py."""
    model_config = ConfigDict(extra="forbid")

    #: Constants an expr reads by name (scalars) or cols()/for_each expand (lists).
    vars: dict[str, float | int | list[float | int | str]] = Field(default_factory=dict)
    aggregations: list[AggregationSpec] = Field(default_factory=list)
    transforms: list[TransformSpec] = Field(default_factory=list)
    preprocessing: list[FeatureDefine] = Field(default_factory=list)
    #: The feature view whose rows are the training rows (and whose columns transforms
    #: read): one row each, or each times landmarks.
    spine: str | None = None
    #: Where each spine row's windows end; or `landmarks`, several decision days.
    decision: Decision | None = None
    #: Long training rows (entity x decision day x horizon) instead of one per entity.
    landmarks: Landmarks | None = None
    #: Targets as aggregations at each horizon (landmarks.horizons).
    labels: list[LabelSpec] = Field(default_factory=list)
    #: The older shape: every entry in Python. Not with the blocks above.
    define: list[FeatureDefine] = Field(default_factory=list)
    #: Named, ordered column lists an estimator reads. An item is a name (a feature, or a
    #: raw column passed through), "+<set>" (another set's names; "+outputs" is every
    #: kept feature in declaration order), "+group:<name>" (every kept feature in that
    #: define group, in declaration order), or {each: "<template>", <axis>: <values>, ...}
    #: expanded like a for_each. A set is an ordered union: a repeated column keeps its
    #: first position.
    inputs: dict[str, list[str | dict[str, Any]]] = Field(default_factory=dict)
    #: Raw columns model.py itself reads (row filters, grouping, target ingredients).
    fetch: list[str] = Field(default_factory=list)
    #: Estimators that each own a slice of the rows, in fit order; each is also an
    #: `inputs` set under its own name.
    submodels: dict[str, SubmodelConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_shape(self) -> FeaturesConfig:
        blocks = self.aggregations or self.transforms or self.preprocessing
        if self.define and blocks:
            raise ValueError("features: `define` or aggregations/transforms/preprocessing, not both")
        if not (self.define or blocks):
            raise ValueError("features: declare transforms, aggregations or preprocessing")
        if self.labels and not (self.landmarks and self.landmarks.horizons):
            raise ValueError("features: labels need landmarks.horizons")
        if self.decision and self.landmarks:
            raise ValueError("features: `decision` or `landmarks`, not both")
        if self.spine and (self.aggregations or self.labels) and not (self.decision or self.landmarks):
            raise ValueError("features: aggregations need `decision: {at_days: N}` or `landmarks`")
        return self
