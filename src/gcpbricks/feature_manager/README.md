# `gcpbricks.feature_manager` — features declared in `features.yaml`

A model's feature engineering as data: a project's `features.yaml`, compiled into a fitted
sklearn transformer the model keeps on its estimator. The engine is the platform's; the
spec is DS-owned and lives in the model's project. Install it with the `feature-manager`
extra (numpy, pandas, scikit-learn); compiling a spec needs only numpy.

```python
from gcpbricks.feature_manager import FeaturePipeline

class MyModel(MaturityWindowModel):
    def fit(self, train_df):
        df = ...                                    # model-owned prep: filters, grouping, target
        self.features_pipe = FeaturePipeline.from_manifest(manifest)   # manifest["features"]
        feat = self.features_pipe.fit_transform(df, y)   # y only if a fit reads "@target"
        X = feat[self.features_pipe.inputs()]       # the `default` inputs set
        ...

    def predict(self, df, **kwargs):
        feat = self.features_pipe.transform(df)     # frozen state, never refit
        ...
```

## Blocks: what runs in SQL, what in Python

A spec states its features in three blocks, in the order they run; the older single
`define` list (below) still compiles, everything in it in Python.

```yaml
aggregations:      # stateless, SQL: Tecton's Aggregate over a long feature view's rows
  - {source: fs_user_ltv__long, input: total_val, function: sum, time_window: 3d}       # total_val_sum_3d
  - {source: fs_user_ltv__long, input: total_val, function: sum, time_window: lifetime}
  - {source: fs_user_ltv__long, input: total_val, function: sum,
     time_window: {window_size: 3d, offset: -3d}}                                     # the 3 days before
  - {name: val_daily, source: fs_user_ltv__long, input: total_val, function: sum,
     time_window: {series_start: -3d, window_size: 1d}, keep: false}                  # a matrix, a day each
transforms:        # stateless, SQL: row-wise formulas, every op one SQL states
  - {name: ratio_d7, formula: "total_val_day_7_cum / maximum(cost, 1e-9)"}
  - {name: rev_trend, formula: "row_slope(val_daily)"}
preprocessing:     # stateful or numpy-only, Python: fit on the training rows, frozen
  - {name: geo_te, fit: mean, of: "@target", by: geo}
  - {name: gini, formula: "row_gini(cols('total_val_day_{d}', d=range(0, 8)))"}
  - {name: siblings, udf: "model:compute_sibling_features", outputs: [sibling_count_30d], inputs: [geo]}
```

- **aggregations**: `function` is one of sum, count, mean, min, max, stddev_samp/pop,
  var_samp/pop, count_distinct, first, last; `input` a column of `source` or
  `{name, formula}` over its row. A window ends at the decision point (the model's
  `at_days`, that day included): `3d` at day 7 is days 5-7; `lifetime` every day up to 7;
  `offset: -3d` ends it three days earlier; a series is `series_start / step_size`
  windows, earliest first. An empty window's sum and count are 0, the rest NaN. The
  default name is `{input}_{function}_{window}`.
- **transforms** may read the feature view's numeric columns, aggregations and earlier
  transforms; an op SQL lacks (row_gini, row_polyfit_r2, float32, `%`, `**` other than
  -1/0.5/1-4) fails the compile with "move it to preprocessing".
- **preprocessing** takes every `define` kind (`formula` for `expr`, `udf` for `custom`)
  and reads anything above; nothing above reads it, so the flow is one way.

`FeatureStore.batch_feature_view(name, source, plan.sql_stage(), at_days=)` computes the
SQL blocks in the training set's own query (sql.py lowers each op as numpy computes it;
tests/test_feature_sql.py holds that on DuckDB, Spark and BigQuery) and hands back their
values as `__sql_<spec hash>__<name>`. The pipeline reads a value only under its own spec's
hash; without one it computes a transform itself, and fails on an aggregation once
something reads it. `sql.compute_stage(stage, df)` runs the transforms on a DataFrame in
DuckDB, as golden fixtures check them.

### Spine and decision: the rows, stated in the spec

```yaml
spine: fs_demo__segment_cost   # the training rows: one per row of this view
decision: {at_days: 7}                    # windows end at D7, that day included; or `landmarks`
```

With both stated the spec alone says what every feature is, and
`FeatureStore.training_set_from_spec(plan, start, end, where=)` builds the training set:
the spine's rows with timestamp in [start, end], the SQL blocks over them, and the raw
columns the Python steps read. The three shapes:

| spine | decision | rows |
|---|---|---|
| the entity view (`segment_cost`) | `decision: {at_days: N}` | one per entity, features as of DN |
| the entity view | `landmarks: {at, horizons}` | entity x decision day x horizon, with labels |
| the long view (`segment_long`) | `decision: {at_days: 0}` | one per entity x age, each row's recent days |

`python -m gcpbricks.feature_manager.explain features.yaml --dbt-features DIR [--markdown]`
prints each feature as a sentence (`total_val_sum_3d: sum of total_val in …, D5..D7, per
spine row, across age; none there: 0`), to check against what was meant before training
and as a guide's lookup table. `preview(df)` shows a training set without the
`__sql_<hash>__` prefixes.

Two mistakes that would compute without failing fail the compile: an age window past
`decision.at_days`, and an aggregation whose source is the spine with `over` unstated
(`over: [age]` to read the entity's other rows, `over: []` for each row's own key).

### Age windows, `over`, landmarks and labels

```yaml
landmarks: {at: [3, 7], horizons: [30]}   # a row per entity x decision day x horizon
                                          # at: {range: [0, 91, 7]} for D0, D7, ..., D90
aggregations:
  - {name: "val_d{d}_cum", source: fs_demo__segment_long, input: total_val, function: sum,
     time_window: {from_day: 0, to_day: "{d}"}, over: [age], for_each: {d: [0, 1, 3]}}
  - {source: fs_demo__segment_long, input: total_val, function: sum,
     time_window: lifetime, over: [age]}
labels:                                   # targets at each horizon, never features
  - {name: rev_cum, source: fs_demo__segment_long, input: total_val,
     function: sum, time_window: lifetime, over: [age]}
  - {name: rev_after, source: fs_demo__segment_long, input: total_val,
     function: sum, time_window: lifetime, over: [age], since_decision: true}
```

- `{from_day, to_day}` is days past the anchor (the spine's timestamp, e.g. install
  day), both included, whatever the decision day: a wide table's `<stem>_day_<N>_cum` is
  `{from_day: 0, to_day: N}`.
- An aggregation joins on the source's keys the spine has, less those in its `over`
  (aggregated across); a key the spine lacks must be listed there, or the training set
  fails. With the long view itself as the spine and `at_days=0`, each (segment, age) row
  keeps its age and reads its segment's rows up to its own day: `3d` with `over: [age]`
  is the last 3 days, a `1d` series under `row_slope` their trend.
- `landmarks` replaces `at_days`: each spine row once per `at` day (column
  `decision_day`) and horizon (`horizon_day`). Windows end at that row's decision day;
  `lifetime` starts at the anchor. An age window past the first `at`, or a raw
  `_day_<N>` column, fails the compile (a row would read its future). Transforms read
  `decision_day` and `horizon_day` as numbers, so a value as of a fixed age is a
  `lifetime` sum of the ages it covers masked until reached:
  `where(decision_day >= 7, rev_le7, nan)` over `input: {formula: "where(age <= 7, total_val, nan)"}`.
- A label's window ends at the horizon; `since_decision` counts only the days after the
  decision day.
- Maturity follows the source's frontier (its latest timestamp read): a decision day it
  hasn't reached has no row; a horizon it hasn't reached, a NULL label.
- With a `Spine(start, end)`, each source read is bounded by constant timestamps, so
  BigQuery prunes partitions.

`python -m gcpbricks.feature_manager.migrate features.yaml --dbt-features DIR --view NAME`
moves a `define` spec to the blocks, keeping each entry's text and comments.

## Spec (`features.yaml`)

```yaml
vars:                        # constants: scalars read by name, lists for cols()/for_each
  days: [1, 3, 7, 10, 14]
  eps: 1.2341e-4
define:                      # runs top to bottom; reading a later name is an error
  - name: "{p}_growth_d{x}_d{y}_cum"
    expr: "({p}_day_{y}_cum - {p}_day_{x}_cum) / ({p}_day_{x}_cum + eps)"
    for_each: {p: [ads_rev, total_val], "x,y": {pairs: days}}

  - name: ratio_daily                        # a matrix: never an output itself
    expr: "cols('total_val_day_{d}', d=range(0, 8)) / maximum(cost, 1e-12)"
    keep: false
  - {name: log_ratio, expr: "log1p(row_sum(ratio_daily))", keep: false}
  - {name: log_ratio_med, fit: median, of: log_ratio, by: platform, keep: false}
  - {name: ratio_alpha, expr: "log_ratio - log_ratio_med"}

  - {name: clicks_bin, fit: quantile_bin, of: clicks, q: [0, 0.25, 0.5, 0.75, 1]}
  - {name: geo_te, fit: mean, of: "@target", by: geo}

  - name: siblings                          # what no expression states
    custom: "model:compute_sibling_features"
    inputs: [snapshot_date, product_id, campaign_id]
    outputs: [sibling_count_30d]
    batch_dependent: true

inputs:                      # the estimator's columns, by set name
  default: [+outputs, cost]
fetch: [is_demo_model]     # raw columns model.py itself reads
```

Layout: an entry that reads on one line stays one inline mapping (`- {name: …, expr: …}`),
so a spec scans as a list of feature names; one with a long `expr`, a `for_each` or
several fit parameters goes one key per line. Always quote an `expr` — `{`, `:`, `#` and
`*` mean something to YAML.

Every entry is exactly one of:

| Kind | Fields | Meaning |
|---|---|---|
| `expr` | `expr` | Row-wise expression over frame columns and earlier features. |
| `fit` | `fit: median \| mean \| iqr`, `of`, `by?`, `min_count?`, `default?`, `smoothing?` (mean), `missing_key?` | Statistic of `of` per `by` group (one column or a list), or overall. A group under `min_count` fit rows, or never seen, gets `default`: `overall` (the statistic over every row), `nan`, or `table_mean`. `smoothing: s` shrinks a group mean toward the overall one: (n·mean + s·overall)/(n + s). A row with a missing key gets `default` — or, with `missing_key: group`, is the group "nan" like any other. |
| `fit` | `fit: quantile`, `of`, `q` | One value: the q-quantile over every non-NaN cell of `of` — a column or a whole matrix. |
| `fit` | `fit: quantile_bin`, `of`, `q: [..]` | Values at quantiles `q` become bin edges, outermost widened to ±inf. Output is the bin number 1..k, NaN → 0. |
| `fit` | `fit: rank`, `of`, `by?`, `side?` (right), `missing_key?` | Share of the row's group's fit values below it (`left`) or at-or-below it (`right`) — a frozen ECDF; an unseen group ranks against all fit values. |
| `fit` | `fit: code`, `of: <column>` | Index of the value among the fit's sorted distinct values (any type); -1 for an unseen or missing one. |
| `fit` | `fit: yeo_johnson`, `of` | A Yeo-Johnson transform fitted on `of` (unstandardized). |
| `lookup` | `lookup: <column>`, `table: {value: number}`, `otherwise?` (NaN) | Stateless: the column's value, as text, mapped through `table` — e.g. a string id to a flag or a tier. |
| `custom` | `custom: "module:function"`, `outputs`, `inputs?`, `batch_dependent?` | `fn(frame) -> frame` with `outputs` on the same index. Stateless. |

A fit's `of` is an expression like `expr` (a bare name included), evaluated on the fit
frame; `of: "@target"` reads `y` instead. A per-column fit statistic (a "market vector")
is one fit per column with `for_each`, read back with `cols()`.

Shared fields: `name`, `keep` (false = intermediate, dropped from the output), `group`
(a family name `inputs` can take whole) and `for_each` (`name`/`expr`/`of`/`by`/`group`/
`inputs`/`outputs` are formatted with each combination; a key `"x,y"` unpacks pairs). A value is a list, a list var's name,
`{pairs: <list|var>}` or `{range: [start, stop]}`.

## Estimator inputs and the fetch

`inputs` names the ordered column lists an estimator reads; `model.py` asks for one with
`self.features_pipe.inputs("<set>")` (`default` when unnamed), from the fitted plan, so
a pickled model keeps the sets it trained with. An item is:

| Item | Meaning |
|---|---|
| `name` | A feature, or a raw column passed through as it is. |
| `+<set>` | Another set's columns, in place. `+outputs` is every kept feature, in declaration order. |
| `+group:<name>` | Every kept feature whose define has `group: <name>`, in declaration order. A group with a placeholder (`group: "pairs_{p}"`) splits a `for_each` entry into one group per value. |
| `{each: "<template>", <axis>: <values>, ...}` | The template formatted with every combination, first axis outermost — values as in `for_each`. |

A set is an ordered union: a column that two of its items both bring in keeps its first
position. A `keep: false` feature, an unknown set or group, or a cycle fails at compile. `fetch` lists the raw columns model code reads directly (row filters, segment
routing, a baseline).

A model's `feature_columns` — what training and serving SELECT — is derived: the raw
columns its defines read, the raw columns its `inputs` pass through, then `fetch`
(`fetch_columns(spec)`; the entity keys are always added by the read). A literal
`feature_columns` in `model.py` overrides it.

## Submodels

A model that trains several estimators on different rows declares them under
`submodels`, in fit order. Each has a row rule, its columns and its parameters:

```yaml
submodels:
  up:
    when: "signups >= 200"                 # an expression over frame columns and kept features
    params: {n_estimators: 2500, max_depth: 6}
    inputs: [+common, +group:value_ratios, cost]
  low:
    when: "(signups > 0) & (signups < 200)"
    params: {n_estimators: 2500, max_depth: 10}
    inputs: [+common, LTV_day_7_cum]
```

```python
owner = self.features_pipe.route(feat)      # each row's submodel name, None if no rule matches
for name in self.features_pipe.submodels():
    part = feat[owner == name]
    model = XGBRegressor(**self.features_pipe.params(name))
    model.fit(part[self.features_pipe.inputs(name)], ...)
```

`route()` raises on a row two rules match; what an unrouted row means (dropped, or an
error) is model.py's call. A submodel's `inputs` take the same items as an `inputs` set,
and it is one under its own name, so another submodel can reuse it (`+up`). A `when`
reads only kept features and raw columns, and its raw columns join the fetch.

## Hyperparameter search

The search space is not part of the spec: it is model.yaml's
`hyperparameter_tuning.search`, keyed by submodel name. A submodel's `params` here stay the
defaults, and each submodel is searched as its own study:

```yaml
# model.yaml
hyperparameter_tuning:
  search:
    up:                                     # a features.yaml submodel; absent = fixed
      learning_rate: {type: double, min: 0.01, max: 0.1, scale: log}
      max_depth: {type: integer, min: 3, max: 8}
```

Every searched key must be set in that submodel's `params`, checked at load. A type is
`double`, `integer` (`min`/`max`, optional `scale: log`) or `categorical` (`values`).
The sweep (`training/hyperopt/`) passes each trial's values as overrides on `params`;
`FeaturePipeline.params(name)` returns what the submodel trained with. Each version
logs those params to its Vertex AI Experiments run, and a retrain reuses `@champion`'s
until the declared space changes.

## Ops an `expr` may use

| Group | Ops |
|---|---|
| Arithmetic | `+ - * / ** %`, `< <= > >= == !=`, `&`, `\|`, `~`, constants `nan`, `inf` |
| Elementwise | `log log1p exp expm1 sqrt abs tanh isnan`, `fillna(x, v)`, `where(c, a, b)`, `maximum(a, b)`, `minimum(a, b)`, `clip(x, lo=, hi=)`, `float32(x)` (arithmetic on it stays float32), `float64(x)` |
| Build a matrix | `cols("prefix_day_{d}", d=<list var \| [..] \| range(a, b)>)` → N × k |
| Reduce a row | `row_sum row_mean row_max row_min`, `row_std(m, ddof=0)`, `row_gini(m, eps=1e-12)`, `row_argmax(m, last=0)`, `row_slope(m, x=, eps=0)`, `row_polyfit(m, deg=, k=, x=)` (coefficient of x^k), `row_polyfit_r2(m, deg=, x=, eps=1e-12)` |
| Map a row | `row_detrend(m, x=)` (residuals of its least-squares line), `row_diff(m)` (k-1 day-over-day differences), `col_slice(m, start=, stop=)` |

A vector and a matrix combine row by row (`forecast - ref` subtracts each row's own `ref`).
Curve fits take x as a keyword (a list or a list var, e.g. `x=days`); without it, x is the
column position 0..k-1. `row_argmax` on a boolean matrix finds the first (`last=1`: the
last) True. A datetime column reads as days since 1970-01-01, so `(snapshot_date + 3) % 7`
is the weekday (Monday = 0).

Anything else — attribute access, subscripts, unknown calls — fails at compile. Division
by zero and `log(0)` follow IEEE (`inf`, `-inf`, `nan`); guard with `maximum`/`where`.

`tests/test_feature_manager.py` holds worked specs, from a few features to a larger one with a `custom` step.

## Guarantees

- **Batch independence.** Every op works within one row and every `fit` step only looks
  up frozen values, so a segment scores the same whichever chunk it is served in. Only a
  `custom` step can break this, and it must say so (`batch_dependent: true`).
- **Point-in-time safe, if pickled by value.** A project that pickles the fitted pipeline
  with `cloudpickle.register_pickle_by_value(gcpbricks.feature_manager)` serves an
  artifact without importing this package, so a backfill routed to an old version runs
  that version's features whatever release is installed.
- **Not across releases when fitting.** A model fitted on a newer release runs that
  release's engine. A project on `mlops_version: latest` holds its features to golden
  fixtures (the features its specs produce on fixed synthetic frames) and checks them
  before each retrain, so a release that moves a model's features fails that retrain
  instead of training on them; demo_model's `ci/feature_golden.py` does this.

## Catching a bad spec before it trains

- **Compile** (`compile_spec(spec)`, or `FeaturePipeline(spec).plan()`) rejects anything
  wrong in the spec alone: an op outside the whitelist, wrong arguments, a matrix output,
  a read of a later feature, a parameter its `fit` stat does not take. The plan lists
  `raw_columns` (what the input frame must carry), `numeric_columns` (the ones it
  computes with), `outputs` and `intermediates`.
- **Schema** (`plan.schema_problems(plan, {column: BigQuery type})`, e.g. demo_model's
  preflight on every MR) checks the plan against the live feature table: every raw column must exist,
  and every numeric column must have a numeric, BOOL or date type — a STRING used in
  arithmetic fails there, named with the feature that does it. Group keys and custom
  inputs may be any type. It needs only numpy, not the model's ML stack.
