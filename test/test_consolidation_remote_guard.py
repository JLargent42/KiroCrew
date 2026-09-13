"""Peer-produced transcripts must never be consolidated into local memory.

The relay mirrors a remote turn's frames through ordinary local ``slot.append``
calls, which is what makes the local transcript a true mirror -- and also lands
peer-authored conversation in local history. Without an executor gate in
``history_consolidation`` -- it carries no memory_mode or privacy gate of any
other kind -- a distilled summary of another machine's conversation could
be re-injected into unrelated local sessions, carried into ``kirocrew snapshot``
and surfaced in local search.

The gate lives in ``_consolidate`` because every entry point funnels through it,
the same argument the retry-eligibility gate beside it makes. These tests pin
that the refusal happens BEFORE anything reads the transcript, and that ordinary
local sessions are untouched.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import history as history_mod
from kiro_crew.history import ConversationLog, HistoryConsolidator

KEY = "dashboard:peer-mirror"


def _seed_log(tmp_path, key: str = KEY, count: int = 3) -> ConversationLog:
    """A real transcript with *count* unconsolidated messages."""
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    with history_mod.allow_on_loop_persist():
        for i in range(count):
            log.append(key, "user", f"m{i}")
    return log


def _make_consolidator(log: Any, **kw: Any) -> HistoryConsolidator:
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    kw.setdefault("history_idle_secs", 0)
    kw.setdefault("sessions", None)
    return HistoryConsolidator(log=log, memory=memory, migrated=True, **kw)


def _mark_remote(log: ConversationLog, key: str = KEY, value: str = "remote") -> None:
    with history_mod.allow_on_loop_persist():
        log.update_metadata(key, {"executor": value})


class TestIsRemoteTranscript:
    """The predicate itself: absence reads local, an unreadable header does not."""

    def test_an_explicit_remote_executor_is_remote(self, tmp_path):
        log = _seed_log(tmp_path)
        _mark_remote(log)
        assert _make_consolidator(log)._is_remote_transcript(KEY) is True

    def test_an_absent_header_reads_as_local(self, tmp_path):
        # The executor header is only written for BOUND slots, so failing closed
        # on absence would refuse every ordinary local session.
        log = _seed_log(tmp_path)
        assert _make_consolidator(log)._is_remote_transcript(KEY) is False

    @pytest.mark.parametrize("value", ["", "local", "acp", "unknown-future-value"])
    def test_other_executor_values_read_as_local(self, tmp_path, value):
        log = _seed_log(tmp_path)
        _mark_remote(log, value=value)
        assert _make_consolidator(log)._is_remote_transcript(KEY) is False

    @pytest.mark.parametrize("value", ["REMOTE", "Remote", "rEmOtE"])
    def test_the_comparison_is_case_insensitive(self, tmp_path, value):
        # A hand-edited transcript header is not bound by the API's validation,
        # so the membership test normalizes rather than trusting the casing.
        log = _seed_log(tmp_path)
        _mark_remote(log, value=value)
        assert _make_consolidator(log)._is_remote_transcript(KEY) is True

    def test_an_unreadable_header_fails_closed(self):
        # Distinct from absence: False means the executor is UNKNOWN, not local.
        # Skipping a consolidation is recoverable; leaking peer content is not,
        # so the unknown case refuses.
        log = MagicMock()
        log.get_metadata_status.return_value = ({}, False)
        assert _make_consolidator(log)._is_remote_transcript(KEY) is True

    def test_a_metadata_status_exception_fails_closed(self):
        # The status reader handles damaged content and bounded I/O retries, but
        # failures before it can establish readability must remain fail-closed.
        log = MagicMock()
        log.get_metadata_status.side_effect = OSError("metadata status unavailable")
        assert _make_consolidator(log)._is_remote_transcript(KEY) is True


class TestConsolidateRefusesRemoteTranscripts:
    @pytest.mark.asyncio
    async def test_the_remote_guard_is_evaluated_off_the_event_loop(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        evaluated_without_running_loop: list[bool] = []

        def is_remote(_key: str) -> bool:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                evaluated_without_running_loop.append(True)
            else:
                evaluated_without_running_loop.append(False)
            return True

        c._is_remote_transcript = is_remote  # type: ignore[method-assign]

        assert await c._consolidate(KEY) is None
        assert evaluated_without_running_loop == [True]

    @pytest.mark.asyncio
    async def test_a_remote_transcript_is_refused_before_the_transcript_is_read(self, tmp_path):
        """The guard must precede the snapshot, not merely the memory write.

        Asserting only "memory was not written" would also pass if the refusal
        happened late, after the transcript had been read and a provider turn
        billed. Exploding the snapshot proves the ordering.
        """
        log = _seed_log(tmp_path)
        _mark_remote(log)
        c = _make_consolidator(log)
        log.snapshot_for_consolidation = MagicMock(
            side_effect=AssertionError("guard did not fire before the snapshot")
        )

        assert await c._consolidate(KEY) is None

        log.snapshot_for_consolidation.assert_not_called()
        c._memory.append_history.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_malformed_metadata_header_is_skipped(self, tmp_path):
        log = _seed_log(tmp_path)
        path = log._path(KEY)
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0] = "{malformed metadata"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log._meta_cache.clear()
        c = _make_consolidator(log)
        c._call_llm = AsyncMock(return_value={"history_entry": "must not be written"})

        assert await c._consolidate(KEY) is None

        c._call_llm.assert_not_awaited()
        c._memory.append_history.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_absent_metadata_header_still_consolidates(self, tmp_path):
        log = _seed_log(tmp_path)
        path = log._path(KEY)
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
        log._meta_cache.clear()
        c = _make_consolidator(log)
        c._call_llm = AsyncMock(return_value={"history_entry": "local summary"})

        assert await c._consolidate(KEY) is None

        c._call_llm.assert_awaited_once()
        c._memory.append_history.assert_called_once_with("local summary")

    @pytest.mark.asyncio
    async def test_a_local_transcript_is_not_refused_by_the_guard(self, tmp_path):
        """The negative half: the guard must not block ordinary local sessions.

        Reaching the snapshot is the assertion -- it is the first thing after
        the guard, so being called proves the guard let the session through
        without this test having to drive a full provider consolidation.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        reached = MagicMock(side_effect=RuntimeError("reached the snapshot"))
        log.snapshot_for_consolidation = reached

        with pytest.raises(RuntimeError, match="reached the snapshot"):
            await c._consolidate(KEY)

        reached.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_cli_path_reports_done_rather_than_a_retryable_skip(self, tmp_path):
        """``kirocrew consolidate --all`` is the reachable leak vector.

        It globs every transcript on disk with no executor filter, so it is the
        caller the choke point exists for. Two things are pinned: the transcript
        is never read (the snapshot explodes if reached -- asserting only on the
        return value passes vacuously, because a mocked memory makes an
        unguarded consolidation fail and report done anyway), and a remote
        transcript is a PERMANENT skip, so it reports done (True) like the
        sensitive-session skip rather than False, which the CLI renders as a
        retryable refusal.
        """
        log = _seed_log(tmp_path)
        _mark_remote(log)
        c = _make_consolidator(log)
        log.snapshot_for_consolidation = MagicMock(
            side_effect=AssertionError("the CLI path reached the transcript")
        )

        assert await c.consolidate_now(KEY) is True

        log.snapshot_for_consolidation.assert_not_called()
        c._memory.append_history.assert_not_called()
