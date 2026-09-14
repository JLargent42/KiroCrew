"""Interactive-path reactive fallback when the account is not entitled to the
configured model.

A new conversation starts on the configured model (commonly the ``auto``
sentinel). When the account is not entitled to it, the prompt-time error is an
ENTITLEMENT rejection, classified terminal -- the two throttle-gated fallback
branches in the chat runner do not fire, so the first reply just fails. The runner
runs the same reactive swap the unattended surfaces run
(``stream_and_collect`` Case 2.5 / ``run_bg_oneliner``): retry ONCE on the first
advertised model the account can run, which is never the failed id and never the
``auto`` sentinel.

These tests pin, through the real ``_run_chat`` ladder:
  - an unentitled named model triggers exactly one ``set_model`` to a non-auto
    advertised id and re-queues the turn on the same session;
  - an account advertising NOTHING accessible surfaces the terminal entitlement
    error with no swap and no re-queue;
  - an unrelated provider error (no rejected model) stays terminal -- the trigger
    is not a catch-all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.client import AcpError
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.chat_utils import MODEL_UNENTITLED_KIND, SYNTHETIC_RECOVERY_KIND


def _make_state_for_run_chat(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    return state


def _client_raising(exc: BaseException) -> AsyncMock:
    """A mock ACP client whose FIRST stream raises *exc* before any token, then
    streams a normal completion on the re-queued turn (so a successful swap is
    observable as the replay completing rather than as transient queue state,
    which ``_run_chat`` drains within the same call)."""
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.set_model = AsyncMock()
    client.served_model = ""
    # No wrapped ACP child, so the session-init OAuth drain is a no-op instead
    # of awaiting an auto-created AsyncMock coroutine.
    client.client = None
    calls = {"n": 0}

    async def _stream(msg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        for ev in (
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="hello from the fallback model"),
            LLMEvent(kind=EVENT_COMPLETE),
        ):
            yield ev

    client.stream = _stream
    client.stream_command = _stream
    return client


def _client_raising_always(exc: BaseException) -> AsyncMock:
    """A mock ACP client whose stream always raises *exc* (for the paths that
    must NOT swap or re-queue -- the turn ends terminally on the first pass)."""
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.set_model = AsyncMock()
    client.served_model = ""
    client.client = None

    async def _stream(msg):
        raise exc
        yield  # pragma: no cover -- makes _stream an async generator

    client.stream = _stream
    client.stream_command = _stream
    return client


def _rejection(model: str, advertised: list[str]) -> AcpError:
    """A prompt-time entitlement rejection exactly as ``_raise_acp_error`` tags it:
    a named model absent from the advertised list, ``transient`` False."""
    exc = AcpError(f"Your account does not have access to model '{model}'.", transient=False)
    exc.rejected_model = model
    exc.advertised = list(advertised)
    return exc


@pytest.mark.asyncio
async def test_unentitled_model_swaps_to_first_advertised_and_requeues(tmp_path, monkeypatch):
    """auto is refused for entitlement while the account advertises real models:
    the runner swaps to the first accessible non-auto id and re-queues once."""
    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    # auto rejected; account is served two concrete models.
    client = _client_raising(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    # Capture every queue_insert so the re-queue is observable regardless of the
    # tail-drain that consumes it within the same _run_chat call. _ChatSlot is
    # __slots__-based (its bound method is read-only), so wrap the repository.
    _inserts: list[dict] = []
    _repo = slot._queue_repository
    _real_insert = _repo.queue_insert

    def _record_insert(owner, index, content, kind="", *args, **kw):
        _inserts.append({"content": content, "kind": kind})
        return _real_insert(owner, index, content, kind, *args, **kw)

    monkeypatch.setattr(_repo, "queue_insert", _record_insert)

    await _run_chat(state, slot, "first message")

    # Exactly one swap, to the first advertised model (never auto, never the
    # failed id).
    client.set_model.assert_awaited_once_with("claude-opus-5")
    assert slot._model_access_fallback_used is True

    # A visible, persisted notice names both the refused and the substitute id.
    notices = [m for m in slot.messages if m.get("role") == "notice"]
    assert any("auto" in m["content"] and "claude-opus-5" in m["content"] for m in notices), notices

    # The turn was re-queued once on the same session as a synthetic recovery,
    # not surfaced as a terminal entitlement error.
    recovery_inserts = [
        i
        for i in _inserts
        if i["kind"] == SYNTHETIC_RECOVERY_KIND and i["content"] == "first message"
    ]
    assert len(recovery_inserts) == 1, _inserts
    errors = [m for m in slot.messages if m.get("role") == "error"]
    assert not any(
        (m.get("meta") or {}).get("kind") == MODEL_UNENTITLED_KIND for m in errors
    ), errors


@pytest.mark.asyncio
async def test_no_accessible_model_surfaces_terminal_error_without_swap(tmp_path, monkeypatch):
    """An account advertising nothing but the refused id has no accessible
    candidate: fail fast and legibly with the terminal entitlement error, no
    swap, no re-queue."""
    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    # The configured model is genuinely unentitled (absent from the advertised
    # set), and the only thing advertised is the "auto" sentinel -- which
    # first_advertised_fallback skips -- so there is no accessible candidate.
    client = _client_raising_always(_rejection("claude-opus-5", ["auto"]))
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "first message")

    client.set_model.assert_not_awaited()
    assert slot._model_access_fallback_used is False
    # Terminal entitlement error surfaced (tagged so the frontend offers the
    # picker); nothing re-queued.
    errors = [m for m in slot.messages if m.get("role") == "error"]
    assert any((m.get("meta") or {}).get("kind") == MODEL_UNENTITLED_KIND for m in errors), errors
    assert not [q for q in slot._queue if q.get("kind") == SYNTHETIC_RECOVERY_KIND]


@pytest.mark.asyncio
async def test_unrelated_provider_error_stays_terminal_no_swap(tmp_path, monkeypatch):
    """An error that names NO rejected model is not an access denial: the trigger
    must not widen into a catch-all that masks a real fault as a model switch."""
    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    # A terminal error carrying NO rejected_model tag (e.g. a validation fault).
    # It even carries a usable advertised list, so the ONLY thing that keeps this
    # from swapping is the rejected-model requirement itself -- widen the trigger
    # to fire without a named rejection and this test reds.
    exc = AcpError("ValidationException: malformed request", transient=False)
    exc.advertised = ["claude-opus-5", "claude-sonnet-5"]
    client = _client_raising_always(exc)
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "first message")

    client.set_model.assert_not_awaited()
    assert slot._model_access_fallback_used is False
    assert not [q for q in slot._queue if q.get("kind") == SYNTHETIC_RECOVERY_KIND]


@pytest.mark.asyncio
async def test_swap_recovery_replay_preserves_the_one_shot_flag(tmp_path, monkeypatch):
    """The swap re-queues the user's ORIGINAL message, which the turn-start reset
    cannot tell from a fresh user turn. The _model_access_recovery_pending latch
    the swap sets makes the reset preserve _model_access_fallback_used for that
    one replay, so a still-unentitled candidate cannot trigger a second swap.

    Without the latch the flag would reset to False on the replay and the one-shot
    guarantee would be delivered only by model_is_unusable, contradicting the
    branch's own bounded-by-one-attempt invariant."""
    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    # A clean client: the point is the reset path, not another rejection.
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.set_model = AsyncMock()
    client.served_model = ""
    client.client = None

    async def _stream(msg):
        for ev in (LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), LLMEvent(kind=EVENT_COMPLETE)):
            yield ev

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    # Simulate the state a swap leaves behind before its replay turn runs.
    slot._model_access_fallback_used = True
    slot._model_access_recovery_pending = True

    await _run_chat(state, slot, "first message")

    # The replay preserved the one-shot flag and consumed the latch.
    assert slot._model_access_fallback_used is True
    assert slot._model_access_recovery_pending is False


@pytest.mark.asyncio
async def test_stop_during_set_model_abandons_the_replay(tmp_path, monkeypatch):
    """set_model is a provider RPC that yields the event loop, so a Stop can land
    between the elif's entry guard and the re-queue. The re-queue re-checks the
    live-stop signals every sibling requeue site checks; a stopped turn must not
    replay ahead of the user's intent. The swap itself already happened and is
    harmless -- only the replay is abandoned."""
    state = _make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")

    # A stop resolves during the set_model await: bump _stop_generation there, the
    # same signal the sibling guards read.
    client = _client_raising(_rejection("auto", ["claude-opus-5", "claude-sonnet-5"]))

    async def _set_model_then_stop(_model):
        slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1

    client.set_model = AsyncMock(side_effect=_set_model_then_stop)
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    _inserts: list[dict] = []
    _repo = slot._queue_repository
    _real_insert = _repo.queue_insert

    def _record_insert(owner, index, content, kind="", *args, **kw):
        _inserts.append({"content": content, "kind": kind})
        return _real_insert(owner, index, content, kind, *args, **kw)

    monkeypatch.setattr(_repo, "queue_insert", _record_insert)

    await _run_chat(state, slot, "first message")

    # The swap ran, but the stopped turn was NOT re-queued.
    client.set_model.assert_awaited_once_with("claude-opus-5")
    assert not [
        i
        for i in _inserts
        if i["kind"] == SYNTHETIC_RECOVERY_KIND and i["content"] == "first message"
    ], _inserts
    assert slot._model_access_recovery_pending is False
