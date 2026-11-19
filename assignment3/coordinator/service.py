"""Durable two-phase coordination using the Banking Server participant API."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import httpx
from sqlalchemy import Engine, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import sessionmaker

from coordinator.models import CoordinatedTransfer
from server.app.sharding import resolve_shard

logger = logging.getLogger(__name__)


class CoordinatorConflict(ValueError):
    """Raised when one transaction UUID is reused for another transfer."""


@dataclass(frozen=True, slots=True)
class TransferOutcome:
    transaction_id: UUID
    state: str
    decision: str | None
    source_account_id: int
    destination_account_id: int
    amount: int
    error_code: str | None
    balance_before: int | None
    balance_after: int | None
    created_at: datetime
    replayed: bool


class Coordinator:
    def __init__(
        self,
        engine: Engine,
        shard_urls: dict[str, tuple[str, ...]],
        client: httpx.Client,
        after_commit_hook: Callable[[UUID], None] | None = None,
    ) -> None:
        self.engine = engine
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self.shard_urls = shard_urls
        self.client = client
        self.after_commit_hook = after_commit_hook

    def transfer(
        self,
        transaction_id: UUID,
        source_account_id: int,
        destination_account_id: int,
        amount: int,
    ) -> TransferOutcome:
        source_shard = resolve_shard(source_account_id).shard_id
        destination_shard = resolve_shard(destination_account_id).shard_id
        if source_shard == destination_shard:
            raise ValueError("Coordinator accepts cross-shard transfers only.")
        payload_hash = _payload_hash(source_account_id, destination_account_id, amount)
        created = self._register(
            transaction_id,
            source_account_id,
            destination_account_id,
            amount,
            payload_hash,
            source_shard,
            destination_shard,
        )
        raw = self.engine.raw_connection()
        driver = raw.driver_connection
        driver.autocommit = True
        cursor = driver.cursor()
        cursor.execute("SELECT pg_advisory_lock(%s)", (_lock_key(transaction_id),))
        try:
            item = self._load(transaction_id)
            self._check_payload(item, payload_hash)
            self._drive(item)
            return self._outcome(self._load(transaction_id), replayed=not created)
        finally:
            try:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s)", (_lock_key(transaction_id),)
                )
            except Exception:
                logger.warning("Coordinator lock cleanup failed", exc_info=True)
            try:
                driver.autocommit = False
            except Exception:
                logger.warning("Coordinator connection cleanup failed", exc_info=True)
            raw.close()

    def lookup(self, transaction_id: UUID) -> TransferOutcome | None:
        with self.sessions() as session:
            item = session.get(CoordinatedTransfer, transaction_id)
            if item is None:
                return None
        if item.state != "RESOLVED":
            raw = self.engine.raw_connection()
            driver = raw.driver_connection
            driver.autocommit = True
            cursor = driver.cursor()
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s)", (_lock_key(transaction_id),)
            )
            acquired = cursor.fetchone()[0]
            try:
                if acquired:
                    self._drive(self._load(transaction_id))
            finally:
                if acquired:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(%s)", (_lock_key(transaction_id),)
                    )
                try:
                    driver.autocommit = False
                except Exception:
                    logger.warning(
                        "Coordinator lookup connection cleanup failed", exc_info=True
                    )
                raw.close()
        return self._outcome(self._load(transaction_id), replayed=True)

    def recover(self, limit: int = 100) -> None:
        with self.sessions() as session:
            ids = session.scalars(
                select(CoordinatedTransfer.transaction_id)
                .where(CoordinatedTransfer.state != "RESOLVED")
                .limit(limit)
            ).all()
        for transaction_id in ids:
            try:
                self.lookup(transaction_id)
            except Exception:
                logger.warning(
                    "Coordinator recovery attempt failed",
                    extra={"transaction_id": str(transaction_id)},
                    exc_info=True,
                )

    def _drive(self, item: CoordinatedTransfer) -> None:
        if item.decision is None:
            self._set_state(item.transaction_id, state="PREPARING")
            source = self._participant(
                item.source_shard,
                "prepare",
                {
                    "transaction_id": str(item.transaction_id),
                    "account_id": item.source_account_id,
                    "direction": "DEBIT",
                    "amount": item.amount,
                },
            )
            self._record_participant(item.transaction_id, "source_state", source)
            if source.get("state") == "PREPARED":
                destination = self._participant(
                    item.destination_shard,
                    "prepare",
                    {
                        "transaction_id": str(item.transaction_id),
                        "account_id": item.destination_account_id,
                        "direction": "CREDIT",
                        "amount": item.amount,
                    },
                )
                self._record_participant(
                    item.transaction_id, "destination_state", destination
                )
            else:
                destination = {"state": "NOT_CONTACTED"}
                self._record_participant(
                    item.transaction_id, "destination_state", destination
                )
            decision = (
                "COMMIT"
                if source.get("state") == destination.get("state") == "PREPARED"
                else "ABORT"
            )
            error_code = (
                source.get("error_code")
                or destination.get("error_code")
                or ("DISTRIBUTED_TRANSFER_ABORTED" if decision == "ABORT" else None)
            )
            self._decide(item.transaction_id, decision, error_code)
            if decision == "COMMIT" and self.after_commit_hook is not None:
                self.after_commit_hook(item.transaction_id)

        item = self._load(item.transaction_id)
        action = "commit" if item.decision == "COMMIT" else "abort"
        source = self._resolution_result(item, "source", action)
        destination = self._resolution_result(item, "destination", action)
        self._record_participant(item.transaction_id, "source_state", source)
        self._record_participant(item.transaction_id, "destination_state", destination)
        expected = (
            "COMMITTED"
            if item.decision == "COMMIT"
            else {
                "ABORTED",
                "REJECTED",
                "NOT_CONTACTED",
            }
        )
        resolved = (
            source.get("state") == destination.get("state") == expected
            if isinstance(expected, str)
            else source.get("state") in expected
            and destination.get("state") in expected
        )
        if resolved:
            values: dict[str, object] = {"state": "RESOLVED"}
            if item.decision == "COMMIT":
                values.update(
                    balance_before=source.get("balance_before"),
                    balance_after=source.get("balance_after"),
                )
            self._set_state(item.transaction_id, **values)

    def _resolution_result(
        self, item: CoordinatedTransfer, participant_name: str, action: str
    ) -> dict:
        current = getattr(item, f"{participant_name}_state")
        if item.decision == "ABORT" and current == "NOT_CONTACTED":
            return {"state": "NOT_CONTACTED"}
        shard = getattr(item, f"{participant_name}_shard")
        return self._participant(
            shard, action, {"transaction_id": str(item.transaction_id)}
        )

    def _participant(self, shard: str, action: str, payload: dict) -> dict:
        last: dict = {"state": "UNKNOWN"}
        for base_url in self.shard_urls[shard]:
            try:
                response = self.client.post(
                    f"{base_url}/internal/2pc/{action}", json=payload
                )
            except httpx.RequestError:
                continue
            if response.status_code == 200:
                document = response.json()
                return document if isinstance(document, dict) else last
            if response.status_code == 409:
                status = self._participant_status(
                    shard, UUID(payload["transaction_id"])
                )
                return status
        return last

    def _participant_status(self, shard: str, transaction_id: UUID) -> dict:
        for base_url in self.shard_urls[shard]:
            try:
                response = self.client.get(f"{base_url}/internal/2pc/{transaction_id}")
            except httpx.RequestError:
                continue
            if response.status_code == 200 and isinstance(response.json(), dict):
                return response.json()
        return {"state": "UNKNOWN"}

    def _register(
        self,
        transaction_id: UUID,
        source: int,
        destination: int,
        amount: int,
        payload_hash: str,
        source_shard: str,
        destination_shard: str,
    ) -> bool:
        statement = (
            insert(CoordinatedTransfer)
            .values(
                transaction_id=transaction_id,
                source_account_id=source,
                destination_account_id=destination,
                amount=amount,
                payload_hash=payload_hash,
                source_shard=source_shard,
                destination_shard=destination_shard,
                state="REGISTERED",
            )
            .on_conflict_do_nothing(index_elements=["transaction_id"])
        )
        with self.sessions.begin() as session:
            result = session.execute(statement)
        item = self._load(transaction_id)
        self._check_payload(item, payload_hash)
        return result.rowcount == 1

    def _load(self, transaction_id: UUID) -> CoordinatedTransfer:
        with self.sessions() as session:
            item = session.get(CoordinatedTransfer, transaction_id)
            if item is None:
                raise RuntimeError("Coordinator transaction disappeared.")
            session.expunge(item)
            return item

    @staticmethod
    def _check_payload(item: CoordinatedTransfer, payload_hash: str) -> None:
        if item.payload_hash != payload_hash:
            raise CoordinatorConflict(
                "Transaction UUID was reused with another payload."
            )

    def _set_state(self, transaction_id: UUID, **values: object) -> None:
        values["updated_at"] = text("CURRENT_TIMESTAMP")
        with self.sessions.begin() as session:
            session.execute(
                update(CoordinatedTransfer)
                .where(CoordinatedTransfer.transaction_id == transaction_id)
                .values(**values)
            )

    def _decide(
        self, transaction_id: UUID, decision: str, error_code: str | None
    ) -> None:
        with self.sessions.begin() as session:
            item = session.get(
                CoordinatedTransfer, transaction_id, with_for_update=True
            )
            if item.decision is not None and item.decision != decision:
                raise RuntimeError("Durable coordinator decision cannot be reversed.")
            item.decision = decision
            item.error_code = error_code
            item.state = "DECIDED"

    def _record_participant(
        self, transaction_id: UUID, field: str, result: dict
    ) -> None:
        values: dict[str, object] = {field: result.get("state", "UNKNOWN")}
        if field == "source_state":
            values["balance_before"] = result.get("balance_before")
            values["balance_after"] = result.get("balance_after")
        self._set_state(transaction_id, **values)

    @staticmethod
    def _outcome(item: CoordinatedTransfer, replayed: bool) -> TransferOutcome:
        return TransferOutcome(
            item.transaction_id,
            item.state,
            item.decision,
            item.source_account_id,
            item.destination_account_id,
            item.amount,
            item.error_code,
            item.balance_before,
            item.balance_after,
            item.created_at,
            replayed,
        )


def _payload_hash(source: int, destination: int, amount: int) -> str:
    payload = json.dumps(
        {
            "source_account_id": source,
            "destination_account_id": destination,
            "amount": amount,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _lock_key(transaction_id: UUID) -> int:
    digest = hashlib.blake2b(transaction_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, signed=True)
