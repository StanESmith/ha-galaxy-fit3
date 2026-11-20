"""Heart-rate history: storage round trip and hourly roll-ups."""

from __future__ import annotations

import heart_rate_history as hrh

H = 1_700_000_000 - 1_700_000_000 % 3600  # a top of the hour


def test_store_round_trip_keeps_every_record_in_order() -> None:
    records = {H + 600: (72, 70, 75, None), H: (65, None, None, 21316)}
    data = hrh.to_store(records)
    assert [row[0] for row in data["records"]] == [H, H + 600]
    assert hrh.from_store(data) == records


def test_from_store_skips_malformed_rows_and_missing_data() -> None:
    assert hrh.from_store(None) == {}
    assert hrh.from_store({"records": [[H, 70, None, None, None], "junk", [H + 1, 0, None, None, None], [1, 2]]}) == {
        H: (70, None, None, None)
    }


def test_hourly_stats_mean_min_max_per_requested_hour_only() -> None:
    records = {
        H + 60: (60, None, None, None),
        H + 1800: (80, 78, 90, None),  # a record's own min and max widen the hour's range
        H + 3600 + 5: (100, None, None, None),
        H + 7200: (55, None, None, None),  # not requested
    }
    rows = hrh.hourly_stats(records, [H, H + 3600])
    assert rows == [(H, 70.0, 60.0, 90.0), (H + 3600, 100.0, 100.0, 100.0)]


def test_hourly_stats_ignores_hours_without_records() -> None:
    assert hrh.hourly_stats({H: (70, None, None, None)}, [H + 3600]) == []
    assert hrh.hour_of(H + 3599) == H


def test_implausible_dates_are_dropped_in_place() -> None:
    real = 1_794_000_000  # November 2026
    records = {1_638_332_408: (66, None, None, None), hrh.MIN_RECORD_TIME: (70, None, None, None), real: (72, None, None, None)}
    assert hrh.drop_implausible(records) == 1
    assert sorted(records) == [hrh.MIN_RECORD_TIME, real]
    assert hrh.is_plausible(real) and not hrh.is_plausible(1_638_332_408)
