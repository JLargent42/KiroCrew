"""Unit and integration tests for scripts/check_connector_manifest.py.

Two layers, matching the gate's own two jobs:

- **Negative/structural tests** (`TestFieldRules`, `TestCrossFileConsistency`)
  exercise `validate_entry` directly against synthetic dicts, so a single bad
  field is pinned without needing a real file on disk. This is where a
  schema-violating input is proven to actually fail, not just assumed to.
- **Fixture/integration tests** (`TestFixtureEntries`) run the gate's own
  `--test`/scan entry points against the two real fixture entries this PR
  ships under `docs/system-specs/connector-manifest/entries/github/`, so the
  documented examples are proven to stay valid as the schema evolves —
  a fixture that silently rotted would defeat the point of shipping one.

The self-test suite inside the script itself (`check_connector_manifest.py
--test`) is the authoritative probe-by-probe pin; this file additionally
runs it as a subprocess so a CI failure surfaces in pytest's own summary
rather than only in a separate gate step.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(_REPO_ROOT, "scripts", "check_connector_manifest.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_connector_manifest", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_connector_manifest"] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


def _entry(**overrides):
    return gate._minimal_planned_entry(**overrides)


_REF = gate._MATCHING_REF


# ---------------------------------------------------------------------------
# Field-level rules, each pinned independently of the script's own self-test
# (belt-and-suspenders: a regression that breaks the self-test's own
# assertions AND this file's independent expectations is caught twice).
# ---------------------------------------------------------------------------


class TestFieldRules:
    def test_minimal_planned_entry_is_valid(self):
        result = gate.validate_entry(_entry(), _REF)
        assert result.ok, result.findings

    @pytest.mark.parametrize(
        "field_name",
        [
            "operation_id",
            "operation_kind",
            "provider",
            "service_id",
            "category",
            "source_status",
            "source",
            "effect",
            "policy",
            "adapter",
            "status",
            "last_reached_status",
        ],
    )
    def test_missing_required_field_fails(self, field_name):
        entry = _entry()
        del entry[field_name]
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == field_name for f in result.findings)

    def test_unknown_service_id_rejected(self):
        entry = _entry(service_id="not_a_real_service")
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_unknown_category_rejected(self):
        entry = _entry(category="not_a_real_category")
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_pagination_required_for_list_and_search(self):
        for kind in ("list", "search"):
            entry = _entry(operation_kind=kind, pagination=None)
            result = gate.validate_entry(entry, _REF)
            assert not result.ok, f"operation_kind={kind} without pagination should fail"

    def test_pagination_not_required_for_single_fetch(self):
        entry = _entry(operation_kind="single_fetch", pagination=None)
        result = gate.validate_entry(entry, _REF)
        assert result.ok, result.findings

    @pytest.mark.parametrize("effect", ["write", "delete", "share", "external_send", "admin"])
    def test_retry_required_for_write_shaped_effects(self, effect):
        entry = _entry(effect=effect, retry=None)
        result = gate.validate_entry(entry, _REF)
        assert not result.ok, f"effect={effect} without retry should fail"

    @pytest.mark.parametrize("effect", ["read", "billable"])
    def test_retry_not_required_for_read_or_billable(self, effect):
        entry = _entry(effect=effect, retry=None)
        result = gate.validate_entry(entry, _REF)
        assert result.ok, result.findings

    def test_retry_idempotency_class_enum_enforced(self):
        entry = _entry(effect="write", retry={"idempotency_class": "bogus", "detail": "x"})
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    @pytest.mark.parametrize(
        "source_kind,snapshot_ref,expect_ok",
        [
            ("official_docs", "https://docs.github.com/en/rest", True),
            ("official_docs", "not-a-url", False),
            ("format_spec", "https://www.rfc-editor.org/rfc/rfc4180", True),
            ("format_spec", "AGENTS.md", False),
            ("repo_path", "AGENTS.md", True),
            ("repo_path", "docs/does/not/exist.md", False),
            ("repo_path", "/etc/passwd", False),
            ("user_stated", "AGENTS.md", True),
            ("search_snippet_corroborated", "AGENTS.md", True),
        ],
    )
    def test_snapshot_ref_resolution_contract(self, source_kind, snapshot_ref, expect_ok):
        entry = _entry(
            source={
                "source_kind": source_kind,
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": snapshot_ref,
            }
        )
        result = gate.validate_entry(entry, _REF)
        assert result.ok is expect_ok, result.findings

    def test_snapshot_ref_rejects_absolute_path_outside_repo(self):
        """The exact regression the S1 orphaned finding was about: a
        snapshot_ref pointing into the campaign's private, out-of-repo
        workspace must not validate."""
        entry = _entry(
            source={
                "source_kind": "search_snippet_corroborated",
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": "/mnt/external-campaign-workspace/catalog-evidence.json",
            }
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_snapshot_ref_rejects_relative_traversal_outside_repo(self):
        """A relative-looking
        repo_path snapshot_ref that traverses out of the repo via '..'
        segments (e.g. '../../../../etc/passwd') is NOT caught by the
        isabs() guard alone, because os.path.join + os.path.exists happily
        resolves '..' through the real filesystem. The fix requires the
        REALPATH to stay under the repo root, checked via os.path.commonpath
        — not just that the string itself lacks a leading '/'."""
        entry = _entry(
            source={
                "source_kind": "repo_path",
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": "../../../../../../../etc/passwd",
            }
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok, "a relative path that resolves outside the repo root must be rejected"

    @pytest.mark.parametrize(
        "field_name,bad_value",
        [
            ("category", []),
            ("category", {}),
            ("source_status", []),
            ("effect", []),
            ("operation_kind", []),
        ],
    )
    def test_enum_fields_never_crash_on_unhashable_json_types(self, field_name, bad_value):
        """`value not in allowed` on a frozenset raises
        TypeError when value is a list/dict (unhashable), instead of cleanly
        failing validation. A malformed manifest entry must produce a
        Finding, never crash the whole gate."""
        entry = _entry(**{field_name: bad_value})
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_source_kind_unhashable_type_never_crashes(self):
        entry = _entry(
            source={
                "source_kind": [],
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": "x",
            }
        )
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_retry_idempotency_class_unhashable_type_never_crashes(self):
        entry = _entry(effect="write", retry={"idempotency_class": [], "detail": "x"})
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_status_unhashable_type_never_crashes(self):
        entry = _entry(status=[], last_reached_status=[])
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_operation_kind_unhashable_type_never_crashes_pagination_check(self):
        entry = _entry(operation_kind=[])
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_evidence_matrix_row_status_unhashable_type_never_crashes(self):
        entry = _entry(
            status="implementing",
            last_reached_status="implementing",
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": None,
                    "status": [],
                    "last_reached_status": [],
                }
            ],
        )
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_not_yet_sourced_placeholder_is_exempt_from_existence_check(self):
        entry = _entry()  # default source is not_yet_sourced with a placeholder
        result = gate.validate_entry(entry, _REF)
        assert result.ok, result.findings

    @pytest.mark.parametrize(
        "evidence_path",
        [
            "docs/system-specs/connector-manifest/campaign-evidence/catalog-evidence.json",
            "docs/system-specs/connector-manifest/campaign-evidence/contract-and-dag.md",
        ],
    )
    def test_evidence_catalog_artifacts_are_resolvable_in_repo(self, evidence_path):
        """The evidence catalog must have a
        named, in-repo home that a reader with only this repo can open.
        These paths point at the committed copies, not the private
        campaign workspace. code-audit.json is deliberately excluded from
        this list — it is not mirrored (zero consumers in this repo's own
        validator/tests/spec; see campaign-evidence/README.md)."""
        entry = _entry(
            source={
                "source_kind": "repo_path",
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": evidence_path,
            }
        )
        result = gate.validate_entry(entry, _REF)
        assert result.ok, result.findings

    def test_code_audit_json_is_deliberately_not_mirrored(self):
        """Pins the deletion decision: code-audit.json was removed as a
        zero-consumer artifact — a
        snapshot_ref citing it must fail, not silently resolve, so a future
        re-add of the file without updating this test is caught."""
        entry = _entry(
            source={
                "source_kind": "repo_path",
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": "docs/system-specs/connector-manifest/campaign-evidence/code-audit.json",
            }
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    @pytest.mark.parametrize(
        "schema_ref,expect_ok",
        [
            ("https://docs.github.com/en/rest/issues", True),
            ("not-a-url-and-not-in-repo.md", False),
            ("AGENTS.md", True),
            ("AGENTS.md#some-fragment", True),
            ("docs/does/not/exist.md", False),
            ("/etc/passwd", False),
            ("../../../../../../../etc/passwd", False),
        ],
    )
    def test_schema_ref_resolution_contract(self, schema_ref, expect_ok):
        """schema_ref was only checked for presence,
        never resolvability, unlike snapshot_ref — the same defect class one
        field over. Applies the identical https://-or-in-repo-path rule,
        including the realpath/commonpath containment check (the same fix
        as the F1 path-traversal finding)."""
        entry = _entry(
            input_schema={"schema_ref": schema_ref, "schema_version": "1"},
        )
        result = gate.validate_entry(entry, _REF)
        assert result.ok is expect_ok, result.findings

    def test_code_complete_requires_verification_contract_and_tested_sha(self):
        entry = _entry(status="code_complete", last_reached_status="code_complete")
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_merged_requires_merged_sha(self):
        entry = _entry(
            status="merged",
            last_reached_status="merged",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "r1", "receipt_ref": "e1"},
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == "status" for f in result.findings)

    def test_blocked_requires_populated_blocker(self):
        entry = _entry(status="blocked", last_reached_status="implementing", blocker=None)
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_last_reached_status_can_never_be_blocked(self):
        entry = _entry(status="blocked", last_reached_status="blocked")
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_last_reached_status_must_equal_status_when_not_blocked(self):
        entry = _entry(status="implementing", last_reached_status="planned")
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_evidence_matrix_must_be_total_once_status_leaves_planned(self):
        entry = _entry(
            status="implementing",
            last_reached_status="implementing",
            auth_modes=["oauth_user", "service_to_service"],
            account_types=["personal"],
            surfaces=["chat"],
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": None,
                    "status": "implementing",
                    "last_reached_status": "implementing",
                }
            ],
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == "evidence_by_mode_surface_and_auth" for f in result.findings)

    def test_evidence_matrix_applicable_false_requires_exclusion_reason(self):
        entry = _entry(
            status="implementing",
            last_reached_status="implementing",
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": False,
                    "exclusion_reason": None,
                    "verification_contract_ref": None,
                    "status": "planned",
                    "last_reached_status": "planned",
                }
            ],
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_operation_id_service_id_must_match_file_path(self):
        entry = _entry()
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/gmail/wrong.json"
        )
        assert not result.ok


# ---------------------------------------------------------------------------
# Cross-file consistency: verification_contract <-> ConformanceRun <->
# EvidenceReceipt, using temporary runs/receipts files so this suite never
# depends on (or corrupts) the real fixture jsonl files.
# ---------------------------------------------------------------------------


class TestCrossFileConsistency:
    @pytest.fixture(autouse=True)
    def _isolated_manifest_root(self, tmp_path, monkeypatch):
        runs_dir = tmp_path / "runs"
        receipts_dir = tmp_path / "receipts"
        runs_dir.mkdir()
        receipts_dir.mkdir()
        monkeypatch.setattr(gate, "RUNS_ROOT", str(runs_dir))
        monkeypatch.setattr(gate, "RECEIPTS_ROOT", str(receipts_dir))
        self.runs_dir = runs_dir
        self.receipts_dir = receipts_dir

    def _write_run(self, service_id, **fields):
        base = {
            "run_id": "run_1",
            "operation_id": "op_1",
            "account_binding_ref": "fixture",
            "auth_mode": "oauth_user",
            "account_type": "personal",
            "surface": "chat",
            "tested_sha": "a" * 40,
            "adapter_version": "0.1.0",
            "input_schema_version": "1.0.0",
            "output_schema_version": "1.0.0",
            "runner_version": "0.1.0",
            "executed_at": "2026-01-01T00:00:00Z",
            "request_shape_hash": "sha256:0",
            "response_summary": {
                "fields_present": [],
                "types_matched": True,
                "unexpected_fields": [],
            },
            "verdict": "pass",
            "evidence_receipt_ref": "receipt_1",
        }
        base.update(fields)
        path = self.runs_dir / f"{service_id}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(base) + "\n")
        return base

    def _write_receipt(self, service_id, **fields):
        base = {
            "receipt_id": "receipt_1",
            "conformance_run_ref": "run_1",
            "claim": "test claim",
            "runtime_verified": True,
            "readback_result": None,
            "cleanup_confirmed": False,
            "cleanup_status": "not_applicable",
            "negative_test_refs": ["neg_1"],
        }
        base.update(fields)
        path = self.receipts_dir / f"{service_id}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(base) + "\n")
        return base

    def test_valid_three_way_reference_passes(self):
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert result.ok, result.findings

    def test_contract_verified_run_coordinates_must_match_a_declared_applicable_cell(self):
        """the spec requires contract_verified to be
        certified against a (auth_mode, account_type, surface) combination
        the entry ITSELF declares — a run for an undeclared/mismatched
        coordinate would satisfy an entry-level check that only verifies
        operation_id, even though the entry's own matrix never declared
        that coordinate applicable."""
        self._write_run(
            "github",
            operation_id="op_1",
            auth_mode="service_to_service",  # entry's matrix below never declares this
            account_type="personal",
            surface="chat",
        )
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="contract_verified",
            last_reached_status="contract_verified",
            tested_sha="a" * 40,
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": None,
                    "status": "implementing",
                    "last_reached_status": "implementing",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_contract_verified_run_coordinates_matching_a_declared_cell_passes(self):
        self._write_run(
            "github",
            operation_id="op_1",
            auth_mode="oauth_user",
            account_type="personal",
            surface="chat",
        )
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="contract_verified",
            last_reached_status="contract_verified",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            input_schema={"schema_ref": "https://example.com/x", "schema_version": "1.0.0"},
            output_schema={"schema_ref": "https://example.com/y", "schema_version": "1.0.0"},
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert result.ok, result.findings
        assert result.ok, result.findings

    def test_run_ref_pointing_at_nothing_fails(self):
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "does_not_exist", "receipt_ref": "does_not_exist"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_back_pointer_mismatch_fails(self):
        self._write_run("github", operation_id="op_1", evidence_receipt_ref="receipt_1")
        self._write_receipt("github", conformance_run_ref="some_other_run")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_run_operation_id_mismatch_fails(self):
        self._write_run("github", operation_id="a_different_operation")
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_non_passing_run_never_promotes_status(self):
        self._write_run("github", operation_id="op_1", verdict="fail")
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_runtime_verified_false_never_promotes_status(self):
        self._write_run("github", operation_id="op_1")
        self._write_receipt("github", runtime_verified=False)
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_stale_tested_sha_flagged(self):
        """Immutable ref binding: once the entry's own tested_sha has moved
        past what the certifying run recorded, the pointer is stale."""
        self._write_run("github", operation_id="op_1", tested_sha="a" * 40)
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="b" * 40,  # moved on since the run executed
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any("STALE" in f.message for f in result.findings)

    def test_stale_input_schema_version_flagged(self):
        """the spec names FIVE fields that
        must match the certifying run, not just tested_sha/runner_version/
        adapter.version — input_schema.schema_version and
        output_schema.schema_version are the other two."""
        self._write_run("github", operation_id="op_1", input_schema_version="1.0.0")
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            input_schema={"schema_ref": "x", "schema_version": "2.0.0"},  # moved on
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any("STALE" in f.message and "input_schema" in f.field for f in result.findings)

    def test_stale_output_schema_version_flagged(self):
        self._write_run("github", operation_id="op_1", output_schema_version="1.0.0")
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            output_schema={"schema_ref": "y", "schema_version": "3.0.0"},  # moved on
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any("STALE" in f.message and "output_schema" in f.field for f in result.findings)

    def test_release_verified_requires_run_tested_sha_equals_release_sha(self):
        self._write_run("github", operation_id="op_1", tested_sha="a" * 40)
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="release_verified",
            last_reached_status="release_verified",
            tested_sha="a" * 40,
            merged_sha="a" * 40,
            release_sha="c" * 40,  # different from the run's tested_sha
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_matrix_cell_wrong_coordinate_never_promotes_cell(self):
        """A run recorded for a DIFFERENT auth_mode/account_type/surface must
        never promote this cell, even though it is a genuinely passing run
        for the same operation_id — the four-coordinate equality rule."""
        self._write_run(
            "github",
            operation_id="op_1",
            auth_mode="service_to_service",  # cell below declares oauth_user
            account_type="personal",
            surface="chat",
        )
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="implementing",
            last_reached_status="implementing",
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_code_complete_does_not_require_a_passing_verdict(self):
        """code_complete's own spec text says it "has not yet
        run a live ConformanceRun" — requiring verdict==pass at this rung
        would reject the spec's own legitimate rung. The verdict/
        runtime_verified check applies from contract_verified onward only."""
        self._write_run("github", operation_id="op_1", verdict="fail", runner_version="0.1.0")
        self._write_receipt("github", runtime_verified=False)
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            input_schema={"schema_ref": "https://example.com/x", "schema_version": "1.0.0"},
            output_schema={"schema_ref": "https://example.com/y", "schema_version": "1.0.0"},
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "code_complete",
                    "last_reached_status": "code_complete",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert result.ok, result.findings

    def test_contract_verified_still_requires_a_passing_verdict(self):
        """The other half of the same fix: contract_verified (and later)
        still requires verdict==pass — only code_complete is exempt."""
        self._write_run("github", operation_id="op_1", verdict="fail")
        self._write_receipt("github", runtime_verified=False)
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="contract_verified",
            last_reached_status="contract_verified",
            tested_sha="a" * 40,
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_malformed_jsonl_record_is_a_finding_not_a_crash(self):
        """a JSONL line that decodes to a non-object (a bare
        scalar) must not crash the gate with AttributeError on the first
        lookup — it must be reported as a Finding."""
        path = self.runs_dir / "github.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("42\n")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )  # must not raise
        assert not result.ok

    def test_duplicate_run_id_is_a_finding_not_a_silent_first_match(self):
        """two ConformanceRun records sharing one run_id must
        be rejected as an ambiguous evidence graph, not silently resolved
        to whichever one is first in the file."""
        self._write_run("github", run_id="dup_run", operation_id="op_1")
        self._write_run("github", run_id="dup_run", operation_id="op_2")
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "dup_run", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )  # must not raise
        assert not result.ok

    def test_unhashable_axis_element_in_matrix_is_a_finding_not_a_crash(self):
        """auth_modes/account_types/surfaces containing an
        unhashable element (a list) must not crash set construction — it
        must be reported per-element as a Finding."""
        entry = _entry(
            status="implementing",
            last_reached_status="implementing",
            auth_modes=[[]],
            account_types=["personal"],
            surfaces=["chat"],
            evidence_by_mode_surface_and_auth=[],
        )
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_unhashable_row_coordinate_in_matrix_is_a_finding_not_a_crash(self):
        entry = _entry(
            status="implementing",
            last_reached_status="implementing",
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": [],
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": None,
                    "status": "implementing",
                    "last_reached_status": "implementing",
                }
            ],
        )
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_unhashable_jsonl_id_is_a_finding_not_a_crash(self):
        """record_id in seen_ids hashes record_id, so a JSONL
        record whose id field is itself unhashable (run_id: []) must not
        crash the gate."""
        path = self.runs_dir / "github.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"run_id": []}) + "\n")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )  # must not raise
        assert not result.ok

    def test_applicable_row_outside_declared_cross_product_never_certifies(self):
        """an applicable=true row whose OWN coordinate is not
        even a member of the entry's own declared auth_modes x
        account_types x surfaces cross-product must never count as a
        legitimate cell a run can certify against — a rogue row claiming
        applicable=true for an undeclared coordinate is not itself proof
        that coordinate is real."""
        self._write_run(
            "github",
            operation_id="op_1",
            auth_mode="rogue_undeclared_mode",
            account_type="personal",
            surface="chat",
        )
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="contract_verified",
            last_reached_status="contract_verified",
            tested_sha="a" * 40,
            auth_modes=["oauth_user"],  # rogue_undeclared_mode is NOT here
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "rogue_undeclared_mode",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_merged_entry_still_requires_live_verified_completeness(self):
        """the completeness check was gated solely on
        effective_status == "live_verified", so a merged (or
        release_verified) entry with an applicable cell still stuck at
        implementing skipped the check entirely, even though merged's own
        per-rung requirements are cumulative on top of live_verified's."""
        entry = _entry(
            status="merged",
            last_reached_status="merged",
            tested_sha="a" * 40,
            merged_sha="b" * 40,
            verification_contract={"run_ref": "x", "receipt_ref": "y"},
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": None,
                    "status": "implementing",  # not contract_verified or later
                    "last_reached_status": "implementing",
                }
            ],
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_receipt_cleanup_confirmed_disagreeing_with_cleanup_status_fails(self):
        """The spec derives cleanup_confirmed from cleanup_status
        (true iff 'confirmed') — a receipt where the two disagree is
        malformed and was never checked."""
        self._write_run("github", operation_id="op_1")
        self._write_receipt(
            "github", cleanup_status="not_applicable", cleanup_confirmed=True  # disagrees
        )
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_invalid_cleanup_status_enum_fails(self):
        self._write_run("github", operation_id="op_1")
        self._write_receipt("github", cleanup_status="bogus_value")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_readback_result_must_be_null_for_read_effect(self):
        self._write_run("github", operation_id="op_1")
        self._write_receipt(
            "github",
            readback_result={
                "checked_at": "2026-01-01T00:00:00Z",
                "method": "independent_read",
                "matched": True,
                "detail": "x",
            },
        )
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            effect="read",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_readback_result_missing_subfields_for_write_effect_fails(self):
        self._write_run("github", operation_id="op_1")
        self._write_receipt(
            "github", readback_result={"checked_at": "2026-01-01T00:00:00Z"}  # incomplete
        )
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            effect="write",
            retry={"idempotency_class": "none_verify_by_readback", "detail": "x"},
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_well_formed_readback_result_for_write_effect_passes(self):
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt(
            "github",
            readback_result={
                "checked_at": "2026-01-01T00:00:00Z",
                "method": "independent_read",
                "matched": True,
                "detail": "x",
            },
        )
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            effect="write",
            retry={"idempotency_class": "none_verify_by_readback", "detail": "x"},
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "code_complete",
                    "last_reached_status": "code_complete",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert result.ok, result.findings


class TestFieldTypeEnforcement:
    """Several documented array/string fields were
    checked for presence and, for arrays, container type — but never their
    OWN elements' or scalar's type. A string-valued array or a non-string
    scalar in a documented string field was silently accepted."""

    @pytest.mark.parametrize("field_name", ["operation_id", "provider", "observed_at"])
    def test_non_string_scalar_field_is_rejected(self, field_name):
        entry = _entry(**{field_name: 42})
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    @pytest.mark.parametrize(
        "field_name", ["tool_names", "auth_modes", "scopes", "account_types", "surfaces"]
    )
    def test_non_string_array_element_is_rejected(self, field_name):
        entry = _entry(**{field_name: ["valid_string", 42]})
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    @pytest.mark.parametrize(
        "field_name", ["tool_names", "auth_modes", "scopes", "account_types", "surfaces"]
    )
    def test_non_array_value_for_array_field_is_rejected(self, field_name):
        entry = _entry(**{field_name: "not_a_list"})
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_code_refs_non_string_element_is_rejected(self):
        entry = _entry(code_refs=["valid/path.py", 42])
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_code_refs_empty_array_is_legal(self):
        entry = _entry(code_refs=[])
        result = gate.validate_entry(entry, _REF)
        assert result.ok, result.findings


# ---------------------------------------------------------------------------
# Fixture entries this PR actually ships: prove they stay valid, not just
# that the validator's own synthetic inputs do.
# ---------------------------------------------------------------------------


class TestFixtureEntries:
    def test_all_shipped_fixture_entries_validate_clean(self):
        result = gate.run_scan()
        assert result.ok, result.findings

    def test_fixture_count_matches_expectation(self):
        paths = gate._iter_entry_files()
        # This repo ships exactly 1 fixture entry as of this PR (gh_search_repositories,
        # status: planned). A prior draft also shipped a synthetic contract_verified
        # fixture (gh_get_issue) with a fabricated ConformanceRun/EvidenceReceipt pair
        # at this same authoritative path — review correctly flagged this as fabricated
        # evidence in the authoritative record the spec's own text says is "written
        # once and never edited" / "kept, never deleted". That lifecycle case is
        # exercised instead by TestCrossFileConsistency above, against synthetic
        # dicts and temp-file jsonl fixtures that never land in the real, published
        # entries/runs/receipts trees. A future entry-population round will add many
        # more real entries; this assertion exists to catch an accidental fixture
        # deletion, not to cap growth — update the expected count in the SAME commit
        # that adds or removes a fixture.
        # assertion exists to catch an accidental fixture deletion, not to cap growth —
        # update the expected count in the SAME commit that adds or removes a fixture.
        assert len(paths) == 1, paths


# ---------------------------------------------------------------------------
# Subprocess-level: the gate's own --test and default scan modes, run exactly
# as CI will invoke them.
# ---------------------------------------------------------------------------


class TestCLI:
    def test_self_test_subcommand_passes(self):
        proc = subprocess.run(
            [sys.executable, _SCRIPT_PATH, "--test"],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_default_scan_passes_on_real_repo_state(self):
        proc = subprocess.run(
            [sys.executable, _SCRIPT_PATH],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_scan_fails_closed_on_a_broken_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = os.path.join(tmp, "broken.json")
            with open(bad_path, "w", encoding="utf-8") as fh:
                json.dump({"operation_id": "incomplete"}, fh)
            proc = subprocess.run(
                [sys.executable, _SCRIPT_PATH, "--entry", bad_path],
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
            )
            assert proc.returncode == 1
            assert "required field is missing" in (proc.stdout + proc.stderr)

    def test_invalid_json_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = os.path.join(tmp, "broken.json")
            with open(bad_path, "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            proc = subprocess.run(
                [sys.executable, _SCRIPT_PATH, "--entry", bad_path],
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
            )
            assert proc.returncode == 1
            assert "invalid JSON" in (proc.stdout + proc.stderr)
