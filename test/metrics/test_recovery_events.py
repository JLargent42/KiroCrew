"""Metric vocabulary and best-effort histogram primitive, without samplers."""

from unittest.mock import Mock

import pytest

from kiro_crew.metrics import events as ev


def test_histogram_forwards_value_unit_and_closed_attrs(monkeypatch):
    recorder = Mock()
    monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: recorder)
    ev.emit_histogram(ev.RECOVERY_DURATION_SECS, 12.5, {"layer": "L1_tool_call"}, unit="s")
    recorder.histogram.assert_called_once_with(
        ev.RECOVERY_DURATION_SECS, 12.5, unit="s", attrs={"layer": "L1_tool_call"}
    )
    ev.emit_histogram(ev.TASKQ_DEPTH, 3, {"state": "queued"})
    recorder.histogram.assert_called_with(ev.TASKQ_DEPTH, 3, unit="1", attrs={"state": "queued"})


@pytest.mark.parametrize("where", ["provider", "recorder"])
def test_histogram_never_raises(monkeypatch, where):
    recorder = Mock()
    provider = Mock(return_value=recorder)
    target = provider if where == "provider" else recorder.histogram
    target.side_effect = RuntimeError("telemetry unavailable")
    monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", provider)
    ev.emit_histogram(ev.RECOVERY_DURATION_SECS, 2, {}, unit="s")


def test_metric_vocabulary_is_complete_unique_and_namespaced():
    names = (
        "TASKQ_DEPTH",
        "TASKQ_OLDEST_WAIT_SECS",
        "TASKQ_COMPLETIONS",
        "TASKQ_EFFECTIVE_CAP",
        "TASKQ_PRESSURE_REASON",
        "HOST_PROCS_PEAK",
        "HOST_FDS_PEAK",
        "HOST_RSS_PEAK_MB",
        "LOOP_LAG_MS",
        "RECOVERY_DURATION_SECS",
        "RECOVERY_ATTEMPTS",
        "RECOVERY_ESCALATIONS",
        "RESTARTS_TOTAL",
        "ADAPTIVE_DECISIONS",
    )
    values = [getattr(ev, name) for name in names]
    assert len(values) == len(set(values)) == 14
    assert all(value.startswith("kirocrew.") for value in values)
    assert ev.RECOVERY_DURATION_SECS == "kirocrew.recovery.duration_secs"
