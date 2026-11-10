"""A bundle's dashboards: what the console shows for a bundle's operations and its data,
declared in the bundle (resources.dashboards) and rendered by the console, not Cloud
Monitoring.

    widgets:
      - {title: Run time by job, type: line, source: runs, measure: avg(duration_min), by: job, bin: 1d}
      - {title: Failed runs, type: counter, source: runs, measure: count, filter: {state: FAILED}, window: 7d}
      - {title: MAPE by model, type: line, dataset: accuracy, measure: avg(mape), by: model, bin: 1w}
      - {title: Warnings, type: logs, query: 'severity>=WARNING'}

A data widget reads one source or one dataset and aggregates its rows: rows in the window
(the widget's, else the page's) and matching `filter`, grouped by the time `bin` and `by`,
reduced to `measure`. Sources are the console's own reads, scoped to the dashboard's bundle
and target, with fixed fields (SOURCES). Datasets are BigQuery SQL the bundle declares
(`@start` / `@end` are the window), for data outside the platform: keep their logic in dbt
and the dataset a plain SELECT.
"""
from __future__ import annotations

import re
import statistics
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The built-in sources: their time field and every field a widget may name.
SOURCES: dict[str, dict] = {
    "runs": {"time": "start", "fields": {"job", "state", "start", "end", "duration_min"}},
    "tasks": {"time": "start", "fields": {"job", "task", "status", "start", "duration_min"}},
    # One row per Cloud Run execution of the bundle's job clusters: its peak memory.
    "memory": {"time": "start", "fields": {"run_job", "state", "start", "peak_mb", "limit_mb", "peak_fraction"}},
    # One row per metric of each run of the bundle's experiments.
    "model_metrics": {"time": "start", "fields": {"experiment", "run", "state", "start", "metric", "value"}},
}
DATA_TYPES = ("counter", "table", "line", "bar")
WINDOW = re.compile(r"^(\d+)([hdw])$")
MEASURE = re.compile(r"^(count|(avg|sum|min|max|p95|last)\((\w+)\))$")
_UNIT = {"h": timedelta(hours=1), "d": timedelta(days=1), "w": timedelta(weeks=1)}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def window_delta(window: str) -> timedelta:
    m = WINDOW.fullmatch(window)
    if not m:
        raise ValueError(f"window {window!r}: a number and h, d or w, e.g. 24h, 7d, 4w")
    n, unit = m.groups()
    return int(n) * _UNIT[unit]


def _window(v: str | None) -> str | None:
    if v is not None and not WINDOW.fullmatch(v):
        raise ValueError(f"window {v!r}: a number and h, d or w, e.g. 24h, 7d, 4w")
    return v


class Dataset(_Strict):
    #: BigQuery SQL; `@start` and `@end` (TIMESTAMP) are the window when it names them.
    bigquery: str
    #: The column the window and a widget's `bin` read; none: the window does not apply.
    time: str | None = None


class Widget(_Strict):
    title: str
    type: Literal["counter", "table", "line", "bar", "logs", "alerts", "text"]
    source: Literal["runs", "tasks", "memory", "model_metrics"] | None = None
    dataset: str | None = None
    #: count, or avg / sum / min / max / p95 / last of a field.
    measure: str = "count"
    by: list[str] = []
    bin: Literal["1h", "1d", "1w"] | None = None
    #: field: value, or field: [values]; every one must hold.
    filter: dict[str, str | list[str]] = {}
    window: str | None = None
    sort: Literal["asc", "desc"] | None = None
    limit: int | None = Field(default=None, gt=0)
    #: A logs widget's Logging filter, scoped to the window.
    query: str | None = None
    #: A text widget's text.
    text: str | None = None
    #: Columns of the page's 12.
    width: int = Field(default=6, ge=1, le=12)

    @field_validator("by", mode="before")
    @classmethod
    def _one_or_many(cls, v):
        return [v] if isinstance(v, str) else v

    _w = field_validator("window")(_window)

    @model_validator(mode="after")
    def _fits_its_type(self):
        data = self.type in DATA_TYPES
        if data and (self.source is None) == (self.dataset is None):
            raise ValueError(f"{self.title!r}: a {self.type} widget reads one source or one dataset")
        if not data and (self.source or self.dataset):
            raise ValueError(f"{self.title!r}: a {self.type} widget reads no source or dataset")
        if not MEASURE.fullmatch(self.measure):
            raise ValueError(f"{self.title!r}: measure {self.measure!r} is count, or avg / sum / "
                             f"min / max / p95 / last of a field, e.g. avg(duration_min)")
        if self.type in ("line", "bar") and not (self.bin or self.by):
            raise ValueError(f"{self.title!r}: a {self.type} chart needs a bin, a by, or both")
        if self.type == "line" and not self.bin:
            raise ValueError(f"{self.title!r}: a line chart is over time: give it a bin")
        if self.type == "logs" and not self.query:
            raise ValueError(f"{self.title!r}: a logs widget needs a query (a Logging filter)")
        if self.type == "text" and not self.text:
            raise ValueError(f"{self.title!r}: a text widget needs its text")
        return self

    @property
    def field(self) -> str | None:
        """The field the measure reads (None for count)."""
        return MEASURE.fullmatch(self.measure).group(3)

    def fields(self) -> set[str]:
        return {*self.by, *self.filter, *([self.field] if self.field else [])}


class Filter(_Strict):
    """A page-wide filter: a dropdown of `field`'s values in one source or dataset. A pick
    narrows every data widget whose rows carry the field, and is a dataset's `@<name>` STRING
    parameter ('' when nothing is picked) where its SQL names it."""

    source: Literal["runs", "tasks", "memory", "model_metrics"] | None = None
    dataset: str | None = None
    field: str
    label: str | None = None

    @model_validator(mode="after")
    def _one_place(self):
        if (self.source is None) == (self.dataset is None):
            raise ValueError("a filter reads its values from one source or one dataset")
        if self.source and self.field not in SOURCES[self.source]["fields"]:
            raise ValueError(f"source {self.source} has no field {self.field!r}")
        return self


class DashboardSpec(_Strict):
    """A dashboard file (resources.dashboards.<key>.file_path), YAML or JSON."""

    #: The page's window, until a reader picks another.
    window: str = "30d"
    datasets: dict[str, Dataset] = {}
    filters: dict[str, Filter] = {}
    widgets: list[Widget] = Field(min_length=1)

    _w = field_validator("window")(_window)

    @model_validator(mode="after")
    def _names_hold(self):
        for name, f in self.filters.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError(f"filter {name!r}: lowercase letters, digits and _, as its @{name} parameter")
            if f.dataset is not None and f.dataset not in self.datasets:
                raise ValueError(f"filter {name!r}: no dataset {f.dataset!r}; datasets: {sorted(self.datasets)}")
        for w in self.widgets:
            if w.dataset is not None:
                if w.dataset not in self.datasets:
                    raise ValueError(f"{w.title!r}: no dataset {w.dataset!r}; datasets: {sorted(self.datasets)}")
                if w.bin and not self.datasets[w.dataset].time:
                    raise ValueError(f"{w.title!r}: dataset {w.dataset!r} has no time column to bin")
            if w.source is not None:
                unknown = sorted(w.fields() - SOURCES[w.source]["fields"])
                if unknown:
                    raise ValueError(f"{w.title!r}: source {w.source} has no field {unknown}; it has "
                                     f"{sorted(SOURCES[w.source]['fields'])}")
        return self


# --- aggregation ----------------------------------------------------------------------

def _ts(v) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    t = datetime.fromisoformat(str(v))
    return t if t.tzinfo else t.replace(tzinfo=UTC)



def _bin(t: datetime, size: str) -> str:
    if size == "1h":
        return t.strftime("%Y-%m-%dT%H:00")
    if size == "1d":
        return t.strftime("%Y-%m-%d")
    return (t - timedelta(days=t.weekday())).strftime("%Y-%m-%d")


def _reduce(kind: str, values: list):
    if kind == "count":
        return len(values)
    nums = [float(v) for v in values if v is not None and v != ""]
    if kind == "last":
        return values[-1] if values else None
    if not nums:
        return None
    if kind == "avg":
        return sum(nums) / len(nums)
    if kind == "sum":
        return sum(nums)
    if kind == "min":
        return min(nums)
    if kind == "max":
        return max(nums)
    return statistics.quantiles(nums, n=20, method="inclusive")[18] if len(nums) > 1 else nums[0]


def narrow(w: Widget, picked: dict[str, str], fields: set[str]) -> Widget:
    """`w` with the page's picked filter values added, for the fields its rows carry."""
    extra = {f: v for f, v in picked.items() if v and f in fields and f not in w.filter}
    return w.model_copy(update={"filter": {**w.filter, **extra}}) if extra else w


def aggregate(rows: list[dict], w: Widget, time_field: str | None, start: datetime | None) -> dict:
    """A data widget's rows, reduced: {"columns": [...], "rows": [[...]]}, the bin first, then
    `by`, then the measure (named as written). Rows are read oldest first, so last() is the
    newest value."""
    kind = MEASURE.fullmatch(w.measure).group(2) or "count"
    keep = []
    for r in rows:
        t = _ts(r.get(time_field)) if time_field else None
        if (start is not None or w.bin) and time_field and (t is None or (start is not None and t < start)):
            continue
        if all((str(r.get(k)) in v) if isinstance(v, list) else str(r.get(k)) == v
               for k, v in w.filter.items()):
            keep.append(r)
    if time_field:
        keep.sort(key=lambda r: _ts(r.get(time_field)) or datetime.min.replace(tzinfo=UTC))
    groups: dict[tuple, list] = {}
    for r in keep:
        key = ((_bin(_ts(r[time_field]), w.bin),) if w.bin else ()) + tuple(r.get(b) for b in w.by)
        groups.setdefault(key, []).append(r.get(w.field) if w.field else None)
    out = [[*k, _reduce(kind, vals)] for k, vals in groups.items()]
    if not out and not w.bin and not w.by:
        out = [[_reduce(kind, [])]]
    if w.sort:
        out.sort(key=lambda row: (row[-1] is None, row[-1] if row[-1] is not None else 0),
                 reverse=w.sort == "desc")
    elif w.bin:
        out.sort(key=lambda row: row[0])
    if w.limit:
        out = out[: w.limit]
    return {"columns": [*(["bin"] if w.bin else []), *w.by, w.measure], "rows": out}
