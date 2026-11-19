"""In-memory per-shard replica health and round-robin selection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from gateway.config import ReplicaConfig


@dataclass(slots=True)
class ReplicaState:
    config: ReplicaConfig
    healthy: bool = False


class NoHealthyReplica(RuntimeError):
    """Raised when the requested shard has no usable application replica."""


class ReplicaPool:
    """Maintain health and independent round-robin cursors per shard."""

    def __init__(self, replicas: tuple[ReplicaConfig, ...]) -> None:
        self._replicas = {
            shard_id: [
                ReplicaState(replica)
                for replica in replicas
                if replica.shard_id == shard_id
            ]
            for shard_id in {replica.shard_id for replica in replicas}
        }
        self._cursors = {shard_id: 0 for shard_id in self._replicas}
        self._lock = asyncio.Lock()

    async def select(self, shard_id: str) -> ReplicaConfig:
        """Select the next healthy replica in one shard only."""
        async with self._lock:
            candidates = self._replicas.get(shard_id, [])
            if not candidates or not any(item.healthy for item in candidates):
                raise NoHealthyReplica(shard_id)
            start = self._cursors[shard_id]
            for offset in range(len(candidates)):
                index = (start + offset) % len(candidates)
                if candidates[index].healthy:
                    self._cursors[shard_id] = (index + 1) % len(candidates)
                    return candidates[index].config
        raise NoHealthyReplica(shard_id)

    async def set_health(self, replica_id: str, healthy: bool) -> None:
        """Update one replica without disturbing round-robin state."""
        async with self._lock:
            for candidates in self._replicas.values():
                for item in candidates:
                    if item.config.replica_id == replica_id:
                        item.healthy = healthy
                        return

    async def snapshot(self) -> dict[str, dict[str, bool]]:
        """Return health state for readiness and tests."""
        async with self._lock:
            return {
                shard_id: {item.config.replica_id: item.healthy for item in candidates}
                for shard_id, candidates in self._replicas.items()
            }
