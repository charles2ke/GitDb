"""Core GitDb client: collections, documents and batched commits."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from datetime import datetime, timezone
from types import TracebackType
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
)

import requests

from .errors import AuthError, ConflictError, NotFoundError, ValidationError
from .http import DEFAULT_API_URL, DEFAULT_RAW_URL, GitHubClient
from .ids import new_id, validate_id, validate_name

__all__ = ["GitDb", "Collection", "Batch"]

Document = Dict[str, Any]
BLOB_MODE = "100644"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _encode(document: Mapping[str, Any]) -> str:
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _decode(content: str) -> Document:
    raw = base64.b64decode(content)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValidationError("stored document is not a JSON object")
    return data


class GitDb:
    """A document database backed by a GitHub repository.

    Parameters
    ----------
    repo:
        ``"owner/name"`` of the backing repository.
    token:
        A GitHub personal access token with ``contents:write`` permission.
        Optional when ``read_only`` is enabled.
    branch:
        Branch used for reads and writes.
    root:
        Directory inside the repository holding the collections.
    shard_depth / shard_width:
        When ``shard_depth`` is greater than zero, documents are nested in
        subdirectories built from the leading characters of their id, e.g.
        ``data/users/01/HZ/01HZ....json`` for ``shard_depth=2, shard_width=2``.
    conflict_retries:
        How often a write is retried after a sha conflict (refetching the sha
        each time). Set to ``0`` to disable automatic retries.
    """

    def __init__(
        self,
        repo: str,
        token: Optional[str] = None,
        *,
        branch: str = "main",
        root: str = "data",
        api_url: str = DEFAULT_API_URL,
        raw_url: str = DEFAULT_RAW_URL,
        session: Optional[requests.Session] = None,
        auth: Optional[Any] = None,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        timeout: float = 30.0,
        shard_depth: int = 0,
        shard_width: int = 2,
        conflict_retries: int = 2,
        cache: bool = True,
        read_only: bool = False,
        committer: Optional[Mapping[str, str]] = None,
        author: Optional[Mapping[str, str]] = None,
    ) -> None:
        if not isinstance(repo, str) or repo.count("/") != 1 or not all(repo.split("/")):
            raise ValidationError(f"repo must look like 'owner/name', got {repo!r}")
        if shard_depth < 0 or shard_width < 1:
            raise ValidationError("shard_depth must be >= 0 and shard_width >= 1")
        if token is None and auth is None and session is None and not read_only:
            raise AuthError("a token, auth or session is required unless read_only=True")

        self.repo = repo
        self.branch = branch
        self.root = root.strip("/")
        self.shard_depth = shard_depth
        self.shard_width = shard_width
        self.conflict_retries = max(0, int(conflict_retries))
        self.read_only = read_only
        self.raw_url = raw_url.rstrip("/")
        self.committer = dict(committer) if committer else None
        self.author = dict(author) if author else None
        self.client = GitHubClient(
            token,
            api_url=api_url,
            session=session,
            auth=auth,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            timeout=timeout,
        )
        self._cache_enabled = cache
        self._sha_cache: MutableMapping[str, str] = {}
        self._collections: Dict[str, Collection] = {}

    # ------------------------------------------------------------------ paths
    def collection(self, name: str) -> Collection:
        """Return (and memoize) the :class:`Collection` called ``name``."""
        validate_name(name)
        if name not in self._collections:
            self._collections[name] = Collection(self, name)
        return self._collections[name]

    def collection_path(self, collection: str) -> str:
        validate_name(collection)
        return f"{self.root}/{collection}" if self.root else collection

    def document_path(self, collection: str, doc_id: str) -> str:
        validate_id(doc_id)
        parts = [self.collection_path(collection)]
        for level in range(self.shard_depth):
            start = level * self.shard_width
            chunk = doc_id[start : start + self.shard_width]
            if not chunk:
                break
            parts.append(chunk)
        parts.append(f"{doc_id}.json")
        return "/".join(parts)

    @staticmethod
    def id_from_path(path: str) -> Optional[str]:
        name = path.rsplit("/", 1)[-1]
        return name[:-5] if name.endswith(".json") else None

    # ------------------------------------------------------------------ cache
    def invalidate(self, path: Optional[str] = None) -> None:
        """Drop cached blob shas, either for one path or for everything."""
        if path is None:
            self._sha_cache.clear()
        else:
            self._sha_cache.pop(path, None)

    def _cache_sha(self, path: str, sha: Optional[str]) -> None:
        if not self._cache_enabled:
            return
        if sha is None:
            self._sha_cache.pop(path, None)
        else:
            self._sha_cache[path] = sha

    # ------------------------------------------------------------------ reads
    def _read(self, path: str) -> Tuple[Optional[Document], Optional[str]]:
        """Return ``(document, sha)`` or ``(None, None)`` when absent."""
        if self.read_only:
            url = f"{self.raw_url}/{self.repo}/{self.branch}/{path}"
            response = self.client.request("GET", url, allow_404=True)
            if response.status_code == 404:
                return None, None
            data = response.json()
            if not isinstance(data, dict):
                raise ValidationError(f"stored document at {path} is not a JSON object")
            return data, None

        response = self.client.request(
            "GET",
            f"/repos/{self.repo}/contents/{path}",
            params={"ref": self.branch},
            allow_404=True,
        )
        if response.status_code == 404:
            self._cache_sha(path, None)
            return None, None
        payload = response.json()
        if isinstance(payload, list):
            raise ValidationError(f"{path} is a directory, not a document")
        sha = payload.get("sha")
        self._cache_sha(path, sha)
        return _decode(payload.get("content", "")), sha

    def _sha(self, path: str, *, refresh: bool = False) -> Optional[str]:
        if not refresh and self._cache_enabled and path in self._sha_cache:
            return self._sha_cache[path]
        _, sha = self._read(path)
        return sha

    # ----------------------------------------------------------------- writes
    def _assert_writable(self) -> None:
        if self.read_only:
            raise AuthError("this GitDb instance is read-only")

    def _commit_fields(self, message: str) -> Dict[str, Any]:
        body: Dict[str, Any] = {"message": message, "branch": self.branch}
        if self.committer:
            body["committer"] = self.committer
        if self.author:
            body["author"] = self.author
        return body

    def _write(
        self,
        path: str,
        document: Mapping[str, Any],
        *,
        sha: Optional[str],
        message: str,
    ) -> str:
        self._assert_writable()
        body = self._commit_fields(message)
        body["content"] = _encode(document)
        if sha:
            body["sha"] = sha
        response = self.client.request("PUT", f"/repos/{self.repo}/contents/{path}", json=body)
        new_sha = response.json().get("content", {}).get("sha")
        self._cache_sha(path, new_sha)
        return str(new_sha)

    def _remove(self, path: str, *, sha: str, message: str) -> None:
        self._assert_writable()
        body = self._commit_fields(message)
        body["sha"] = sha
        self.client.request("DELETE", f"/repos/{self.repo}/contents/{path}", json=body)
        self._cache_sha(path, None)

    # ------------------------------------------------------------------ trees
    def _list_paths(self, prefix: str) -> List[str]:
        """List every ``*.json`` path below ``prefix`` (Trees API, Contents fallback)."""
        try:
            payload = self.client.get_json(
                f"/repos/{self.repo}/git/trees/{self.branch}",
                params={"recursive": "1"},
            )
        except NotFoundError:
            return self._list_paths_via_contents(prefix)
        if payload.get("truncated"):
            return self._list_paths_via_contents(prefix)
        needle = f"{prefix}/"
        return sorted(
            entry["path"]
            for entry in payload.get("tree", [])
            if entry.get("type") == "blob"
            and entry.get("path", "").startswith(needle)
            and entry["path"].endswith(".json")
        )

    def _list_paths_via_contents(self, prefix: str) -> List[str]:
        found: List[str] = []
        pending = [prefix]
        while pending:
            current = pending.pop()
            response = self.client.request(
                "GET",
                f"/repos/{self.repo}/contents/{current}",
                params={"ref": self.branch},
                allow_404=True,
            )
            if response.status_code == 404:
                continue
            entries = response.json()
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if entry.get("type") == "dir":
                    pending.append(entry["path"])
                elif entry.get("type") == "file" and entry.get("path", "").endswith(".json"):
                    found.append(entry["path"])
                    self._cache_sha(entry["path"], entry.get("sha"))
        return sorted(found)

    # ---------------------------------------------------------------- history
    def history(self, collection: str, doc_id: str, *, limit: int = 30) -> List[Dict[str, Any]]:
        """Return the commit history of a single document, newest first."""
        path = self.document_path(collection, doc_id)
        commits = self.client.get_json(
            f"/repos/{self.repo}/commits",
            params={"path": path, "sha": self.branch, "per_page": min(limit, 100)},
        )
        history: List[Dict[str, Any]] = []
        for commit in commits[:limit]:
            info = commit.get("commit", {})
            history.append(
                {
                    "sha": commit.get("sha"),
                    "message": info.get("message"),
                    "author": info.get("author", {}).get("name"),
                    "date": info.get("author", {}).get("date"),
                    "url": commit.get("html_url"),
                }
            )
        return history

    # ------------------------------------------------------------------ batch
    def batch(self, message: str = "gitdb batch") -> Batch:
        """Return a :class:`Batch` writing every queued change in one commit."""
        self._assert_writable()
        return Batch(self, message)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> GitDb:
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"GitDb(repo={self.repo!r}, branch={self.branch!r}, root={self.root!r})"


def _with_metadata(
    doc_id: str,
    document: Mapping[str, Any],
    existing: Optional[Mapping[str, Any]] = None,
) -> Document:
    if not isinstance(document, Mapping):
        raise ValidationError("document must be a mapping")
    now = _utcnow()
    merged: Document = dict(document)
    merged["_id"] = doc_id
    merged["_created_at"] = (existing or {}).get("_created_at") or merged.get("_created_at") or now
    merged["_updated_at"] = now
    previous_rev = (existing or {}).get("_rev")
    merged["_rev"] = int(previous_rev) + 1 if isinstance(previous_rev, int) else 1
    return merged


class Collection:
    """A named set of JSON documents stored under ``{root}/{name}/``."""

    def __init__(self, db: GitDb, name: str) -> None:
        self.db = db
        self.name = validate_name(name)

    # ------------------------------------------------------------------ paths
    @property
    def path(self) -> str:
        return self.db.collection_path(self.name)

    def document_path(self, doc_id: str) -> str:
        return self.db.document_path(self.name, doc_id)

    # ------------------------------------------------------------------- CRUD
    def insert(
        self,
        document: Mapping[str, Any],
        *,
        id: Optional[str] = None,
        message: Optional[str] = None,
    ) -> str:
        """Create a new document and return its id.

        Raises :class:`~gitdb.errors.ConflictError` when the id already exists.
        """
        doc_id = validate_id(id) if id is not None else new_id()
        path = self.document_path(doc_id)
        payload = _with_metadata(doc_id, document)
        self.db._write(
            path,
            payload,
            sha=None,
            message=message or f"insert {self.name}/{doc_id}",
        )
        return doc_id

    def get(self, doc_id: str) -> Optional[Document]:
        """Return the document, or ``None`` when it does not exist."""
        document, _ = self.db._read(self.document_path(doc_id))
        return document

    def exists(self, doc_id: str) -> bool:
        return self.get(doc_id) is not None

    def replace(
        self,
        doc_id: str,
        document: Mapping[str, Any],
        *,
        message: Optional[str] = None,
    ) -> Document:
        """Overwrite a document wholesale (it must already exist)."""
        return self._modify(doc_id, document, merge=False, message=message)

    def update(
        self,
        doc_id: str,
        patch: Mapping[str, Any],
        *,
        message: Optional[str] = None,
    ) -> Document:
        """Shallow-merge ``patch`` into an existing document."""
        return self._modify(doc_id, patch, merge=True, message=message)

    def upsert(
        self,
        doc_id: str,
        document: Mapping[str, Any],
        *,
        message: Optional[str] = None,
    ) -> Document:
        """Update the document when it exists, otherwise create it."""
        try:
            return self._modify(doc_id, document, merge=True, message=message)
        except NotFoundError:
            self.insert(document, id=doc_id, message=message)
            result = self.get(doc_id)
            if result is None:  # pragma: no cover - defensive
                raise
            return result

    def _modify(
        self,
        doc_id: str,
        changes: Mapping[str, Any],
        *,
        merge: bool,
        message: Optional[str],
    ) -> Document:
        validate_id(doc_id)
        path = self.document_path(doc_id)
        attempts = self.db.conflict_retries + 1
        last_error: Optional[ConflictError] = None
        for _ in range(attempts):
            existing, sha = self.db._read(path)
            if existing is None or sha is None:
                raise NotFoundError(f"document {self.name}/{doc_id} does not exist")
            base = dict(existing) if merge else {}
            base.update(changes)
            payload = _with_metadata(doc_id, base, existing)
            try:
                self.db._write(
                    path,
                    payload,
                    sha=sha,
                    message=message or f"update {self.name}/{doc_id}",
                )
            except ConflictError as exc:
                last_error = exc
                self.db.invalidate(path)
                continue
            return payload
        raise last_error or ConflictError(f"could not write {self.name}/{doc_id}")

    def delete(self, doc_id: str, *, message: Optional[str] = None) -> None:
        """Delete a document, raising :class:`NotFoundError` when it is missing."""
        validate_id(doc_id)
        path = self.document_path(doc_id)
        attempts = self.db.conflict_retries + 1
        last_error: Optional[ConflictError] = None
        for _ in range(attempts):
            sha = self.db._sha(path, refresh=last_error is not None)
            if sha is None:
                raise NotFoundError(f"document {self.name}/{doc_id} does not exist")
            try:
                self.db._remove(path, sha=sha, message=message or f"delete {self.name}/{doc_id}")
            except ConflictError as exc:
                last_error = exc
                self.db.invalidate(path)
                continue
            return
        raise last_error or ConflictError(f"could not delete {self.name}/{doc_id}")

    # -------------------------------------------------------------- iteration
    def list(self, limit: Optional[int] = None) -> List[str]:
        """Return the sorted ids stored in this collection."""
        ids = [
            doc_id
            for doc_id in (GitDb.id_from_path(path) for path in self.db._list_paths(self.path))
            if doc_id is not None
        ]
        return ids[:limit] if limit is not None else ids

    def all(self, limit: Optional[int] = None) -> Iterator[Document]:
        """Yield every document in the collection (one API call per document)."""
        for doc_id in self.list(limit=limit):
            document = self.get(doc_id)
            if document is not None:
                yield document

    def find(
        self,
        predicate: Callable[[Document], bool],
        *,
        limit: Optional[int] = None,
    ) -> List[Document]:
        """Client-side filter over every document in the collection."""
        matches: List[Document] = []
        for document in self.all():
            if predicate(document):
                matches.append(document)
                if limit is not None and len(matches) >= limit:
                    break
        return matches

    def count(self) -> int:
        return len(self.list())

    def history(self, doc_id: str, *, limit: int = 30) -> List[Dict[str, Any]]:
        return self.db.history(self.name, doc_id, limit=limit)

    def __iter__(self) -> Iterator[Document]:
        return self.all()

    def __len__(self) -> int:
        return self.count()

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"Collection(name={self.name!r}, path={self.path!r})"


class Batch:
    """Queue many writes and flush them as a single Git commit.

    Uses the Git Data API: blobs -> tree -> commit -> ref update. The context
    manager commits on a clean exit and discards the queue when the block
    raises.
    """

    def __init__(self, db: GitDb, message: str) -> None:
        self.db = db
        self.message = message
        self._puts: Dict[str, Document] = {}
        self._deletes: List[str] = []
        self.committed_sha: Optional[str] = None

    # ------------------------------------------------------------ queue items
    def put(self, collection: str, doc_id: str, document: Mapping[str, Any]) -> str:
        """Queue a create-or-replace for ``collection/doc_id``."""
        validate_id(doc_id)
        path = self.db.document_path(collection, doc_id)
        self._puts[path] = _with_metadata(doc_id, document)
        if path in self._deletes:
            self._deletes.remove(path)
        return doc_id

    def insert(self, collection: str, document: Mapping[str, Any]) -> str:
        """Queue a new document with a generated id and return that id."""
        return self.put(collection, new_id(), document)

    def delete(self, collection: str, doc_id: str) -> None:
        """Queue the removal of ``collection/doc_id``."""
        path = self.db.document_path(collection, doc_id)
        self._puts.pop(path, None)
        if path not in self._deletes:
            self._deletes.append(path)

    @property
    def operations(self) -> int:
        return len(self._puts) + len(self._deletes)

    # ---------------------------------------------------------------- commit
    def commit(self) -> Optional[str]:
        """Write every queued operation in one commit; return the commit sha."""
        if not self._puts and not self._deletes:
            return None
        self.db._assert_writable()
        repo = self.db.repo
        client = self.db.client

        ref = client.get_json(f"/repos/{repo}/git/ref/heads/{self.db.branch}")
        base_commit = ref["object"]["sha"]
        base_tree = client.get_json(f"/repos/{repo}/git/commits/{base_commit}")["tree"]["sha"]

        entries: List[Dict[str, Any]] = []
        for path, document in self._puts.items():
            blob = client.request(
                "POST",
                f"/repos/{repo}/git/blobs",
                json={"content": _encode(document), "encoding": "base64"},
            ).json()
            entries.append({"path": path, "mode": BLOB_MODE, "type": "blob", "sha": blob["sha"]})
        for path in self._deletes:
            entries.append({"path": path, "mode": BLOB_MODE, "type": "blob", "sha": None})

        tree = client.request(
            "POST",
            f"/repos/{repo}/git/trees",
            json={"base_tree": base_tree, "tree": entries},
        ).json()
        commit = client.request(
            "POST",
            f"/repos/{repo}/git/commits",
            json={"message": self.message, "tree": tree["sha"], "parents": [base_commit]},
        ).json()
        client.request(
            "PATCH",
            f"/repos/{repo}/git/refs/heads/{self.db.branch}",
            json={"sha": commit["sha"], "force": False},
        )

        for path in self._touched_paths():
            self.db.invalidate(path)
        self._puts.clear()
        self._deletes.clear()
        self.committed_sha = str(commit["sha"])
        return self.committed_sha

    def _touched_paths(self) -> Iterable[str]:
        return list(self._puts) + list(self._deletes)

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
            self._puts.clear()
            self._deletes.clear()
