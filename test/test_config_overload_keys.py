"""Stage-local scalar config contracts; final overload inventory belongs to layer 7."""

import dataclasses
import json
import re
from unittest.mock import patch

import pytest

from kiro_crew.config import sections
from kiro_crew.config.loader import KiroCrewConfig

_CLAMP_RE = re.compile(r"[Cc]lamped to (-?\d+(?:\.\d+)?)\s*(?:s)?\.\.\s*(-?\d+(?:\.\d+)?)")
# Existing platform/sentinel/derived fields do not have scalar round-trip semantics.
_TRANSFORMED = frozenset(
    {
        "sandbox_allow_unsandboxed_exec",
        "dangerously_skip_permissions",
        "subagent_timeout_secs",
        "yolo_duration",
        "max_subagents",
        "acp_backend",
        "member_acp_backend",
        "fallback_model",
        "jail",
        "log_level",
        "bot_name",
        "completion_keep",
        "reasoning_effort",
        "stub_servers",
        "stub_overrides",
        "socket_path",
        "overlay_dir",
    }
)
_FLOOR_COMPANIONS = {
    "spawn_concurrency_max": {"spawn_concurrency_min": 1},
    "spawn_concurrency_initial": {"spawn_concurrency_min": 1, "spawn_concurrency_max": 64},
}


def _loaded(tmp_path, data):
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
        return KiroCrewConfig.load()


def _scalar_fields(cls):
    return [
        f
        for f in dataclasses.fields(cls)
        if not f.name.startswith("_")
        and f.name not in _TRANSFORMED
        and isinstance(f.default, (bool, int, float, str))
    ]


_SECTIONS = (("agent", sections.AgentConfig), ("mcp_gateway", sections.McpGatewayConfig))
CASES = [(section, f) for section, cls in _SECTIONS for f in _scalar_fields(cls)]


def _ids(value):
    return value.name if isinstance(value, dataclasses.Field) else str(value)


def _clamp(field):
    match = _CLAMP_RE.search(str(field.metadata.get("help", "")))
    return (float(match[1]), float(match[2])) if match else None


_CLAMPED = [
    (s, f)
    for s, f in CASES
    if isinstance(f.default, (int, float))
    and not isinstance(f.default, bool)
    and _clamp(f) is not None
]


@pytest.mark.parametrize("section,field", CASES, ids=_ids)
def test_absent_key_loads_the_declared_default(section, field, tmp_path):
    cfg = _loaded(tmp_path, {section: {}})
    assert getattr(getattr(cfg, section), field.name) == field.default


@pytest.mark.parametrize("section,field", CASES, ids=_ids)
def test_explicit_default_round_trips(section, field, tmp_path):
    cfg = _loaded(tmp_path, {section: {field.name: field.default}})
    assert getattr(getattr(cfg, section), field.name) == field.default


@pytest.mark.parametrize(
    "section,field", [(s, f) for s, f in CASES if isinstance(f.default, bool)], ids=_ids
)
def test_a_bool_key_flips(section, field, tmp_path):
    flipped = not field.default
    cfg = _loaded(tmp_path, {section: {field.name: flipped}})
    assert getattr(getattr(cfg, section), field.name) is flipped


@pytest.mark.parametrize(
    "section,field",
    [(s, f) for s, f in CASES if isinstance(f.default, str) and f.metadata.get("enum")],
    ids=_ids,
)
def test_an_enum_key_accepts_every_listed_value(section, field, tmp_path):
    for value in field.metadata["enum"]:
        cfg = _loaded(tmp_path, {section: {field.name: value}})
        assert getattr(getattr(cfg, section), field.name) == value


@pytest.mark.parametrize("section,field", _CLAMPED, ids=_ids)
def test_a_declared_clamp_is_the_loader_clamp(section, field, tmp_path):
    lo, hi = _clamp(field)
    kind = type(field.default)
    companions = _FLOOR_COMPANIONS.get(field.name, {})
    for value in (kind(lo), kind(hi), kind((lo + hi) / 2)):
        cfg = _loaded(tmp_path, {section: {**companions, field.name: value}})
        assert getattr(getattr(cfg, section), field.name) == pytest.approx(value)
    cfg = _loaded(tmp_path, {section: {field.name: kind(hi * 1000 + 12345)}})
    assert getattr(getattr(cfg, section), field.name) <= hi
    cfg = _loaded(tmp_path, {section: {**companions, field.name: kind(lo - 1000)}})
    assert getattr(getattr(cfg, section), field.name) >= lo


def test_stage_inventory_is_real_and_recovery_knobs_are_not_exposed():
    names = {(section, field.name) for section, field in CASES}
    assert len(CASES) == 48
    assert ("agent", "conductor_skill") not in names
    assert {
        ("agent", "admission_gate"),
        ("agent", "session_start_timeout_secs"),
        ("agent", "subagent_stall_idle_secs"),
        ("mcp_gateway", "max_backends"),
    } <= names
    assert {field.name for _, field in _CLAMPED} == {
        "workflow_run_timeout_secs",
        "chat_turn_timeout_secs",
        "tool_approval_timeout_secs",
    }
    agent_fields = {field.name for field in dataclasses.fields(sections.AgentConfig)}
    assert not {"recovery_backoff_base_secs", "recovery_backoff_max_secs"} & agent_fields


def test_workflow_run_timeout_secs_clamp_is_enforced_at_the_loader(tmp_path):
    """The declared 60..21600 bound is the actual load-path clamp, not a future fix."""
    cfg = _loaded(tmp_path, {"agent": {"workflow_run_timeout_secs": 21612345}})
    assert cfg.agent.workflow_run_timeout_secs == 21600
    cfg = _loaded(tmp_path, {"agent": {"workflow_run_timeout_secs": 1}})
    assert cfg.agent.workflow_run_timeout_secs == 60
