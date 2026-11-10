"""The feature store: Tecton's and Databricks Feature Engineering's workflow over dbt-features's
feature groups, each one a batch FeatureView, with its own query engine for BigQuery,
Spark and DuckDB.

    from gcpbricks.features import FeatureService, FeatureStore

    fs = FeatureStore()                                   # FEATURE_CONFIG or the published one
    fs.list_feature_views(family="revenue")
    fv = fs.get_feature_view("fs_demo_features")
    fv.join_keys, fv.timestamp_field, fv.feature_names
    df = fv.family("acquisition").get_features_in_range(start, end)   # the window's rows
    svc = FeatureService("forecast_30d", [fv[["cost", "signups"]], fv.family("revenue")])
    ts = fs.create_training_set(events, feature_service=svc)          # as of each event
    df = ts.load_df()
    ts = fs.create_training_set(fv.spine(start, end, where="cost > 0"),    # a view's rows
                                ["lbl_demo__labels"], exclude_columns=["feature_timestamp"])
    ts.write_parquet(path)                                # streamed to a file, never all in memory
    ts.to_sql()                                           # the one query it runs
    svc.save(model_dir)                                   # what the model was trained on
    scored = fs.score_batch(model, events, FeatureService.load(model_dir))
    fv.table_schema(), fv.max_timestamp()                 # the live table's columns, its frontier

The views are dbt-features's (feature_config.py): this module only reads them. A feature is
named by its view and itself ("view:feature"); where a list of features is taken, "view"
and "view/family" name several, as fv and fv.family() do.

Every read is one query. Ibis builds it over each view's declared columns and compiles it
to the engine's dialect; each view's table goes in as a subquery of the engine's own FROM
(BigQuery's time travel, a Parquet path, a spine's `where`), so the engine's own client
runs it: BigQuery's (labelled jobs, results streamed), Spark's session, DuckDB in-process.
A training set's point-in-time join is the latest row at or before each event, per view,
matched on that view's keys (no age limit: a date in the keys keeps a match inside its
day). A spine() view's own features are read off its rows, not joined back on.
"""
from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from gcpbricks import job_labels
from gcpbricks.feature_config import FeatureGroup, from_checkout

#: What dbt-features's CI publishes on every merge to main.
DEFAULT_CONFIG = "gs://my-ml-bucket/feature_store/features.json"
SPEC_FILE = "feature_spec.json"
#: The version of what a training read returns for the same spec and window. Bump it with
#: any change that alters a read's rows or values (2: a sliding window over earlier days no
#: longer cuts a source's frontier), so a project's cached training set goes stale with it.
FETCH_VERSION = 2
#: The column an event's (a spine row's) as-of time is read from, unless named.
EVENT_TS = "event_timestamp"
#: Where a DataFrame spine is uploaded for a BigQuery join: a temp table that expires after
#: 30 minutes. The dataset's name is from when Feast did this upload.
SCRATCH_DATASET = "feast"
ENGINES = ("bigquery", "spark", "duckdb")
#: How far back max_timestamp() looks first, before it reads the whole table: a daily source is a day
#: or two behind, so a staler one only costs the full read.
RECENT_DAYS = 7
#: Rows per Parquet row group in write_parquet: BigQuery's stream batches are far smaller.
ROW_GROUP_ROWS = 100_000
#: BigQuery Storage read streams a streamed read opens, each a download thread.
READ_STREAMS = 4
_IBIS_TYPES = {"Int64": "int64", "Float64": "float64", "Bool": "boolean", "String": "string",
               "UnixTimestamp": "timestamp"}
_DIALECTS = {"bigquery": "bigquery", "spark": "spark", "duckdb": "duckdb"}
_IBIS_DIALECTS = {"bigquery": "bigquery", "spark": "pyspark", "duckdb": "duckdb"}
_SPINE = "__spine"


# --- declarations ----------------------------------------------------------------------------

class FeatureReference:
    """Some of a view's features: fv[["a", "b"]] or fv.family("x"), as Tecton selects them."""

    def __init__(self, view: FeatureView, names: list[str]):
        unknown = sorted(set(names) - {f.name for f in view.features})
        if unknown:
            raise KeyError(f"{view.name} has no feature {unknown}")
        self.view, self.feature_names = view, list(dict.fromkeys(names))

    @property
    def refs(self) -> list[str]:
        return [f"{self.view.name}:{n}" for n in self.feature_names]

    def get_features_in_range(self, start: datetime | None = None, end: datetime | None = None,
                              *, where: str | None = None) -> pd.DataFrame:
        """Every row of the view's table with its timestamp in [start, end] (open ends
        unbounded) and `where`, a condition in the engine's SQL, holding: keys, timestamp
        and these features."""
        return self.view.store._read_range(self.view.group, self.feature_names, start, end, where)

    def __repr__(self) -> str:
        return f"FeatureReference({self.view.name!r}, {self.feature_names!r})"


class FeatureView(FeatureReference):
    """One dbt-features feature group: a batch feature view over its offline table, keyed by its
    meta.entity columns, its rows stamped by its meta.feature_timestamp column."""

    def __init__(self, store: FeatureStore, group: FeatureGroup):
        self.store, self.group = store, group
        super().__init__(self, [f.name for f in group.features])

    name = property(lambda self: self.group.name)
    description = property(lambda self: self.group.description)
    join_keys = property(lambda self: list(self.group.keys))
    timestamp_field = property(lambda self: self.group.timestamp)
    features = property(lambda self: list(self.group.features))
    #: "feature", or "label": its columns are training targets, not model inputs.
    kind = property(lambda self: self.group.kind)
    offline_table = property(lambda self: self.group.offline_table)

    @property
    def families(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for f in self.group.features:
            out.setdefault(f.family, []).append(f.name)
        return out

    def __getitem__(self, names: str | list[str]) -> FeatureReference:
        return FeatureReference(self, [names] if isinstance(names, str) else list(names))

    def family(self, name: str) -> FeatureReference:
        return FeatureReference(self, self.group.family(name))

    def spine(self, start: datetime | None = None, end: datetime | None = None, *,
              where: str | None = None) -> Spine:
        """The view's rows with their timestamp in [start, end] and `where` holding, as a
        training set's events: its keys and timestamp, left where the engine reads them."""
        return Spine(self.name, start, end, where)

    def table_schema(self) -> dict[str, str]:
        """The live table's columns and their types (BigQuery's FLOAT64, STRING, DATE, ...;
        Arrow's off BigQuery): metadata only, no row read. What the table holds now, which
        the published view can lag."""
        return self.store._table_schema(self.group)

    def max_timestamp(self) -> datetime | None:
        """The latest timestamp in the table, read as of the store's `as_of`: how far its
        data reaches. None for an empty table."""
        return self.store._max_timestamp(self.group)

    def __repr__(self) -> str:
        return f"FeatureView({self.name!r}, keys={self.join_keys}, {len(self.features)} features)"


class BatchFeatureView(FeatureReference):
    """A view computed over another view's rows (Tecton's BatchFeatureView with its
    transformation and Aggregates): a features.yaml's SQL stage -- aggregations over long
    views' rows up to the decision point, then transforms over the source view's columns
    and those -- in the training set's own query. Its features are the stage's columns
    (prefix + name), what the model's pipeline then reads instead of computing them."""

    def __init__(self, store: FeatureStore, name: str, source: FeatureView, stage,
                 at_days: int | None):
        from gcpbricks import feature_config as C
        from gcpbricks.feature_manager import sql as S
        from gcpbricks.feature_manager.plan import LANDMARK_COLUMNS, check_aggregations

        at_days = stage.at_days if at_days is None else at_days
        aggregated = [n for n in (*stage.nodes, *stage.labels) if n.kind == "aggregate"]
        if aggregated and at_days is None and not stage.landmarks:
            raise ValueError(f"{name}: aggregations need at_days (the decision point) or landmarks")
        check_aggregations(aggregated, source.name, None if stage.landmarks else at_days)
        self.store, self.name, self.source, self.stage, self.at_days = store, name, source, stage, at_days
        _, out = S.stage_program(stage, S.column)
        self.features = [C.Feature(name=n, dtype="Float64") for n in out]
        produced = {n.name for n in stage.nodes} | (set(LANDMARK_COLUMNS) if stage.landmarks else set())
        self.raw_columns = list(dict.fromkeys(r for n in stage.nodes if n.kind != "aggregate"
                                              for r in n.reads if r not in produced))
        super().__init__(self, [f.name for f in self.features])

    def __repr__(self) -> str:
        return f"BatchFeatureView({self.name!r}, source={self.source.name!r}, {len(self.features)} columns)"


@dataclass(frozen=True)
class FeatureService:
    """What a model reads, as "view:feature" refs (Tecton's FeatureService, Databricks'
    feature spec). Saved next to the model as feature_spec.json, so scoring looks up exactly
    what training did."""

    name: str
    features: list[str]
    #: Spine columns the model also reads as inputs (e.g. a segment's platform).
    spine_inputs: list[str] = field(default_factory=list)

    def __init__(self, name: str, features: list[FeatureReference | str], *,
                 spine_inputs: list[str] | None = None):
        refs: list[str] = []
        for item in features:
            if isinstance(item, FeatureReference):
                refs += item.refs
            elif ":" in item:
                refs.append(item)
            else:
                raise ValueError(f"{item!r}: a service names features as 'view:feature', a "
                                 f"FeatureView or a selection of one (fv[[...]], fv.family())")
        refs = list(dict.fromkeys(refs))
        dup = sorted(n for n, c in Counter(r.split(":", 1)[1] for r in refs).items() if c > 1)
        if dup:
            raise ValueError(f"features named the same in two views: {dup}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "features", refs)
        object.__setattr__(self, "spine_inputs", list(spine_inputs or []))

    @property
    def feature_names(self) -> list[str]:
        return [r.split(":", 1)[1] for r in self.features]

    def save(self, model_dir: str | Path) -> Path:
        path = Path(model_dir) / SPEC_FILE
        path.write_text(json.dumps(asdict(self), indent=2))
        return path

    @classmethod
    def load(cls, model_dir: str | Path) -> FeatureService:
        """A saved service; one saved before services had names loads with name ""."""
        d = json.loads((Path(model_dir) / SPEC_FILE).read_text())
        return cls(d.get("name", ""), d["features"], spine_inputs=d.get("spine_inputs"))


@dataclass(frozen=True)
class Spine:
    """A view's own rows with their timestamp in [start, end] (keys and timestamp), as a
    training set's events, left where the engine reads them: no round trip through pandas.
    `where`, a condition on the view's columns in the engine's SQL, keeps only the rows it
    holds for, as filtering the spine DataFrame before create_training_set does in
    Databricks."""

    group: str
    start: datetime | None = None
    end: datetime | None = None
    where: str | None = None


@dataclass
class TrainingSet:
    store: FeatureStore
    spine: pd.DataFrame | Spine
    feature_service: FeatureService
    timestamp_key: str
    exclude_columns: list[str] = field(default_factory=list)

    def load_df(self, *, float32: bool = False) -> pd.DataFrame:
        """The events with every feature joined as of each one's timestamp, less
        exclude_columns. float32: every float64 column as float32, half the memory, at
        float32's ~7 significant digits."""
        with self.store._execute(self._query()) as result:
            table = result.to_arrow()
        if float32:
            table = _float32(table)
        # Frees each Arrow column as pandas takes it, so the result isn't held twice.
        return table.to_pandas(self_destruct=True, split_blocks=True)

    def write_parquet(self, path: str | Path, *, float32: bool = False) -> int:
        """load_df()'s rows written to a local Parquet file, as Databricks'
        load_df().write.parquet() does, never all in memory at once: the result streams to
        the file batch by batch. Returns the rows written."""
        import pyarrow as pa

        with self.store._execute(self._query()) as result:
            writer, batches, rows = None, [], 0
            for batch in result.to_batches():
                batches.append(batch)
                rows += batch.num_rows
                if sum(b.num_rows for b in batches) >= ROW_GROUP_ROWS:
                    writer = _write_row_group(writer, path, pa.Table.from_batches(batches), float32)
                    batches = []
            if batches:
                writer = _write_row_group(writer, path, pa.Table.from_batches(batches), float32)
            elif writer is None:
                # No rows: an empty file that still has the result's columns.
                writer = _write_row_group(None, path, result.to_arrow(), float32)
            writer.close()
        return rows

    def to_sql(self) -> str:
        """The query load_df() runs, in the engine's dialect (a DataFrame spine read as
        `__spine`)."""
        return self.store._compile(self._query())

    @property
    def spec(self) -> FeatureService:
        """Deprecated: feature_service."""
        return self.feature_service

    def _query(self) -> _Query:
        return self.store._training_query(self.spine, self.feature_service.features,
                                          self.timestamp_key, self.exclude_columns)


# --- the store -------------------------------------------------------------------------------

def load_groups(config: str) -> dict[str, FeatureGroup]:
    """The groups in `config`: a rendered JSON (a path or gs:// URI), or a dbt-features checkout,
    read from its _models.yml directly."""
    if Path(config).is_dir():
        return from_checkout(config)
    if config.startswith("gs://"):
        from google.cloud import storage

        bucket, _, blob = config[len("gs://"):].partition("/")
        text = storage.Client().bucket(bucket).blob(blob).download_as_text()
    else:
        text = Path(config).read_text()
    return {g["name"]: FeatureGroup.model_validate(g) for g in json.loads(text)["groups"]}


class FeatureStore:
    def __init__(self, config: str | None = None, *, engine: str = "bigquery",
                 bigquery_project: str = "my-gcp-project",
                 bigquery_location: str = "us-central1", spark_conf: dict[str, str] | None = None,
                 spark_catalog: str | None = None, as_of: datetime | None = None):
        """`config`: see load_groups; defaults to FEATURE_CONFIG, then DEFAULT_CONFIG.
        `engine` reads the views' offline tables and runs the joins:
          "bigquery"  a dataset.table there, Application Default Credentials; an Iceberg
                      table BigQuery sees (managed, or in the BigLake metastore) reads as one
          "spark"     a Spark table, `spark_catalog` prefixed onto a dataset.table (e.g. the
                      Iceberg catalog `spark_conf` configures), or a Parquet path
          "duckdb"    an offline_table is a Parquet path: tests and local files
        `as_of` reads every view's table as it was then (BigQuery time travel, the last 7
        days), so a run that reads in several steps sees one version of each table."""
        if engine not in ENGINES:
            raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")
        if as_of is not None and engine != "bigquery":
            raise ValueError(f"as_of needs engine='bigquery', not {engine!r}")
        self.groups = load_groups(config or os.environ.get("FEATURE_CONFIG") or DEFAULT_CONFIG)
        self.engine = engine
        self.bigquery_project = bigquery_project
        self.bigquery_location = bigquery_location
        self.spark_conf = dict(spark_conf or {})
        self.spark_catalog = spark_catalog
        self.as_of = as_of
        self.derived: dict[str, BatchFeatureView] = {}

    # --- discovery -------------------------------------------------------------------------

    def get_feature_view(self, name: str) -> FeatureView:
        return FeatureView(self, self._group(name))

    def training_set_from_spec(self, plan, start: datetime | None = None, end: datetime | None = None, *,
                               where: str | None = None, features: list | None = None) -> TrainingSet:
        """The training rows a features.yaml states: its `spine` view's rows with timestamp
        in [start, end] (`where` filtering them), its SQL stage computed over them as its
        `decision` / `landmarks` say, the raw columns its Python steps read, and `features`
        from other views."""
        if plan.spine is None:
            raise ValueError("the spec states no `spine`; use create_training_set")
        stage = plan.sql_stage()
        name = f"{plan.spine}__{plan.sql_hash or 'raw'}"
        bfv = self.derived.get(name) or self.batch_feature_view(name, plan.spine, stage)
        fv = self.get_feature_view(plan.spine)
        raw = [c for c in stage.python_columns if c not in fv.join_keys and c != fv.timestamp_field]
        return self.create_training_set(Spine(plan.spine, start, end, where),
                                        [bfv, *([fv[raw]] if raw else []), *(features or [])])

    def batch_feature_view(self, name: str, source: str, stage, *,
                           at_days: int | None = None) -> BatchFeatureView:
        """A view computed in the training set's query from `source`'s rows: `stage` is a
        compiled features.yaml's SQL stage (feature_manager's Plan.sql_stage()), `at_days`
        the decision point its aggregations end at (that day included)."""
        if name in self.groups:
            raise ValueError(f"{name!r} is a feature view of dbt-features's")
        view = BatchFeatureView(self, name, self.get_feature_view(source), stage, at_days)
        self.derived[name] = view
        return view

    def list_feature_views(self, *, family: str | None = None,
                           kind: str | None = None) -> list[FeatureView]:
        views = [self.get_feature_view(n) for n in sorted(self.groups)]
        return [v for v in views if (family is None or family in v.families)
                and (kind is None or v.kind == kind)]

    # --- reads -----------------------------------------------------------------------------

    def create_training_set(self, df: pd.DataFrame | Spine,
                            features: list[FeatureReference | str] | None = None, *,
                            feature_service: FeatureService | None = None,
                            timestamp_key: str = EVENT_TS, spine_inputs: list[str] | None = None,
                            exclude_columns: list[str] | None = None) -> TrainingSet:
        """The events `df` with features joined on as of each one's timestamp, as
        Databricks' create_training_set: a DataFrame carries the keys, `timestamp_key` and
        any label; a view's spine() carries its keys and timestamp (`timestamp_key` is its
        timestamp_field), and labels come in as features of a label view. The features are
        `feature_service`'s, or `features` (FeatureViews, selections, "view",
        "view/family", "view:feature"). Nothing is read until load_df()."""
        if (features is None) == (feature_service is None):
            raise ValueError("pass features or a feature_service, not both")
        if feature_service is None:
            feature_service = FeatureService("", self._resolve(features), spine_inputs=spine_inputs)
        else:
            self._resolve(feature_service.features)
        if isinstance(df, Spine):
            timestamp_key = self._group(df.group).timestamp
        return TrainingSet(self, df, feature_service, timestamp_key, list(exclude_columns or []))

    def get_features_for_events(self, events: pd.DataFrame | Spine,
                                features: list[FeatureReference | str] | None = None, *,
                                feature_service: FeatureService | None = None,
                                timestamp_key: str = EVENT_TS) -> pd.DataFrame:
        """Tecton's get_features_for_events: create_training_set(...).load_df()."""
        return self.create_training_set(events, features, feature_service=feature_service,
                                        timestamp_key=timestamp_key).load_df()

    def score_batch(self, model, df: pd.DataFrame, feature_service: FeatureService, *,
                    timestamp_key: str = EVENT_TS, output: str = "prediction") -> pd.DataFrame:
        """The events with the service's features joined on, and `model.predict` of them in
        `output`. The model sees the columns in the service's order; a feature dbt-features has
        since dropped fails here, not as a silent NaN column."""
        out = self.get_features_for_events(df, feature_service=feature_service,
                                           timestamp_key=timestamp_key)
        out[output] = model.predict(out[feature_service.spine_inputs + feature_service.feature_names])
        return out

    # --- deprecated: demo_model's main reads through these until it moves to the above ---

    def list_groups(self, family: str | None = None) -> list[dict]:
        return [self.describe(v.name) for v in self.list_feature_views(family=family)]

    def describe(self, group: str) -> dict:
        v = self.get_feature_view(group)
        return {"name": v.name, "description": v.description, "offline_table": v.offline_table,
                "keys": v.join_keys, "timestamp": v.timestamp_field, "families": v.families,
                "project": v.group.project}

    def table_schema(self, group: str) -> dict[str, str]:
        return self.get_feature_view(group).table_schema()

    def max_timestamp(self, group: str) -> datetime | None:
        return self.get_feature_view(group).max_timestamp()

    def read_table(self, group: str, start: datetime | None = None, end: datetime | None = None,
                   *, features: list[str] | None = None, family: str | None = None) -> pd.DataFrame:
        v = self.get_feature_view(group)
        sel = v[features] if features is not None else (v.family(family) if family else v)
        return sel.get_features_in_range(start, end)

    def spine(self, group: str, start: datetime | None = None, end: datetime | None = None, *,
              where: str | None = None) -> Spine:
        return self.get_feature_view(group).spine(start, end, where=where)

    def training_set(self, spine: pd.DataFrame | Spine, features: list[str], *,
                     timestamp_col: str = EVENT_TS, spine_inputs: list[str] | None = None,
                     exclude_columns: list[str] | None = None) -> TrainingSet:
        return self.create_training_set(spine, features, timestamp_key=timestamp_col,
                                        spine_inputs=spine_inputs, exclude_columns=exclude_columns)

    def resolve(self, features: list[str]) -> list[str]:
        return self._resolve(features)

    # --- the query engine ------------------------------------------------------------------

    def _group(self, name: str) -> FeatureGroup:
        if name not in self.groups:
            raise KeyError(f"no feature view {name!r}; views: {sorted(self.groups)}")
        return self.groups[name]

    def _resolve(self, features: list[FeatureReference | str]) -> list[str]:
        """FeatureViews, selections, "view" / "view/family" / "view:feature" -> refs."""
        refs: list[str] = []
        for item in features:
            if isinstance(item, FeatureReference):
                if not (isinstance(item.view, BatchFeatureView) and self.derived.get(item.view.name) is item.view):
                    self._group(item.view.name)
                refs += item.refs
            elif ":" in item:
                view, _, name = item.partition(":")
                v = self.derived.get(view) or self.get_feature_view(view)
                refs += v[[name]].refs
            else:
                view, _, family = item.partition("/")
                v = self.get_feature_view(view)
                refs += (v.family(family) if family else v).refs
        return FeatureService("", refs).features

    def _table(self, g: FeatureGroup, alias: str | None = None):
        """The view as an Ibis table of its declared columns, named `alias` (default: the
        view), to be swapped for its offline table when the query compiles."""
        import ibis

        schema = {**{k: _IBIS_TYPES[t] for k, t in g.key_types.items()},
                  g.timestamp: "timestamp",
                  **{f.name: _IBIS_TYPES[f.dtype] for f in g.features}}
        return ibis.table(schema, name=alias or g.name)

    def _read_range(self, g: FeatureGroup, names: list[str], start, end, where) -> pd.DataFrame:
        t = self._table(g)
        t = _in_window(t, g.timestamp, start, end)
        q = _Query(t.select(*g.keys, g.timestamp, *names), {g.name: (g, where)})
        with self._execute(q) as result:
            return result.to_arrow().to_pandas()

    def _training_query(self, spine: pd.DataFrame | Spine, refs: list[str], timestamp_key: str,
                        exclude: list[str]) -> _Query:
        """One query: the events, then each other view's features as of each event's
        timestamp (the latest row at or before it), matched on that view's keys."""
        import ibis

        by_view: dict[str, list[str]] = {}
        for ref in refs:
            view, _, name = ref.partition(":")
            by_view.setdefault(view, []).append(name)
        derived = {v: by_view.pop(v) for v in list(by_view) if v in self.derived}
        # A derived view's raw columns come in as its source's; those nothing asked for go
        # once it is computed.
        requested = {v: list(names) for v, names in by_view.items()}
        scratch: list[str] = []
        for v in derived:
            bfv = self.derived[v]
            have = by_view.setdefault(bfv.source.name, [])
            extra = [c for c in bfv.raw_columns if c not in have]
            have += extra
            scratch += [c for c in extra if c not in requested.get(bfv.source.name, [])]
        sources: dict[str, tuple] = {}
        if isinstance(spine, Spine):
            g = self._group(spine.group)
            sources[_SPINE] = (g, spine.where)
            events = _in_window(self._table(g, _SPINE), g.timestamp, spine.start, spine.end)
            events = events.select(*g.keys, g.timestamp, *by_view.pop(spine.group, []))
        else:
            if timestamp_key not in spine.columns:
                raise KeyError(f"the spine has no {timestamp_key!r} column")
            from gcpbricks.feature_manager.sql import ibis_schema

            spine = _utc(spine, timestamp_key)
            sources[_SPINE] = (spine, None)
            events = ibis.table(ibis_schema(spine), name=_SPINE)
        ts = timestamp_key
        out = events
        selected = [events[c] for c in events.columns]
        for view, names in by_view.items():
            g = self._group(view)
            sources[view] = (g, None)
            rows = self._table(g)
            keys = list(g.keys)
            missing = sorted(set(keys) - set(events.columns))
            if missing:
                raise KeyError(f"the spine lacks {view}'s keys {missing}")
            distinct = events.select(*keys, ts).distinct()
            rows = rows.filter(rows[g.timestamp] <= distinct[ts].max().as_scalar())
            rows = rows.select(**{f"_k{i}": rows[k] for i, k in enumerate(keys)},
                               _fts=rows[g.timestamp], **{n: rows[n] for n in names})
            matched = distinct.join(rows, [distinct[k] == rows[f"_k{i}"] for i, k in enumerate(keys)]
                                    + [rows._fts <= distinct[ts]])
            latest = ibis.row_number().over(group_by=[matched[k] for k in keys] + [matched[ts]],
                                            order_by=ibis.desc(matched._fts))
            matched = matched.mutate(_rn=latest)
            matched = matched.filter(matched._rn == 0).select(
                **{f"_j{i}": matched[k] for i, k in enumerate(keys)}, _ts=matched[ts],
                **{n: matched[n] for n in names})
            out = out.left_join(matched, [out[k] == matched[f"_j{i}"] for i, k in enumerate(keys)]
                                + [out[ts] == matched._ts])
            selected += [matched[n] for n in names]
        expr = out.select(*selected)
        window = (spine.start, spine.end) if isinstance(spine, Spine) else None
        for v, names in derived.items():
            expr = self._derive(expr, self.derived[v], names, ts, sources, window)
        if scratch:
            expr = expr.drop(*dict.fromkeys(scratch))
        if exclude:
            expr = expr.drop(*exclude)
        return _Query(expr, sources)

    def _derive(self, table, view: BatchFeatureView, names: list[str], ts: str, sources: dict,
                window: tuple | None = None):
        """table (the events with the source's raw columns) with `names` of `view`: with
        landmarks, a row per decision day (and horizon); its aggregations and labels
        joined on; then its transforms' program over all of it. `window`: the spine's
        (start, end), bounding each long view's read by constants."""
        import ibis

        from gcpbricks.feature_manager import sql as S

        stage = view.stage
        aggregated = [n for n in (*stage.nodes, *stage.labels) if n.kind == "aggregate"]
        lm = stage.landmarks
        if lm:
            # One unnest per axis, a level each: two in one select would zip, not cross.
            table = table.mutate(decision_day=ibis.literal(lm["at"], "array<int64>").unnest())
            table = table.mutate(_dts=table[ts] + table.decision_day.as_interval("D") + ibis.interval(days=1))
            if lm["horizons"]:
                table = table.mutate(horizon_day=ibis.literal(lm["horizons"], "array<int64>").unnest())
                table = table.mutate(_hts=table[ts] + table.horizon_day.as_interval("D") + ibis.interval(days=1))
        elif aggregated:
            table = table.mutate(_dts=table[ts] + ibis.interval(days=view.at_days + 1))
        points = {**lm, "anchored": True} if lm else {"at": [view.at_days], "horizons": [], "anchored": False}
        by_source: dict[str, list] = {}
        for node in aggregated:
            by_source.setdefault((node.params["source"], tuple(sorted(node.params["over"]))), []).append(node)
        frontiers = []
        for (source, _), nodes in by_source.items():
            table, frontier = self._aggregate(table, ts, self._group(source), nodes, sources,
                                              window, points)
            frontiers.append(frontier)
        if lm and frontiers:
            # A decision day the data doesn't reach yet has no row; a horizon it doesn't
            # reach, no label. Each source's frontier is one row joined on, read once.
            for i, f in enumerate(frontiers):
                table = table.cross_join(f.rename(**{f"_reach{i}": "_reach"}))
            reach = [table[f"_reach{i}"] for i in range(len(frontiers))]
            reach = reach[0] if len(reach) == 1 else ibis.least(*reach)
            table = table.filter(table._dts <= reach + ibis.interval(days=1))
            if lm["horizons"]:
                late = table._hts > reach + ibis.interval(days=1)
                table = table.mutate(**{n.name: ibis.ifelse(late, ibis.null("float64"), table[n.name])
                                        for n in stage.labels})
        table = table.drop(*[c for c in table.columns if c in ("_dts", "_hts") or c.startswith("_reach")])
        program, out = S.stage_program(stage, S.column)
        keep = [c for c in table.columns if not c.startswith(stage.prefix)]
        return self._with_program(table, program, [*keep, *[n for n in out if n in names]], sources)

    def _with_program(self, table, program, keep: list[str], sources: dict):
        """table with a program's columns, `keep` of them: a placeholder _compile swaps for
        the program's layers (feature_manager.sql) over table's own query."""
        import ibis

        name = f"__program{len(sources)}"
        sources[name] = (_Program(table, program, keep), None)
        have = table.schema()
        return ibis.table(ibis.schema({c: have.get(c, "float64") for c in keep}), name=name)

    def _aggregate(self, table, ts: str, g: FeatureGroup, nodes: list, sources: dict,
                   window: tuple | None, landmarks: dict | None):
        """table with each node's aggregation over g's rows of the row's entity (g's keys
        the spine has; those it lacks, the node's `over`) in the node's windows, and g's
        frontier (its latest timestamp read). A feature window ends at the decision point
        (_dts), a label's at the horizon (_hts), both days included; an age window is days
        past the anchor (ts). An empty window: sum and count 0, the rest NULL."""
        import ibis

        from gcpbricks.feature_manager import sql as S

        # `over` keys are aggregated across even when the spine has them: a long spine's
        # row reads its entity's other rows.
        over = set(nodes[0].params["over"])
        keys = [k for k in g.keys if k in table.columns and k not in over]
        stray = sorted(set(g.keys) - set(keys) - over)
        if stray or not keys:
            raise KeyError(f"{nodes[0].name}: the spine lacks {g.name}'s keys {stray or g.keys}; "
                           f"aggregate over them with `over:` if that's what's meant")
        alias = f"__agg_{g.name}{len(sources)}"
        sources[alias] = (g, None)
        src = self._table(g, alias)
        bounds = {n.name: _windows(n, landmarks) for n in nodes}
        if window is not None:
            # Constant bounds, so BigQuery prunes the source's partitions.
            los = [b[0] for bs in bounds.values() for b in bs]
            lo = None if None in los else min(d + _base_days(base, landmarks, False) for base, d in los)
            hi = max(b[1][1] + _base_days(b[1][0], landmarks, True) for bs in bounds.values() for b in bs)
            # The read's latest row is this source's frontier, which gates the rows' decision days
            # and horizons (_derive): read as far as the latest of them, or a window ending before
            # the decision point (a sliding window over earlier days) would pass for the data's end.
            # The join below still keeps each node to its own window.
            gated = "horizon" if (landmarks or {}).get("horizons") else "decision"
            hi = max(hi, _base_days(gated, landmarks, True))
            if window[0] is not None and lo is not None:
                src = src.filter(src[g.timestamp] >= ibis.timestamp(_tz(window[0])) + ibis.interval(days=lo))
            if window[1] is not None:
                src = src.filter(src[g.timestamp] < ibis.timestamp(_tz(window[1])) + ibis.interval(days=hi + 1))
        # Each input on g's own rows: a column, or a formula's program; NaN as NULL, which
        # the aggregates skip.
        program, reads = S.Program("_a"), {}
        low = S.Lowering(program, lambda n: reads.setdefault(n, program.bind(S.column(n))))
        for i, node in enumerate(nodes):
            p = node.params
            value = (low.lower(p["input_formula"]) if p["input_formula"] is not None
                     else program.bind(S.column(p["input_column"])))
            program.bind(S._v(value), name=f"_in{i}")
        rows = self._with_program(src, program, [*keys, g.timestamp, *[f"_in{i}" for i in range(len(nodes))]],
                                  sources)
        rows = rows.select(**{f"_k{i}": rows[k] for i, k in enumerate(keys)}, _fts=rows[g.timestamp],
                           **{f"_in{i}": ibis.ifelse(rows[f"_in{i}"].isnan(), ibis.null("float64"),
                                                     rows[f"_in{i}"]) for i in range(len(nodes))})
        frontier = rows.aggregate(_reach=rows._fts.max())
        points = [c for c in ("_dts", "_hts") if c in table.columns]
        events = table.select(*keys, ts, *points).distinct()
        cond = [events[k] == rows[f"_k{i}"] for i, k in enumerate(keys)]
        his: dict[str, int] = {}
        for bs in bounds.values():
            for _, (base, d) in bs:
                his[base] = max(his.get(base, d), d)
        ends = [_at(events, ts, base, d) for base, d in his.items()]
        cond.append(rows._fts < (ends[0] if len(ends) == 1 else ibis.greatest(*ends)))
        los = {b[0] for bs in bounds.values() for b in bs}
        if None not in los and len({base for base, _ in los}) == 1:
            base = next(iter(los))[0]
            cond.append(rows._fts >= _at(events, ts, base, min(d for _, d in los)))
        matched = events.join(rows, cond)

        aggs, function_of = {}, {}
        for i, node in enumerate(nodes):
            x = matched[f"_in{i}"]
            for j, (lo_b, hi_b) in enumerate(bounds[node.name]):
                inside = matched._fts < _at(matched, ts, *hi_b)
                if lo_b is not None:
                    inside = inside & (matched._fts >= _at(matched, ts, *lo_b))
                if node.params.get("since_decision"):
                    inside = inside & (matched._fts >= matched._dts)
                col = node.params["sql_column"] + ("" if node.shape == "vector" else f"__{j}")
                aggs[col] = _aggregation(node.params["function"], x, matched._fts, inside)
                function_of[col] = node.params["function"]
        agg = matched.group_by(**{f"_a{i}": matched[k] for i, k in enumerate(keys)}, _ats=matched[ts],
                               **{f"_p{c}": matched[c] for c in points}).aggregate(**aggs)
        joined = table.left_join(agg, [table[k] == agg[f"_a{i}"] for i, k in enumerate(keys)]
                                 + [table[ts] == agg._ats] + [table[c] == agg[f"_p{c}"] for c in points])
        return joined.select(*table.columns, **{
            c: ibis.coalesce(joined[c], 0.0) if function_of[c] in ("sum", "count") else joined[c]
            for c in aggs}), frontier

    def _compile(self, q: _Query, names: dict[str, str] | None = None) -> str:
        """q's SQL in the engine's dialect, each source table swapped for a subquery over
        its offline table (or `names`' table for a DataFrame spine)."""
        from gcpbricks.feature_manager import sqlx

        return sqlx.render(self._tree(q.expr, q.sources, names or {}), _DIALECTS[self.engine])

    def _tree(self, expr, sources: dict, names: dict):
        """expr as a sqlglot tree, each placeholder swapped for its source: a view's table,
        a DataFrame spine's, or a program's layers over its own input's tree."""
        import ibis
        import sqlglot
        from sqlglot import exp

        dialect = _DIALECTS[self.engine]
        if self.engine == "spark":
            ibis.pyspark  # noqa: B018  (loading the backend registers options its compiler reads)
        tree = sqlglot.parse_one(ibis.to_sql(expr, dialect=_IBIS_DIALECTS[self.engine]), read=dialect)
        for t in list(tree.find_all(exp.Table)):
            if t.db or t.catalog or t.name not in sources:
                continue
            source, where = sources[t.name]
            if isinstance(source, _Program):
                sub = source.program.sql(self._tree(source.table, sources, names), source.keep)
            else:
                frm = (self._from(source) if isinstance(source, FeatureGroup)
                       else self._quote(names.get(t.name, t.name)))
                sub = sqlglot.parse_one(f"SELECT * FROM {frm}" + (f" WHERE ({where})" if where else ""),
                                        read=dialect)
            t.replace(sub.subquery(t.alias or t.name))
        return tree

    def _from(self, g: FeatureGroup) -> str:
        """The view's offline table as a FROM source in the engine's SQL."""
        if self.engine == "bigquery":
            source = f"`{self._bigquery_table(g)}`"
            if self.as_of is not None:
                ms = int(_tz(self.as_of).timestamp() * 1000)
                source += f" FOR SYSTEM_TIME AS OF TIMESTAMP_MILLIS({ms})"
            return source
        if self.engine == "spark":
            if "/" in g.offline_table:
                return f"parquet.`{g.offline_table}`"
            return (f"{self.spark_catalog}.{g.offline_table}"
                    if self.spark_catalog and g.offline_table.count(".") == 1 else g.offline_table)
        return "read_parquet('" + g.offline_table.replace("'", "''") + "')"

    def _quote(self, name: str) -> str:
        return f'"{name}"' if self.engine == "duckdb" else f"`{name}`"

    def _bigquery_table(self, g: FeatureGroup) -> str:
        return g.offline_table if g.offline_table.count(".") == 2 else (
            f"{self.bigquery_project}.{g.offline_table}")

    @contextmanager
    def _execute(self, q: _Query):
        """q run on the engine; a DataFrame spine's upload lives until the block exits."""
        spine = q.sources.get(_SPINE, (None,))[0]
        spine = spine if isinstance(spine, pd.DataFrame) else None
        if self.engine == "bigquery":
            with self._bigquery_run(q, spine) as result:
                yield result
        elif self.engine == "duckdb":
            import duckdb

            con = duckdb.connect()
            con.execute("SET TimeZone = 'UTC'")
            if spine is not None:
                con.register(_SPINE, spine)
            try:
                result = con.sql(self._compile(q)).arrow()
                # duckdb >= 1.4 gives a RecordBatchReader, earlier versions a Table.
                yield _Result(result.read_all() if hasattr(result, "read_all") else result)
            finally:
                con.close()
        else:
            session = self._spark()
            view = None
            if spine is not None:
                view = f"spine_{uuid.uuid4().hex}"
                session.createDataFrame(spine).createOrReplaceTempView(view)
            try:
                yield _Result(_spark_arrow(session.sql(self._compile(q, {_SPINE: view}))))
            finally:
                if view:
                    session.catalog.dropTempView(view)

    @contextmanager
    def _bigquery_run(self, q: _Query, spine: pd.DataFrame | None):
        client = self._bigquery_client()
        scratch = None
        if spine is not None:
            scratch = f"{self.bigquery_project}.{SCRATCH_DATASET}.spine_{uuid.uuid4().hex}"
            client.load_table_from_dataframe(spine, scratch).result()
            table = client.get_table(scratch)
            table.expires = datetime.now(UTC) + timedelta(minutes=30)
            client.update_table(table, ["expires"])
        # A destination table: a query's anonymous one returns at most 10 GB (responseTooLarge).
        out = f"{self.bigquery_project}.{SCRATCH_DATASET}.result_{uuid.uuid4().hex}"
        try:
            from google.cloud import bigquery

            job = client.query(self._compile(q, {_SPINE: scratch}),
                               job_config=bigquery.QueryJobConfig(destination=out))
            job.result()
            table = client.get_table(out)
            table.expires = datetime.now(UTC) + timedelta(hours=6)
            client.update_table(table, ["expires"])
            yield _BigQueryResult(job)
        finally:
            client.delete_table(out, not_found_ok=True)
            if scratch:
                client.delete_table(scratch, not_found_ok=True)

    def _bigquery_client(self):
        return job_labels.bigquery_client(project=self.bigquery_project,
                                          location=self.bigquery_location)

    def _spark(self):
        from pyspark.sql import SparkSession

        builder = SparkSession.builder
        for k, v in self.spark_conf.items():
            builder = builder.config(k, v)
        return builder.getOrCreate()

    def _table_schema(self, g: FeatureGroup) -> dict[str, str]:
        if self.engine == "bigquery":
            table = self._bigquery_client().get_table(self._bigquery_table(g))
            return {f.name: f.field_type for f in table.schema}
        if self.engine == "spark" and "/" not in g.offline_table:
            return {f.name: f.dataType.simpleString()
                    for f in self._spark().table(self._from(g)).schema.fields}
        import pyarrow.parquet as pq

        return {f.name: str(f.type) for f in pq.read_schema(g.offline_table)}

    def _max_timestamp(self, g: FeatureGroup) -> datetime | None:
        """The latest timestamp, looked for in the last RECENT_DAYS before `as_of` (or now) first: a
        constant bound BigQuery prunes partitions on, and a view (a hash and GROUP BY over its base)
        computes only those days. The whole table only when that window holds nothing."""
        import ibis

        t = self._table(g)
        since = _tz(self.as_of or datetime.now(UTC)) - timedelta(days=RECENT_DAYS)
        for read in (t.filter(t[g.timestamp] >= ibis.timestamp(since)), t):
            q = _Query(read.aggregate(ts=read[g.timestamp].max()), {g.name: (g, None)})
            with self._execute(q) as result:
                latest = result.to_arrow().column("ts")[0].as_py()
            if latest is not None:
                return latest
        return None


# --- internals -------------------------------------------------------------------------------

@dataclass
class _Program:
    """A placeholder's source: `program`'s layers over `table`'s query, leaving `keep`."""
    table: object
    program: object
    keep: list


@dataclass
class _Query:
    expr: object
    #: placeholder table name -> (its FeatureGroup or a DataFrame spine, a `where` on it)
    sources: dict[str, tuple]


class _Result:
    def __init__(self, table):
        self.table = table

    def to_arrow(self):
        return self.table

    def to_batches(self):
        return self.table.to_batches(max_chunksize=ROW_GROUP_ROWS)


class _BigQueryResult:
    def __init__(self, job):
        self.job = job

    def to_arrow(self):
        return self.job.result().to_arrow(create_bqstorage_client=True)

    def to_batches(self):
        from google.cloud import bigquery_storage

        # max_queue_size bounds how far the download runs ahead of the writes; max_stream_count
        # how many pages are held at once: one thread per stream, each holding its next page,
        # and a large result left to the server opens hundreds of streams.
        return self.job.result().to_arrow_iterable(
            bqstorage_client=bigquery_storage.BigQueryReadClient(), max_queue_size=4,
            max_stream_count=READ_STREAMS)


def _windows(node, landmarks: dict) -> list[tuple[tuple[str, int] | None, tuple[str, int]]]:
    """A node's windows as [lo, hi) bounds, each (base, days past it): base "anchor" (the
    spine's timestamp), "decision" (_dts, the end of the decision day) or "horizon"
    (_hts, a label's); lo None, unbounded. `lifetime` is from the anchor on with landmarks
    (their days count from it), unbounded without."""
    from gcpbricks.feature_manager import sql as S

    window = node.params["window"]
    point = "horizon" if node.params.get("label") else "decision"
    if isinstance(window, dict) and "from_day" in window:
        return [(("anchor", window["from_day"]), ("anchor", window["to_day"] + 1))]
    if window == "lifetime":
        return [(("anchor", 0) if landmarks["anchored"] else None, (point, 0))]
    if isinstance(window, str):
        return [((point, -S._days(window)), (point, 0))]
    if "series_start" in window:
        size = S._days(window["window_size"])
        step = S._days(window.get("step_size") or window["window_size"])
        start = S._days(window["series_start"])
        ends = [start + step * (i + 1) for i in range(S.series_width(window))]
        return [((point, e - size), (point, e)) for e in ends]
    stop = S._days(window.get("offset") or "0")
    return [((point, stop - S._days(window["window_size"])), (point, stop))]


def _at(t, ts: str, base: str, days: int):
    import ibis

    return t[{"anchor": ts, "decision": "_dts", "horizon": "_hts"}[base]] + ibis.interval(days=days)


def _base_days(base: str, landmarks: dict | None, latest: bool) -> int:
    """How many days past the anchor a base is, at its earliest (or latest) row."""
    if base == "anchor":
        return 0
    days = (landmarks or {}).get("horizons" if base == "horizon" else "at") or [0]
    return (max(days) if latest else min(days)) + 1


def _aggregation(function: str, x, ts, where):
    """An aggregate of x over the rows `where` holds: count and count_distinct as numbers;
    first/last by timestamp."""
    if function == "sum":
        return x.sum(where=where)
    if function == "count":
        return x.count(where=where).cast("float64")
    if function == "mean":
        return x.mean(where=where)
    if function in ("min", "max"):
        return getattr(x, function)(where=where)
    if function in ("stddev_samp", "stddev_pop"):
        return x.std(how="sample" if function.endswith("samp") else "pop", where=where)
    if function in ("var_samp", "var_pop"):
        return x.var(how="sample" if function.endswith("samp") else "pop", where=where)
    if function == "count_distinct":
        return x.nunique(where=where).cast("float64")
    return (x.argmin if function == "first" else x.argmax)(ts, where=where)


def _tz(t: datetime) -> datetime:
    """A naive datetime as UTC."""
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _utc(df: pd.DataFrame, column: str) -> pd.DataFrame:
    """df with `column`, when naive, as UTC."""
    if df[column].dt.tz is not None:
        return df
    return df.assign(**{column: df[column].dt.tz_localize("UTC")})


def _in_window(t, column: str, start: datetime | None, end: datetime | None):
    import ibis

    if start is not None:
        t = t.filter(t[column] >= ibis.timestamp(_tz(start)))
    if end is not None:
        t = t.filter(t[column] <= ibis.timestamp(_tz(end)))
    return t


def _spark_arrow(df):
    import pyarrow as pa

    to_arrow = getattr(df, "toArrow", None)
    return to_arrow() if to_arrow else pa.Table.from_pandas(df.toPandas(), preserve_index=False)


def _write_row_group(writer, path: str | Path, table, float32: bool):
    """`table` appended to the Parquet file at `path`; the writer, opened on the first call."""
    import pyarrow.parquet as pq

    if float32:
        table = _float32(table)
    writer = writer or pq.ParquetWriter(path, table.schema)
    writer.write_table(table)
    return writer


def _float32(table):
    import pyarrow as pa

    return table.cast(pa.schema([f.with_type(pa.float32()) if pa.types.is_float64(f.type) else f
                                 for f in table.schema]))


# --- deprecated: demo_model's main reads through these until it moves to the above -----------

def FeatureSpec(features: list[str], spine_inputs: list[str] | None = None) -> FeatureService:
    return FeatureService("", features, spine_inputs=spine_inputs)


def score_batch(store: FeatureStore, model, spine: pd.DataFrame, spec: FeatureService, *,
                timestamp_col: str = EVENT_TS, output: str = "prediction") -> pd.DataFrame:
    return store.score_batch(model, spine, spec, timestamp_key=timestamp_col, output=output)
