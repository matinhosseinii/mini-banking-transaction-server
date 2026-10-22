"""Durable one-file-per-request persistence for unresolved mutations."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID


class PendingStoreError(RuntimeError):
    """Raised when pending persistence or locking cannot be completed safely."""


class PendingCollisionError(PendingStoreError):
    """Raised when a request UUID already has an unresolved record."""


@dataclass(frozen=True, slots=True)
class PendingRequest:
    request_id: UUID
    service: str
    operation: str
    method: str
    path: str
    payload: dict[str, Any] | None
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        request_id: UUID,
        service: str,
        operation: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> PendingRequest:
        return cls(
            request_id=request_id,
            service=service,
            operation=operation,
            method=method,
            path=path,
            payload=payload,
            created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )


class PendingStore:
    """Atomic pending request storage protected by one OS-level lock."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock_path = root / ".lock"

    @contextmanager
    def locked(self) -> Iterator[PendingStore]:
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as error:
            raise PendingStoreError("Unable to initialize pending store.") from error
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PendingStoreError(
                    "Pending store is locked by another client process."
                ) from None
            yield self
        finally:
            os.close(descriptor)

    def save(self, request: PendingRequest) -> None:
        directory = self._service_directory(request.service)
        destination = directory / f"{request.request_id}.json"
        document = asdict(request)
        document["request_id"] = str(request.request_id)
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=directory, prefix=".pending-", suffix=".tmp"
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(temporary_name, destination)
            except FileExistsError:
                raise PendingCollisionError(
                    f"Request {request.request_id} is already pending; use "
                    "'pending check' or 'pending retry'."
                ) from None
            os.unlink(temporary_name)
            temporary_name = None
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except PendingCollisionError:
            if temporary_name is not None:
                os.unlink(temporary_name)
            raise
        except OSError as error:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
            raise PendingStoreError("Unable to persist pending request.") from error

    def remove(self, request: PendingRequest) -> None:
        try:
            self._path(request.service, request.request_id).unlink(missing_ok=True)
        except OSError as error:
            raise PendingStoreError(
                "Unable to remove resolved pending request."
            ) from error

    def list(self, service: str | None = None) -> list[PendingRequest]:
        paths = (
            list(self._service_directory(service).glob("*.json"))
            if service is not None
            else list(self.root.glob("*/*.json"))
        )
        return sorted(
            (self._load_path(path) for path in paths), key=lambda item: item.created_at
        )

    def get(self, request_id: UUID) -> PendingRequest | None:
        matches = list(self.root.glob(f"*/{request_id}.json"))
        if not matches:
            return None
        if len(matches) > 1:
            raise PendingStoreError("Request UUID exists in multiple service profiles.")
        return self._load_path(matches[0])

    def _service_directory(self, service: str) -> Path:
        if not service or service in {".", ".."} or "/" in service or "\\" in service:
            raise PendingStoreError("Invalid service profile identifier.")
        directory = self.root / service
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        except OSError as error:
            raise PendingStoreError(
                "Unable to initialize service pending directory."
            ) from error
        return directory

    def _path(self, service: str, request_id: UUID) -> Path:
        return self._service_directory(service) / f"{request_id}.json"

    def _load_path(self, path: Path) -> PendingRequest:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            return PendingRequest(
                request_id=UUID(document["request_id"]),
                service=document["service"],
                operation=document["operation"],
                method=document["method"],
                path=document["path"],
                payload=document.get("payload"),
                created_at=document["created_at"],
            )
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise PendingStoreError(f"Invalid pending record: {path.name}") from error
