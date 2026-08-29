"""Batched commits, coalescing writers and branch-scoped transactions."""

from __future__ import annotations

import time
from collections.abc import Mapping
from types import TracebackType
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional, Type

from .documents import Document, with_metadata
from .errors import ConflictError, GitDbError, ValidationError
from .ids import new_id, validate_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import Collection, GitDb

__all__ = ["Batch", "Writer", "Transaction"]


class _Operation(NamedTuple):
    collection: str
    doc_id: str
    document: Optional[Document]


class _Expectation(NamedTuple):
    collection: str
    doc_id: str
    sha: Optional[str] = None
    rev: Optional[int] = None
    absent: bool = False


class Batch:
    """Queue many writes and flush them as a single Git commit.

    Uses the Git Data API: blobs -> tree -> commit -> ref update. The context
    manager commits on a clean exit and discards the queue when the block
    raises.

    Queued operations may carry preconditions (``expected_sha``/``expected_rev``
    or ``absent=True``). They are verified against the branch head immediately
    before the commit is built, so a batch fails cleanly instead of silently
    overwriting a concurrent change.
    """

    def __init__(self, db: GitDb, message: str) -> None:
        self.db = db
        self.message = message
        self._puts: Dict[str, _Operation] = {}
        self._deletes: Dict[str, _Operation] = {}
        self._expects: Dict[str, _Expectation] = {}
        self.committed_sha: Optional[str] = None

    # ------------------------------------------------------------ queue items
    def put(
        self,
        collection: str,
        doc_id: str,
        document: Mapping[str, Any],
        *,
        expected_sha: Optional[str] = None,
        expected_rev: Optional[int] = None,
        absent: bool = False,
    ) -> str:
        """Queue a create-or-replace for ``collection/doc_id``."""
        validate_id(doc_id)
        path = self.db.document_path(collection, doc_id)
        self._puts[path] = _Operation(collection, doc_id, with_metadata(doc_id, document, document))
        self._deletes.pop(path, None)
        self._record(path, collection, doc_id, expected_sha, expected_rev, absent)
        return doc_id

    def insert(self, collection: str, document: Mapping[str, Any]) -> str:
        """Queue a new document with a generated id and return that id."""
        return self.put(collection, new_id(), document)

    def delete(
        self,
        collection: str,
        doc_id: str,
        *,
        expected_sha: Optional[str] = None,
        expected_rev: Optional[int] = None,
    ) -> None:
        """Queue the removal of ``collection/doc_id``."""
        path = self.db.document_path(collection, doc_id)
        self._puts.pop(path, None)
        self._deletes[path] = _Operation(collection, doc_id, None)
        self._record(path, collection, doc_id, expected_sha, expected_rev, False)

    def expect(
        self,
        collection: str,
        doc_id: str,
        *,
        sha: Optional[str] = None,
        rev: Optional[int] = None,
        absent: bool = False,
    ) -> None:
        """Require a precondition for ``collection/doc_id`` before committing."""
        path = self.db.document_path(collection, doc_id)
        self._record(path, collection, doc_id, sha, rev, absent)

    def _record(
        self,
        path: str,
        collection: str,
        doc_id: str,
        sha: Optional[str],
        rev: Optional[int],
        absent: bool,
    ) -> None:
        if sha is None and rev is None and not absent:
            return
        self._expects[path] = _Expectation(collection, doc_id, sha=sha, rev=rev, absent=absent)

    @property
    def operations(self) -> int:
        return len(self._puts) + len(self._deletes)

    @property
    def expectations(self) -> int:
        return len(self._expects)

    # ---------------------------------------------------------------- commit
    def _verify(self) -> None:
        """Check every precondition against the current branch head."""
        for path, expected in self._expects.items():
            label = f"{expected.collection}/{expected.doc_id}"
            if expected.rev is not None:
                document, _ = self.db._read(path, fresh=True)
                current = document.get("_rev") if document else None
                if current != expected.rev:
                    raise ConflictError(
                        f"document {label} is at revision {current}, expected {expected.rev}"
                    )
                continue
            current_sha = self.db._sha(path, refresh=True)
            if expected.absent and current_sha is not None:
                raise ConflictError(f"document {label} already exists")
            if expected.sha is not None and current_sha != expected.sha:
                raise ConflictError(f"document {label} changed concurrently")

    def commit(self) -> Optional[str]:
        """Write every queued operation in one commit; return the commit sha."""
        if not self._puts and not self._deletes:
            return None
        self.db._assert_writable()

        puts: Dict[str, Mapping[str, Any]] = {}
        changes: Dict[str, Dict[str, Optional[Mapping[str, Any]]]] = {}
        for path, operation in self._puts.items():
            assert operation.document is not None  # noqa: S101 - puts always carry a document
            puts[path] = operation.document
            changes.setdefault(operation.collection, {})[operation.doc_id] = operation.document
        deletes = list(self._deletes)
        for operation in self._deletes.values():
            changes.setdefault(operation.collection, {})[operation.doc_id] = None
        for collection, collection_changes in changes.items():
            if self.db.config(collection).derived:
                puts.update(self.db._derived_documents(collection, collection_changes))

        result = self.db._commit(
            puts,
            deletes,
            message=self.message,
            verify=self._verify if self._expects else None,
        )
        self._puts.clear()
        self._deletes.clear()
        self._expects.clear()
        self.committed_sha = result.sha
        return result.sha

    def discard(self) -> None:
        """Drop every queued operation without writing anything."""
        self._puts.clear()
        self._deletes.clear()
        self._expects.clear()

    def __enter__(self) -> Batch:
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.discard()


class Writer:
    """Coalesce a stream of single-document writes into few commits.

    A burst of individual writes to the same branch is the main source of
    conflicts. The writer buffers operations and flushes them as one commit once
    ``max_operations`` is reached or ``max_seconds`` have passed since the last
    flush (checked whenever an operation is queued, and again on exit).
    """

    def __init__(
        self,
        db: GitDb,
        message: str = "gitdb writer",
        *,
        max_operations: int = 100,
        max_seconds: float = 5.0,
    ) -> None:
        if max_operations < 1:
            raise ValidationError("max_operations must be >= 1")
        self.db = db
        self.message = message
        self.max_operations = int(max_operations)
        self.max_seconds = float(max_seconds)
        self.commits: List[str] = []
        self._batch = db.batch(message)
        self._since = time.monotonic()

    @property
    def pending(self) -> int:
        return self._batch.operations

    def put(self, collection: str, doc_id: str, document: Mapping[str, Any]) -> str:
        result = self._batch.put(collection, doc_id, document)
        self._maybe_flush()
        return result

    def insert(self, collection: str, document: Mapping[str, Any]) -> str:
        doc_id = self._batch.insert(collection, document)
        self._maybe_flush()
        return doc_id

    def delete(self, collection: str, doc_id: str) -> None:
        self._batch.delete(collection, doc_id)
        self._maybe_flush()

    def _maybe_flush(self) -> None:
        if self._batch.operations >= self.max_operations:
            self.flush()
        elif self._batch.operations and time.monotonic() - self._since >= self.max_seconds:
            self.flush()

    def flush(self) -> Optional[str]:
        """Commit everything buffered so far; return the commit sha."""
        sha = self._batch.commit()
        self._since = time.monotonic()
        if sha is not None:
            self.commits.append(sha)
        return sha

    def __enter__(self) -> Writer:
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        if exc_type is None:
            self.flush()
        else:
            self._batch.discard()


class Transaction:
    """Stage several commits on a work branch, then publish them at once.

    Git cannot offer multi-commit transactions, but this comes close: every
    write inside the block lands on a throw-away branch and the target branch is
    only moved — in a single, non-forced ref update — when the block exits
    cleanly. On failure the work branch is deleted and the target branch never
    saw the intermediate states.
    """

    def __init__(
        self, db: GitDb, message: str = "gitdb transaction", *, branch: Optional[str] = None
    ) -> None:
        self.db = db
        self.message = message
        self.target = db.branch
        self.branch = branch or f"gitdb-tx-{new_id().lower()}"
        self.base_sha: Optional[str] = None
        self.head_sha: Optional[str] = None
        self._view: Optional[GitDb] = None
        self._open = False

    # ------------------------------------------------------------- lifecycle
    def start(self) -> Transaction:
        """Create the work branch and open the transaction."""
        if self._open:
            raise ValidationError("transaction is already open")
        self.db._assert_writable()
        self.base_sha = self.db.resolve_ref(refresh=True)
        self.db.client.request(
            "POST",
            f"/repos/{self.db.repo}/git/refs",
            json={"ref": f"refs/heads/{self.branch}", "sha": self.base_sha},
        )
        self._view = self.db.on_branch(self.branch)
        self._open = True
        return self

    @property
    def view(self) -> GitDb:
        """The :class:`~gitdb.client.GitDb` bound to the work branch."""
        if self._view is None:
            raise ValidationError("transaction is not open")
        return self._view

    def collection(self, name: str) -> Collection:
        """Return a collection writing to the work branch."""
        return self.view.collection(name)

    def batch(self, message: Optional[str] = None) -> Batch:
        """Return a batch writing to the work branch."""
        return self.view.batch(message or self.message)

    def _delete_branch(self) -> None:
        try:
            self.db.client.request("DELETE", f"/repos/{self.db.repo}/git/refs/heads/{self.branch}")
        except GitDbError:  # pragma: no cover - best effort cleanup
            pass

    def rollback(self) -> None:
        """Delete the work branch, discarding every staged commit."""
        if not self._open:
            return
        self._open = False
        self._delete_branch()

    def commit(self) -> Optional[str]:
        """Fast-forward the target branch to the work branch head."""
        if not self._open:
            raise ValidationError("transaction is not open")
        self._open = False
        head = self.view.resolve_ref(refresh=True)
        self.head_sha = head
        if head == self.base_sha:
            self._delete_branch()
            return None
        try:
            self.db.client.request(
                "PATCH",
                f"/repos/{self.db.repo}/git/refs/heads/{self.target}",
                json={"sha": head, "force": False},
            )
        except ConflictError as exc:
            # Keep the work branch so the staged commits can be merged by hand.
            raise ConflictError(
                f"{exc.message} - transaction kept on branch {self.branch!r}",
                status=exc.status,
            ) from exc
        self.db._resolved_ref = head
        self._delete_branch()
        return head

    def __enter__(self) -> Transaction:
        return self.start()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
