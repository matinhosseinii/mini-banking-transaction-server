"""Focused tests for Coordinator recovery-loop resilience."""

from __future__ import annotations

import asyncio

from coordinator.app import _recover_once


class TransientRecoveryFailure:
    def __init__(self) -> None:
        self.calls = 0

    def recover(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise OSError("temporary database failover")


def test_recovery_cycle_survives_transient_database_failure(monkeypatch) -> None:
    coordinator = TransientRecoveryFailure()

    async def run_inline(function) -> None:
        function()

    monkeypatch.setattr(asyncio, "to_thread", run_inline)

    async def recover_twice() -> None:
        await _recover_once(coordinator)
        await _recover_once(coordinator)

    asyncio.run(recover_twice())

    assert coordinator.calls == 2
