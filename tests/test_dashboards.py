"""A dashboard's spec and how a data widget reduces its rows (gcpbricks.dashboards)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from gcpbricks.dashboards import DashboardSpec, Widget, aggregate, window_delta

RUNS = [
    {"job": "retrain", "state": "SUCCEEDED", "start": "2026-10-01T02:00:00Z", "duration_min": 30.0},
    {"job": "retrain", "state": "FAILED", "start": "2026-10-02T02:00:00Z", "duration_min": 5.0},
    {"job": "serve", "state": "SUCCEEDED", "start": "2026-10-02T06:00:00Z", "duration_min": 10.0},
    {"job": "serve", "state": "SUCCEEDED", "start": "2026-10-09T06:00:00Z", "duration_min": 14.0},
]
SINCE = datetime(2026, 9, 1, tzinfo=UTC)


def _w(**kw) -> Widget:
    return Widget(title="t", **{"type": "table", "source": "runs", **kw})


def test_count_with_a_filter_is_one_number():
    assert aggregate(RUNS, _w(type="counter", filter={"state": "FAILED"}), "start", SINCE) == {
        "columns": ["count"], "rows": [[1]]}


def test_a_filter_takes_a_list_of_values():
    out = aggregate(RUNS, _w(type="counter", filter={"job": ["serve", "nope"]}), "start", SINCE)
    assert out["rows"] == [[3 - 1]]


def test_a_measure_grouped_by_a_field():
    out = aggregate(RUNS, _w(measure="avg(duration_min)", by="job", sort="desc"), "start", SINCE)
    assert out == {"columns": ["job", "avg(duration_min)"], "rows": [["retrain", 17.5], ["serve", 12.0]]}


def test_a_bin_groups_by_day_or_week_oldest_first():
    day = aggregate(RUNS, _w(type="line", measure="max(duration_min)", bin="1d"), "start", SINCE)
    assert day["rows"] == [["2026-10-01", 30.0], ["2026-10-02", 10.0], ["2026-10-09", 14.0]]
    week = aggregate(RUNS, _w(type="bar", measure="count", bin="1w", by="job"), "start", SINCE)
    assert week["columns"] == ["bin", "job", "count"]
    assert week["rows"] == [["2026-09-28", "retrain", 2], ["2026-09-28", "serve", 1], ["2026-10-05", "serve", 1]]


def test_the_window_drops_older_rows_and_last_is_the_newest():
    out = aggregate(RUNS, _w(measure="last(duration_min)", by="job"), "start",
                    datetime(2026, 10, 2, tzinfo=UTC))
    assert sorted(out["rows"]) == [["retrain", 5.0], ["serve", 14.0]]


def test_p95_and_limit():
    out = aggregate(RUNS, _w(measure="p95(duration_min)", by="job", sort="desc", limit=1), "start", SINCE)
    assert out["rows"][0][0] == "retrain" and 5.0 < out["rows"][0][1] <= 30.0


def test_an_empty_counter_still_has_its_one_cell():
    assert aggregate([], _w(type="counter"), "start", SINCE)["rows"] == [[0]]
    assert aggregate([], _w(type="counter", measure="avg(duration_min)"), "start", SINCE)["rows"] == [[None]]


@pytest.mark.parametrize("widget, message", [
    ({"title": "x", "type": "counter"}, "one source or one dataset"),
    ({"title": "x", "type": "counter", "source": "runs", "dataset": "d"}, "one source or one dataset"),
    ({"title": "x", "type": "logs", "source": "runs", "query": "q"}, "no source or dataset"),
    ({"title": "x", "type": "table", "source": "runs", "measure": "median(x)"}, "measure"),
    ({"title": "x", "type": "line", "source": "runs", "by": "job"}, "give it a bin"),
    ({"title": "x", "type": "bar", "source": "runs"}, "a bin, a by"),
    ({"title": "x", "type": "logs"}, "needs a query"),
    ({"title": "x", "type": "text"}, "needs its text"),
    ({"title": "x", "type": "counter", "source": "runs", "window": "3 days"}, "window"),
    ({"title": "x", "type": "table", "source": "runs", "by": "model"}, "has no field"),
    ({"title": "x", "type": "table", "dataset": "nope"}, "no dataset"),
])
def test_a_bad_widget_is_refused(widget, message):
    with pytest.raises(ValidationError, match=message):
        DashboardSpec.model_validate({"widgets": [widget]})


def test_a_dataset_needs_a_time_column_to_bin():
    spec = {"datasets": {"acc": {"bigquery": "SELECT 1"}},
            "widgets": [{"title": "x", "type": "line", "dataset": "acc", "measure": "avg(mape)", "bin": "1d"}]}
    with pytest.raises(ValidationError, match="no time column"):
        DashboardSpec.model_validate(spec)
    spec["datasets"]["acc"]["time"] = "snapshot_date"
    assert DashboardSpec.model_validate(spec).widgets[0].field == "mape"


def test_windows():
    assert window_delta("24h").total_seconds() == 86400
    assert window_delta("2w").days == 14
