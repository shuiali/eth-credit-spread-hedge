"""Regression tests for the integrated reconciliation supervisor."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from eth_credit_hedge.application import demo_strategy_runtime as runtime


class _Done(RuntimeError):
    pass


class _Operations:
    def update_runtime(self, state: object) -> None:
        del state

    def mark_reconciliation(self, matched: bool, detail: str) -> None:
        del matched, detail


async def _no_sleep(seconds: float) -> None:
    del seconds


def test_periodic_reconciliation_tolerates_one_transient_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = SimpleNamespace(
        state=SimpleNamespace(consecutive_reconciliation_failures=0)
    )
    outcomes = iter((False, True))

    async def reconcile(*args: object) -> bool:
        del args
        try:
            matched = next(outcomes)
        except StopIteration as exc:
            raise _Done from exc
        journal.state.consecutive_reconciliation_failures = (
            0
            if matched
            else journal.state.consecutive_reconciliation_failures + 1
        )
        return matched

    monkeypatch.setattr(runtime, "_reconcile_runtime", reconcile)

    with pytest.raises(_Done):
        asyncio.run(
            runtime._reconciliation_loop(
                journal,
                object(),
                object(),
                _Operations(),
                lambda: None,
                2,
                _no_sleep,
            )
        )


def test_periodic_reconciliation_fails_at_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = SimpleNamespace(
        state=SimpleNamespace(consecutive_reconciliation_failures=0)
    )

    async def reconcile(*args: object) -> bool:
        del args
        journal.state.consecutive_reconciliation_failures += 1
        return False

    monkeypatch.setattr(runtime, "_reconcile_runtime", reconcile)

    with pytest.raises(RuntimeError, match="periodic reconciliation failed"):
        asyncio.run(
            runtime._reconciliation_loop(
                journal,
                object(),
                object(),
                _Operations(),
                lambda: None,
                2,
                _no_sleep,
            )
        )
