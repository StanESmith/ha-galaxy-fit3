"""The client's running health state: the day's steps, calories and distance, and the resting heart rate."""

from __future__ import annotations

import importlib
import pathlib
import sys
import types

_PKG_DIR = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "galaxy_fit3"


def _load():
    sys.modules.setdefault("bleak", types.SimpleNamespace(BleakClient=object))
    pkg = types.ModuleType("gf3h")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["gf3h"] = pkg
    return importlib.import_module("gf3h.client"), importlib.import_module("gf3h.sap.healthsync")


client_mod, hs = _load()

MIDNIGHT = 1_800_000_000


def _sync(steps=(), heart_rates=()):
    return hs.SyncData(op=hs.OP_SYNC_DATA, steps=list(steps), heart_rates=list(heart_rates))


def _step(start, count=10, calorie=1.5, distance=8.0):
    return hs.StepRecord(start=start, count=count, calorie=calorie, distance=distance)


def test_calories_and_distance_are_summed_from_midnight_like_steps() -> None:
    state = client_mod.HealthState()
    state.apply(_sync([_step(MIDNIGHT - 600, 5, 2.0, 3.0), _step(MIDNIGHT + 60, 10, 1.5, 8.0), _step(MIDNIGHT + 660, 20, 3.0, 16.0)]))
    assert state.steps_since(MIDNIGHT) == 30
    assert state.calories_since(MIDNIGHT) == 4.5
    assert state.distance_since(MIDNIGHT) == 24.0


def test_nothing_is_reported_before_any_record_arrives() -> None:
    state = client_mod.HealthState()
    assert state.calories_since(MIDNIGHT) is None
    assert state.distance_since(MIDNIGHT) is None


def test_records_without_calories_or_distance_leave_them_unknown() -> None:
    state = client_mod.HealthState()
    state.apply(_sync([hs.StepRecord(start=MIDNIGHT + 60, count=10)]))
    assert state.steps_since(MIDNIGHT) == 10
    assert state.calories_since(MIDNIGHT) is None and state.distance_since(MIDNIGHT) is None


def test_a_record_the_watch_sends_again_replaces_the_old_one() -> None:
    state = client_mod.HealthState()
    state.apply(_sync([_step(MIDNIGHT + 60, 10, 1.0, 5.0)]))
    state.apply(_sync([_step(MIDNIGHT + 60, 12, 1.4, 6.5)]))
    assert (state.steps_since(MIDNIGHT), state.calories_since(MIDNIGHT), state.distance_since(MIDNIGHT)) == (12, 1.4, 6.5)


def test_old_records_are_dropped_together_when_the_history_grows_long() -> None:
    state = client_mod.HealthState()
    state.apply(_sync([_step(MIDNIGHT + n * 600) for n in range(4200)]))
    assert len(state.step_bins) <= 2048 + 4200 - 4096 or len(state.step_bins) < 4200
    assert set(state.step_calories) == set(state.step_bins) == set(state.step_distance)


def test_the_resting_heart_rate_is_the_newest_record_tagged_as_resting() -> None:
    state = client_mod.HealthState()
    assert state.resting_heart_rate is None
    state.apply(_sync(heart_rates=[
        hs.HeartRateRecord(start=MIDNIGHT, bpm=72, tag=20000),  # an ordinary reading
        hs.HeartRateRecord(start=MIDNIGHT + 100, bpm=58, tag=hs.TAG_RESTING_HEART_RATE),
        hs.HeartRateRecord(start=MIDNIGHT + 50, bpm=61, tag=hs.TAG_RESTING_HEART_RATE),  # older, arrives later
    ]))
    assert state.resting_heart_rate == 58
    assert state.heart_rate is not None  # the ordinary reading path is unchanged


def test_the_resting_tag_is_the_documented_value() -> None:
    assert hs.TAG_RESTING_HEART_RATE == 21316
