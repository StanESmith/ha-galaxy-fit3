"""Heart-rate history: the on-disk format and the hourly roll-ups fed to Home Assistant statistics.

Home Assistant has no way to back-date a sensor state, so the watch's stored readings (which arrive in
batches, hours after they were taken) are kept here as raw records and imported as external statistics.
Pure Python, no Home Assistant imports, so it is tested directly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# start (Unix seconds) -> (bpm, bpm_min, bpm_max, tag); min/max/tag are None when the watch gave none
Record = tuple[int, int | None, int | None, int | None]

STORE_VERSION = 1

# The watch stamps records taken before its clock was first set with a default date (seen: 2021-12-01),
# and the Fit3 did not exist before 2024, so anything earlier is not a real reading.
MIN_RECORD_TIME = 1_704_067_200  # 2024-01-01 00:00 UTC


def is_plausible(start: int) -> bool:
    """Whether a record's start time can be a real reading (not the watch's pre-clock default date)."""
    return start >= MIN_RECORD_TIME


def drop_implausible(records: dict[int, Record]) -> int:
    """Remove records that fail :func:`is_plausible` in place; returns how many were removed."""
    bad = [start for start in records if not is_plausible(start)]
    for start in bad:
        del records[start]
    return len(bad)


def hour_of(unix_seconds: int) -> int:
    """The Unix time of the top of the hour containing ``unix_seconds`` (statistics are hourly)."""
    return unix_seconds - unix_seconds % 3600


def to_store(records: Mapping[int, Record]) -> dict:
    """JSON-serialisable form, oldest record first."""
    return {"version": STORE_VERSION, "records": [[start, *rec] for start, rec in sorted(records.items())]}


def from_store(data: object) -> dict[int, Record]:
    """Parse :func:`to_store` output; anything malformed is skipped rather than raised."""
    out: dict[int, Record] = {}
    rows = data.get("records", []) if isinstance(data, dict) else []
    for row in rows:
        try:
            start, bpm, lo, hi, tag = row
        except (TypeError, ValueError):
            continue
        if isinstance(start, int) and isinstance(bpm, int) and bpm > 0:
            out[start] = (bpm, lo, hi, tag)
    return out


def hourly_stats(records: Mapping[int, Record], hours: Iterable[int]) -> list[tuple[int, float, float, float]]:
    """``(hour_start, mean, min, max)`` for each requested hour that has at least one record.

    The mean is over the records' bpm values; the min and max also take in each record's own
    min/max, since one record can summarise a stretch of readings.
    """
    wanted = set(hours)
    sums: dict[int, list[int]] = {}  # hour -> [count, total, lowest, highest]
    for start, (bpm, lo, hi, _tag) in records.items():
        hour = hour_of(start)
        if hour not in wanted:
            continue
        low, high = min(bpm, lo or bpm), max(bpm, hi or bpm)
        acc = sums.get(hour)
        if acc is None:
            sums[hour] = [1, bpm, low, high]
        else:
            acc[0] += 1
            acc[1] += bpm
            acc[2] = min(acc[2], low)
            acc[3] = max(acc[3], high)
    return [(hour, acc[1] / acc[0], float(acc[2]), float(acc[3])) for hour, acc in sorted(sums.items())]
