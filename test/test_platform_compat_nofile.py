"""Read-only nofile primitive, including unavailable Windows/rlimit branches."""

from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat as pc


def test_posix_reads_the_soft_limit(monkeypatch):
    monkeypatch.setattr(pc, "IS_POSIX", True)
    monkeypatch.setattr(
        pc,
        "resource",
        SimpleNamespace(
            RLIMIT_NOFILE=7,
            RLIM_INFINITY=-1,
            getrlimit=lambda which: (4321, 9999) if which == 7 else None,
        ),
        raising=False,
    )
    assert pc.nofile_soft_limit() == 4321


def test_windows_has_no_rlimit(monkeypatch):
    monkeypatch.setattr(pc, "IS_POSIX", False)
    monkeypatch.delattr(pc, "resource", raising=False)
    assert pc.nofile_soft_limit() == 0


@pytest.mark.parametrize("error", [OSError, ValueError, ImportError])
def test_unreadable_rlimit_is_zero(monkeypatch, error):
    def boom(which):
        raise error("no rlimit")

    monkeypatch.setattr(pc, "IS_POSIX", True)
    monkeypatch.setattr(
        pc, "resource", SimpleNamespace(RLIMIT_NOFILE=7, getrlimit=boom), raising=False
    )
    assert pc.nofile_soft_limit() == 0


@pytest.mark.parametrize("soft", [-1, -2, 0])
def test_infinity_or_negative_is_unbounded(monkeypatch, soft):
    monkeypatch.setattr(pc, "IS_POSIX", True)
    monkeypatch.setattr(
        pc,
        "resource",
        SimpleNamespace(RLIMIT_NOFILE=7, RLIM_INFINITY=-1, getrlimit=lambda which: (soft, -1)),
        raising=False,
    )
    assert pc.nofile_soft_limit() == 0
