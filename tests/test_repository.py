from types import SimpleNamespace

import pytest

from app.infra.db import repository
from app.infra.settings import Settings


class _Result:
    def __init__(self, value, many=False):
        self.value = value
        self.many = many

    def fetchone(self):
        return self.value

    def fetchall(self):
        return self.value


class _FakeDb:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, *args, **kwargs):
        return self.calls.pop(0)


def _make_fake_db(search_row=(100, 100, 100, 100)):
    return _FakeDb(
        [
            _Result(search_row),
            _Result((0, 0, 0, 0)),
            _Result((0, 0, 0, 0)),
            _Result([], many=True),
        ]
    )


def _threshold_stub(thresholds):
    return lambda: SimpleNamespace(conversion_thresholds=thresholds)


def test_conversion_thresholds_default(monkeypatch):
    monkeypatch.setattr(
        repository,
        "get_settings",
        _threshold_stub({1: 15, 7: 50, 30: 150, 365: 500}),
    )
    stats = repository.AgentRepository().get_multi_timeframe_stats(_make_fake_db(), 1)

    for days in (1, 7, 30, 365):
        assert stats[days]["effective_searches"] == 100
        assert stats[days]["low_confidence"] is False


@pytest.mark.parametrize(
    "thresholds,expected_365_low_confidence",
    [
        ({1: 15, 7: 50, 30: 150, 365: 200}, False),
        ({1: 15, 7: 50, 30: 150, 365: 600}, True),
    ],
)
def test_conversion_thresholds_override_365(monkeypatch, thresholds, expected_365_low_confidence):
    monkeypatch.setattr(repository, "get_settings", _threshold_stub(thresholds))
    stats = repository.AgentRepository().get_multi_timeframe_stats(_make_fake_db(), 1)

    for days in (1, 7, 30):
        assert stats[days]["low_confidence"] is False
    assert stats[365]["low_confidence"] is expected_365_low_confidence


def test_conversion_thresholds_validation_empty():
    settings = Settings(_env_file=None)
    settings.conversion_thresholds = {}

    with pytest.raises(RuntimeError, match="conversion_thresholds must be non-empty"):
        settings._validate_startup()


def test_conversion_thresholds_validation_below_min():
    settings = Settings(_env_file=None)
    settings.conversion_thresholds = {1: 0}

    with pytest.raises(RuntimeError, match="conversion_thresholds values must be at least 1"):
        settings._validate_startup()