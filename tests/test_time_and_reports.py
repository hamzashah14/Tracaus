from datetime import datetime, timezone

import pytest

from tests.helpers import load_lambda
from tracaus.time import parse_utc

logs = load_lambda("fetch_logs")
metrics = load_lambda("fetch_metrics")


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-09-24T15:00:00+05:00",
        "2026-09-24T050000-05:00".replace("050000", "05:00:00"),
        "2026-09-24T10:00:00Z",
        "2026-09-24 10:00:00",
        "2026-09-24T15:00:00+0500",
    ],
)
def test_equivalent_times(stamp):
    expected = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)
    assert parse_utc(stamp) == logs.parse_time(stamp) == expected
    start, end = metrics.resolve_window({"time_string": stamp}, datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert start + (end - start) / 2 == expected


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-09-24",
        "garbage",
        "2026-09-24T15:00:00+24:00",
        "2026-09-24T15:00:00+00:60",
        "2026-09-24 10:00:00injected",
        None,
    ],
)
def test_invalid_times_rejected(stamp):
    with pytest.raises(ValueError):
        parse_utc(stamp)
