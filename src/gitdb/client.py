"""Core GitDb client: collections, documents, indexes and commits."""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from types import TracebackType
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from urllib.parse import quote

import requests

from .batch import Batch, Transaction, Writer
from .cache import Cache, CacheEntry, MemoryCache, NullCache
from .derived import (
    CollectionConfig,
    apply_index,
    apply_manifest,
    build_index,
    build_manifest,
    empty_index,
    empty_manifest,
    lookup_index,
    manifest_ids,
)
from .documents import (
    BLOB_MODE,
    Document,
    decode_document,
    dump_document,
    encode_document,
    loads_document,
    with_metadata,
)
from .errors import AuthError, ConflictError, GitDbError, NotFoundError, ValidationError
from .http import DEFAULT_API_URL, DEFAULT_RAW_URL, GitHubClient
from .ids import new_id, validate_id, validate_name
from .paths import PathResolver
from .ratelimit import RateLimit

__all__ = ["GitDb", "Collection", "Batch", "Writer", "Transaction", "TreeEntry", "Page"]

T = TypeVar("T")
R = TypeVar("R")

#: Documents larger than this are written through the Git Data API, which has no
#: practical size limit, instead of the Contents API (which rejects ~1 MB files).
CONTENTS_MAX_BYTES = 900_000

#: How many document paths to request per GraphQL round trip.
GRAPHQL_BATCH_SIZE = 50


class TreeEntry(NamedTuple):
    """A document blob discovered while listing a collection."""

    path: str
    sha: Optional[str]


class CommitResult(NamedTuple):
    """The outcome of a Git Data commit."""

    sha: str
    blobs: Dict[str, str]


class Page(NamedTuple):
    """One page of documents plus the cursor for the next page."""

    documents: List[Document]
    cursor: Optional[str]


def _as_config_map(
    value: Optional[Union[Mapping[str, Sequence[str]], Iterable[str]]],
) -> Dict[str, Tuple[str, ...]]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): tuple(fields) for key, fields in value.items()}
    return {str(name): () for name in value}


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
        How often a single-document write is retried after a sha conflict
        (refetching the sha each time). Set to ``0`` to disable.
    batch_retries:
        How often a batched commit is rebuilt and retried when another writer
        moved the branch in the meantime.
    concurrency:
        Size of the thread pool used to fetch many documents at once. ``1``
        keeps every read strictly sequential.
    indexes:
        ``{"users": ["email"]}`` — secondary index fields maintained per
        collection. Indexed writes go through the Git Data API so the document
        and its indexes land in one commit.
    manifests:
        ``{"users": ["name"]}`` or ``["users"]`` — collections that keep a
        manifest of ids (plus the listed projected fields) so ``list()`` and
        ``count()`` cost a single request.
    use_graphql:
        Fetch documents in bulk through the GraphQL API (needs a token).
    ref:
        Pin every read to an immutable commit sha (implies ``read_only``).
    pin_ref:
        In read-only mode, resolve the branch to a commit sha once and read
        commit-pinned ``raw.githubusercontent.com`` URLs, which are immutable
        and therefore never serve a stale document.
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
        batch_retries: int = 2,
        cache: Union[bool, Cache] = True,
        read_only: bool = False,
        committer: Optional[Mapping[str, str]] = None,
        author: Optional[Mapping[str, str]] = None,
        concurrency: int = 1,
        indexes: Optional[Mapping[str, Sequence[str]]] = None,
        manifests: Optional[Union[Mapping[str, Sequence[str]], Iterable[str]]] = None,
        use_graphql: bool = False,
        graphql_batch_size: int = GRAPHQL_BATCH_SIZE,
        contents_max_bytes: int = CONTENTS_MAX_BYTES,
        pace_requests: bool = True,
        ref: Optional[str] = None,
        pin_ref: bool = True,
    ) -> None:
        if not isinstance(repo, str) or repo.count("/") != 1 or not all(repo.split("/")):
            raise ValidationError(f"repo must look like 'owner/name', got {repo!r}")
        if ref is not None:
            read_only = True
        if token is None and auth is None and session is None and not read_only:
            raise AuthError("a token, auth or session is required unless read_only=True")

        self.repo = repo
        self.branch = branch
        self.paths = PathResolver(root, shard_depth=shard_depth, shard_width=shard_width)
        self.conflict_retries = max(0, int(conflict_retries))
        self.batch_retries = max(0, int(batch_retries))
        self.read_only = read_only
        self.ref = ref
        self.pin_ref = pin_ref
        self.raw_url = raw_url.rstrip("/")
        self.committer = dict(committer) if committer else None
        self.author = dict(author) if author else None
        self.concurrency = max(1, int(concurrency))
        self.use_graphql = use_graphql
        self.graphql_batch_size = max(1, int(graphql_batch_size))
        self.contents_max_bytes = max(1, int(contents_max_bytes))
        self.indexes = _as_config_map(indexes)
        self.manifests = _as_config_map(manifests)
        self.client = GitHubClient(
            token,
            api_url=api_url,
            session=session,
            auth=auth,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            timeout=timeout,
            pace_requests=pace_requests,
        )
        self.cache: Cache = _make_cache(cache)
        self._collections: Dict[str, Collection] = {}
        self._executor: Optional[ThreadPoolExecutor] = None
        self._resolved_ref: Optional[str] = ref
        self._owns_client = True

    # ------------------------------------------------------------------ paths
    @property
    def root(self) -> str:
        return self.paths.root

    @property
    def shard_depth(self) -> int:
        return self.paths.shard_depth

    @property
    def shard_width(self) -> int:
        return self.paths.shard_width

    def collection(self, name: str) -> Collection:
        """Return (and memoize) the :class:`Collection` called ``name``."""
        validate_name(name)
        if name not in self._collections:
            self._collections[name] = Collection(self, name)
        return self._collections[name]

    def collection_path(self, collection: str) -> str:
        return self.paths.collection_path(collection)

    def document_path(self, collection: str, doc_id: str) -> str:
        return self.paths.document_path(collection, doc_id)

    def index_path(self, collection: str, field: str) -> str:
        return self.paths.index_path(collection, field)

    def manifest_path(self, collection: str) -> str:
        return self.paths.manifest_path(collection)

    @staticmethod
    def id_from_path(path: str) -> Optional[str]:
        return PathResolver.id_from_path(path)

    def config(self, collection: str) -> CollectionConfig:
        """Return the derived-file configuration of ``collection``."""
        return CollectionConfig(
            collection,
            indexes=self.indexes.get(collection, ()),
            manifest=collection in self.manifests,
            manifest_fields=self.manifests.get(collection, ()),
        )

    # ------------------------------------------------------------------ views
    @property
    def read_ref(self) -> str:
        """The git ref used for reads: the pinned commit sha or the branch."""
        return self.ref or self.branch

    def _view(
        self,
        *,
        branch: Optional[str] = None,
        ref: Optional[str] = None,
        read_only: Optional[bool] = None,
    ) -> GitDb:
        """Return a lightweight clone sharing the HTTP client but not the cache."""
        clone = object.__new__(GitDb)
        clone.__dict__.update(self.__dict__)
        clone._collections = {}
        clone.cache = MemoryCache() if not isinstance(self.cache, NullCache) else NullCache()
        clone._owns_client = False
        if branch is not None:
            clone.branch = branch
            clone.ref = None
            clone._resolved_ref = None
        if ref is not None:
            clone.ref = ref
            clone._resolved_ref = ref
        if read_only is not None:
            clone.read_only = read_only
        return clone

    def at(self, commit_sha: str) -> GitDb:
        """Return a read-only view pinned to ``commit_sha``.

        Pinned views read immutable content, which makes them reproducible and
        immune to the caching lag of ``raw.githubusercontent.com``.
        """
        if not commit_sha:
            raise ValidationError("commit sha must not be empty")
        return self._view(ref=commit_sha, read_only=True)

    def snapshot(self) -> GitDb:
        """Resolve the branch head and return a read-only view pinned to it."""
        return self.at(self.resolve_ref(refresh=True))

    def on_branch(self, branch: str) -> GitDb:
        """Return a view of the same repository on another branch."""
        return self._view(branch=branch)

    def resolve_ref(self, *, refresh: bool = False) -> str:
        """Return the commit sha the branch currently points at."""
        if self.ref:
            return self.ref
        if self._resolved_ref is None or refresh:
            payload = self.client.get_json(f"/repos/{self.repo}/git/ref/heads/{self.branch}")
            self._resolved_ref = str(payload["object"]["sha"])
        return self._resolved_ref

    def _raw_ref(self) -> str:
        """Return the ref used to build raw urls, pinned to a commit when possible."""
        if self.ref:
            return self.ref
        if not self.pin_ref:
            return self.branch
        try:
            return self.resolve_ref()
        except GitDbError:
            # Falling back to the branch keeps read-only mode usable even when
            # the API is unreachable; reads are then subject to raw caching lag.
            return self.branch

    # ------------------------------------------------------------ rate limits
    def rate_limit(self, resource: str = "core") -> RateLimit:
        """Return the current API quota (``GET /rate_limit`` costs no quota)."""
        return self.client.rate_limit(resource)

    # ------------------------------------------------------------------ cache
    def invalidate(self, path: Optional[str] = None) -> None:
        """Drop cached shas/ETags, either for one path or for everything."""
        if path is None:
            self.cache.clear()
        else:
            self.cache.delete(path)

    @property
    def _sha_cache(self) -> Dict[str, str]:
        """The cached blob shas (introspection helper)."""
        return {
            path: entry.sha
            for path, entry in self.cache.snapshot().items()
            if entry.sha is not None
        }

    def _store(
        self,
        path: str,
        *,
        sha: Optional[str] = None,
        etag: Optional[str] = None,
        document: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.cache.set(
            path, CacheEntry(sha=sha, etag=etag, document=dict(document) if document else None)
        )

    # ------------------------------------------------------------------ reads
    def _read(self, path: str, *, fresh: bool = False) -> Tuple[Optional[Document], Optional[str]]:
        """Return ``(document, sha)`` or ``(None, None)`` when absent."""
        if self.read_only and not fresh:
            return self._read_raw(path)
        return self._read_contents(path)

    def _read_raw(self, path: str) -> Tuple[Optional[Document], Optional[str]]:
        entry = self.cache.get(path)
        url = f"{self.raw_url}/{self.repo}/{self._raw_ref()}/{path}"
        headers: Dict[str, str] = {}
        if entry is not None and entry.etag and entry.document is not None:
            headers["If-None-Match"] = entry.etag
        response = self.client.request("GET", url, headers=headers or None, allow_404=True)
        if response.status_code == 304 and entry is not None and entry.document is not None:
            return dict(entry.document), entry.sha
        if response.status_code == 404:
            self.cache.delete(path)
            return None, None
        document = loads_document(response.content, path)
        self._store(path, etag=response.headers.get("ETag"), document=document)
        return document, None

    def _read_contents(self, path: str) -> Tuple[Optional[Document], Optional[str]]:
        entry = self.cache.get(path)
        headers: Dict[str, str] = {}
        if entry is not None and entry.etag and entry.document is not None:
            headers["If-None-Match"] = entry.etag
        response = self.client.request(
            "GET",
            f"/repos/{self.repo}/contents/{path}",
            params={"ref": self.read_ref},
            headers=headers or None,
            allow_404=True,
        )
        if response.status_code == 304 and entry is not None and entry.document is not None:
            return dict(entry.document), entry.sha
        if response.status_code == 404:
            self.cache.delete(path)
            return None, None
        payload = response.json()
        if isinstance(payload, list):
            raise ValidationError(f"{path} is a directory, not a document")
        sha = payload.get("sha")
        document = self._document_from_contents(payload, path)
        self._store(path, sha=sha, etag=response.headers.get("ETag"), document=document)
        return document, sha

    def _document_from_contents(self, payload: Mapping[str, Any], path: str) -> Document:
        """Decode a Contents API payload, falling back to the blob for big files."""
        content = payload.get("content")
        if payload.get("encoding") == "base64" and isinstance(content, str) and content.strip():
            return decode_document(content, path)
        sha = payload.get("sha")
        if isinstance(sha, str) and sha:
            # Files above ~1 MB come back with an empty body and encoding "none".
            return self._read_blob(sha, path)
        raise ValidationError(f"stored document at {path} has no readable content")

    def _read_blob(self, sha: str, path: Optional[str] = None) -> Document:
        """Fetch one blob by sha using the raw media type (no size limit)."""
        response = self.client.request(
            "GET",
            f"/repos/{self.repo}/git/blobs/{sha}",
            headers={"Accept": "application/vnd.github.raw"},
        )
        document = loads_document(response.content, path)
        if (
            document.get("sha") == sha
            and document.get("encoding") == "base64"
            and isinstance(document.get("content"), str)
        ):
            # The endpoint ignored the raw media type and returned the envelope.
            return decode_document(document["content"], path)
        return document

    def _sha(self, path: str, *, refresh: bool = False) -> Optional[str]:
        if not refresh:
            entry = self.cache.get(path)
            if entry is not None and entry.sha is not None:
                return entry.sha
        _, sha = self._read_contents(path)
        return sha

    def _read_many(self, entries: Sequence[TreeEntry]) -> Dict[str, Document]:
        """Fetch many documents, reusing cached bodies and batching where possible."""
        results: Dict[str, Document] = {}
        pending: List[TreeEntry] = []
        for entry in entries:
            cached = self.cache.get(entry.path)
            if (
                cached is not None
                and cached.document is not None
                and entry.sha is not None
                and cached.sha == entry.sha
            ):
                results[entry.path] = dict(cached.document)
            else:
                pending.append(entry)

        if pending and self.use_graphql and not self.read_only:
            fetched = self._graphql_documents([entry.path for entry in pending])
            results.update(fetched)
            pending = [entry for entry in pending if entry.path not in fetched]

        for path, document in self._map(self._fetch_one, pending):
            if document is not None:
                results[path] = document
        return results

    def _fetch_one(self, entry: TreeEntry) -> Tuple[str, Optional[Document]]:
        if entry.sha and not self.read_only:
            blob = self._read_blob(entry.sha, entry.path)
            self._store(entry.path, sha=entry.sha, document=blob)
            return entry.path, blob
        document, _ = self._read(entry.path)
        return entry.path, document

    def _map(self, function: Callable[[T], R], items: Sequence[T]) -> List[R]:
        if self.concurrency > 1 and len(items) > 1:
            return list(self._pool().map(function, items))
        return [function(item) for item in items]

    def _pool(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self.concurrency)
        return self._executor

    def _graphql_documents(self, paths: Sequence[str]) -> Dict[str, Document]:
        """Fetch many documents with a handful of GraphQL round trips."""
        owner, name = self.repo.split("/")
        found: Dict[str, Document] = {}
        for start in range(0, len(paths), self.graphql_batch_size):
            chunk = list(paths[start : start + self.graphql_batch_size])
            aliases = {f"d{index}": path for index, path in enumerate(chunk)}
            selections = "\n".join(
                f'{alias}: object(expression: "{self.read_ref}:{path}")'
                " { ... on Blob { text oid } }"
                for alias, path in aliases.items()
            )
            query = (
                "query($owner: String!, $name: String!) {"
                f" repository(owner: $owner, name: $name) {{ {selections} }} }}"
            )
            data = self.client.graphql(query, {"owner": owner, "name": name})
            repository = (data or {}).get("repository") or {}
            for alias, path in aliases.items():
                blob = repository.get(alias)
                if not isinstance(blob, Mapping):
                    continue
                text = blob.get("text")
                if not isinstance(text, str):
                    continue
                document = loads_document(text, path)
                sha = blob.get("oid")
                self._store(path, sha=sha if isinstance(sha, str) else None, document=document)
                found[path] = document
        return found

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
        """Write one document and return its new blob sha."""
        self._assert_writable()
        payload = dump_document(document)
        if len(payload) > self.contents_max_bytes:
            # The Contents API rejects large files; the Git Data API does not.
            result = self._commit({path: document}, (), message=message)
            return result.blobs[path]
        body = self._commit_fields(message)
        body["content"] = encode_document(document)
        if sha:
            body["sha"] = sha
        response = self.client.request("PUT", f"/repos/{self.repo}/contents/{path}", json=body)
        new_sha = response.json().get("content", {}).get("sha")
        self._store(path, sha=new_sha, document=document)
        return str(new_sha)

    def _remove(self, path: str, *, sha: str, message: str) -> None:
        self._assert_writable()
        body = self._commit_fields(message)
        body["sha"] = sha
        self.client.request("DELETE", f"/repos/{self.repo}/contents/{path}", json=body)
        self.cache.delete(path)

    def _create_blob(self, document: Mapping[str, Any]) -> str:
        response = self.client.request(
            "POST",
            f"/repos/{self.repo}/git/blobs",
            json={"content": encode_document(document), "encoding": "base64"},
        )
        return str(response.json()["sha"])

    def _commit(
        self,
        puts: Mapping[str, Mapping[str, Any]],
        deletes: Sequence[str] = (),
        *,
        message: str,
        verify: Optional[Callable[[], None]] = None,
        retries: Optional[int] = None,
    ) -> CommitResult:
        """Write ``puts``/``deletes`` as a single commit (blobs → tree → commit → ref).

        The ref is updated without ``force``, so a concurrent push makes the
        commit fail instead of silently overwriting history. When that happens
        the tree is rebuilt on the new head and retried up to ``retries`` times.
        """
        self._assert_writable()
        if verify is not None:
            # Check preconditions before uploading anything so a doomed commit
            # fails without leaving orphan blobs behind.
            verify()
        # Blobs are content addressed, so they survive a rebuild and are uploaded once.
        blobs = {path: self._create_blob(document) for path, document in puts.items()}
        entries: List[Dict[str, Any]] = [
            {"path": path, "mode": BLOB_MODE, "type": "blob", "sha": blobs[path]} for path in puts
        ]
        entries.extend(
            {"path": path, "mode": BLOB_MODE, "type": "blob", "sha": None} for path in deletes
        )

        attempts = (self.batch_retries if retries is None else max(0, int(retries))) + 1
        last_error: Optional[ConflictError] = None
        for attempt in range(attempts):
            if verify is not None and attempt:
                # Re-check against the head this attempt is about to build on.
                verify()
            base_commit = self.resolve_ref(refresh=True)
            base_tree = self.client.get_json(f"/repos/{self.repo}/git/commits/{base_commit}")[
                "tree"
            ]["sha"]
            tree = self.client.request(
                "POST",
                f"/repos/{self.repo}/git/trees",
                json={"base_tree": base_tree, "tree": entries},
            ).json()
            commit_body: Dict[str, Any] = {
                "message": message,
                "tree": tree["sha"],
                "parents": [base_commit],
            }
            if self.author:
                commit_body["author"] = self.author
            if self.committer:
                commit_body["committer"] = self.committer
            commit = self.client.request(
                "POST", f"/repos/{self.repo}/git/commits", json=commit_body
            ).json()
            try:
                self.client.request(
                    "PATCH",
                    f"/repos/{self.repo}/git/refs/heads/{self.branch}",
                    json={"sha": commit["sha"], "force": False},
                )
            except ConflictError as exc:
                last_error = exc
                self._resolved_ref = None
                if attempt >= attempts - 1:
                    break
                time.sleep(self.client._sleep_for(attempt, None))
                continue

            commit_sha = str(commit["sha"])
            self._resolved_ref = commit_sha
            for path, blob_sha in blobs.items():
                self._store(path, sha=blob_sha, document=puts[path])
            for path in deletes:
                self.cache.delete(path)
            return CommitResult(commit_sha, dict(blobs))
        raise last_error or ConflictError("could not update the branch ref")

    # ---------------------------------------------------------------- derived
    def _derived_documents(
        self,
        collection: str,
        changes: Mapping[str, Optional[Mapping[str, Any]]],
    ) -> Dict[str, Document]:
        """Return the index/manifest blobs that must accompany ``changes``."""
        config = self.config(collection)
        updates: Dict[str, Document] = {}
        for field in config.indexes:
            path = self.index_path(collection, field)
            current, _ = self._read(path)
            updates[path] = apply_index(
                current or empty_index(collection, field), collection, field, changes
            )
        if config.manifest:
            path = self.manifest_path(collection)
            current, _ = self._read(path)
            updates[path] = apply_manifest(
                current or empty_manifest(collection),
                collection,
                changes,
                config.manifest_fields,
            )
        return updates

    def reindex(self, collection: str, *, message: Optional[str] = None) -> Optional[str]:
        """Rebuild every index and manifest of ``collection`` in one commit."""
        self._assert_writable()
        config = self.config(collection)
        if not config.derived:
            return None
        documents = {
            str(document.get("_id")): document
            for document in self.collection(collection).all()
            if document.get("_id")
        }
        updates: Dict[str, Document] = {}
        for field in config.indexes:
            updates[self.index_path(collection, field)] = build_index(collection, field, documents)
        if config.manifest:
            updates[self.manifest_path(collection)] = build_manifest(
                collection, documents, config.manifest_fields
            )
        result = self._commit(updates, message=message or f"reindex {collection}")
        return result.sha

    # ------------------------------------------------------------------ trees
    def _tree(self, expression: str, *, recursive: bool = True) -> Dict[str, Any]:
        payload = self.client.get_json(
            f"/repos/{self.repo}/git/trees/{quote(expression, safe='')}",
            params={"recursive": "1"} if recursive else None,
        )
        return payload if isinstance(payload, dict) else {}

    def _list_entries(self, prefix: str) -> List[TreeEntry]:
        """Return every ``*.json`` blob below ``prefix``, with its sha."""
        entries = self._scoped_entries(prefix)
        if entries is None:
            entries = self._repository_entries(prefix)
        if entries is None:
            entries = self._contents_entries(prefix)
        for entry in entries:
            if entry.sha:
                self._store(entry.path, sha=entry.sha)
        return sorted(entries, key=lambda entry: entry.path)

    def _scoped_entries(self, prefix: str) -> Optional[List[TreeEntry]]:
        """List the collection subtree only — far smaller than the whole repo tree.

        Returns ``None`` when the subtree could not be read (a missing path and
        a server that does not understand the path-scoped expression look the
        same), so the caller can fall back to a listing that is authoritative.
        """
        try:
            payload = self._tree(f"{self.read_ref}:{prefix}")
        except GitDbError:
            return None
        if payload.get("truncated"):
            return self._descend(prefix)
        return [
            TreeEntry(f"{prefix}/{entry['path']}", entry.get("sha"))
            for entry in payload.get("tree", [])
            if entry.get("type") == "blob" and str(entry.get("path", "")).endswith(".json")
        ]

    def _descend(self, prefix: str) -> List[TreeEntry]:
        """Walk a truncated tree one shard at a time instead of listing everything."""
        try:
            payload = self._tree(f"{self.read_ref}:{prefix}", recursive=False)
        except GitDbError:
            return self._contents_entries(prefix)
        if payload.get("truncated"):
            return self._contents_entries(prefix)
        found: List[TreeEntry] = []
        for entry in payload.get("tree", []):
            name = str(entry.get("path", ""))
            if entry.get("type") == "blob" and name.endswith(".json"):
                found.append(TreeEntry(f"{prefix}/{name}", entry.get("sha")))
            elif entry.get("type") == "tree" and name:
                nested = self._scoped_entries(f"{prefix}/{name}")
                found.extend(
                    nested if nested is not None else self._contents_entries(f"{prefix}/{name}")
                )
        return found

    def _repository_entries(self, prefix: str) -> Optional[List[TreeEntry]]:
        """Fall back to the whole-repository tree (older GitHub Enterprise)."""
        try:
            payload = self._tree(self.read_ref)
        except GitDbError:
            return None
        if payload.get("truncated"):
            return None
        needle = f"{prefix}/"
        return [
            TreeEntry(str(entry["path"]), entry.get("sha"))
            for entry in payload.get("tree", [])
            if entry.get("type") == "blob"
            and str(entry.get("path", "")).startswith(needle)
            and str(entry["path"]).endswith(".json")
        ]

    def _contents_entries(self, prefix: str) -> List[TreeEntry]:
        found: List[TreeEntry] = []
        pending = [prefix]
        while pending:
            current = pending.pop()
            response = self.client.request(
                "GET",
                f"/repos/{self.repo}/contents/{current}",
                params={"ref": self.read_ref},
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
                elif entry.get("type") == "file" and str(entry.get("path", "")).endswith(".json"):
                    found.append(TreeEntry(str(entry["path"]), entry.get("sha")))
        return found

    def _list_paths(self, prefix: str) -> List[str]:
        """Return every ``*.json`` path below ``prefix``."""
        return [entry.path for entry in self._list_entries(prefix)]

    # ---------------------------------------------------------------- history
    def history(self, collection: str, doc_id: str, *, limit: int = 30) -> List[Dict[str, Any]]:
        """Return the commit history of a single document, newest first."""
        path = self.document_path(collection, doc_id)
        commits = self.client.get_json(
            f"/repos/{self.repo}/commits",
            params={"path": path, "sha": self.read_ref, "per_page": min(limit, 100)},
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

    def _read_at(self, path: str, commit_sha: str) -> Optional[Document]:
        """Read a path as it was at ``commit_sha`` (bypasses the cache)."""
        response = self.client.request(
            "GET",
            f"/repos/{self.repo}/contents/{path}",
            params={"ref": commit_sha},
            allow_404=True,
        )
        if response.status_code == 404:
            return None
        payload = response.json()
        if isinstance(payload, list):
            raise ValidationError(f"{path} is a directory, not a document")
        return self._document_from_contents(payload, path)

    def revert(self, commit_sha: str, *, message: Optional[str] = None) -> Optional[str]:
        """Undo ``commit_sha`` by committing its inverse (history is never rewritten)."""
        self._assert_writable()
        commit = self.client.get_json(f"/repos/{self.repo}/commits/{commit_sha}")
        parents = commit.get("parents") or []
        if not parents:
            raise ValidationError(f"commit {commit_sha} has no parent to revert to")
        parent = str(parents[0]["sha"])
        puts: Dict[str, Document] = {}
        deletes: List[str] = []
        for changed in commit.get("files") or []:
            path = str(changed.get("filename", ""))
            if not path.endswith(".json") or (self.root and not path.startswith(f"{self.root}/")):
                continue
            previous = self._read_at(path, parent)
            if previous is None:
                deletes.append(path)
            else:
                puts[path] = previous
        if not puts and not deletes:
            return None
        result = self._commit(puts, deletes, message=message or f"revert {commit_sha[:7]}")
        return result.sha

    # ------------------------------------------------------- batching helpers
    def batch(self, message: str = "gitdb batch") -> Batch:
        """Return a :class:`Batch` writing every queued change in one commit."""
        self._assert_writable()
        return Batch(self, message)

    def writer(
        self,
        message: str = "gitdb writer",
        *,
        max_operations: int = 100,
        max_seconds: float = 5.0,
    ) -> Writer:
        """Return a :class:`Writer` that coalesces many writes into few commits."""
        self._assert_writable()
        return Writer(self, message, max_operations=max_operations, max_seconds=max_seconds)

    def transaction(
        self,
        message: str = "gitdb transaction",
        *,
        branch: Optional[str] = None,
    ) -> Transaction:
        """Return a :class:`Transaction` staging several commits on a work branch."""
        self._assert_writable()
        return Transaction(self, message, branch=branch)

    # ------------------------------------------------------------ maintenance
    def compact(self, *, message: str = "gitdb compaction", confirm: bool = False) -> str:
        """Squash the whole branch history into a single commit.

        This **rewrites history** and force-updates the branch, so every clone
        and open pull request against it is invalidated. Pass ``confirm=True``
        to acknowledge that.
        """
        self._assert_writable()
        if not confirm:
            raise ValidationError(
                "compact() rewrites history and force-updates the branch; "
                "call compact(confirm=True) to proceed"
            )
        head = self.resolve_ref(refresh=True)
        tree_sha = self.client.get_json(f"/repos/{self.repo}/git/commits/{head}")["tree"]["sha"]
        body: Dict[str, Any] = {"message": message, "tree": tree_sha, "parents": []}
        if self.author:
            body["author"] = self.author
        if self.committer:
            body["committer"] = self.committer
        commit = self.client.request("POST", f"/repos/{self.repo}/git/commits", json=body).json()
        self.client.request(
            "PATCH",
            f"/repos/{self.repo}/git/refs/heads/{self.branch}",
            json={"sha": commit["sha"], "force": True},
        )
        self._resolved_ref = str(commit["sha"])
        self.invalidate()
        return self._resolved_ref

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
        if self._owns_client:
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


def _make_cache(cache: Union[bool, Cache]) -> Cache:
    if cache is True:
        return MemoryCache()
    if cache is False:
        return NullCache()
    if isinstance(cache, Cache):
        return cache
    raise ValidationError("cache must be True, False or a Cache instance")


class Collection:
    """A named set of JSON documents stored under ``{root}/{name}/``."""

    def __init__(self, db: GitDb, name: str) -> None:
        self.db = db
        self.name = validate_name(name)

    # ------------------------------------------------------------------ paths
    @property
    def path(self) -> str:
        return self.db.collection_path(self.name)

    @property
    def config(self) -> CollectionConfig:
        return self.db.config(self.name)

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
        payload = with_metadata(doc_id, document)
        commit_message = message or f"insert {self.name}/{doc_id}"
        if self.config.derived:
            self._commit_document(doc_id, payload, message=commit_message, expect_absent=True)
        else:
            self.db._write(path, payload, sha=None, message=commit_message)
        return doc_id

    def get(self, doc_id: str, *, fresh: bool = False) -> Optional[Document]:
        """Return the document, or ``None`` when it does not exist.

        ``fresh=True`` bypasses ``raw.githubusercontent.com`` in read-only mode
        and reads through the API instead, which is never cached.
        """
        document, _ = self.db._read(self.document_path(doc_id), fresh=fresh)
        return document

    def exists(self, doc_id: str) -> bool:
        return self.get(doc_id) is not None

    def replace(
        self,
        doc_id: str,
        document: Mapping[str, Any],
        *,
        message: Optional[str] = None,
        expected_rev: Optional[int] = None,
    ) -> Document:
        """Overwrite a document wholesale (it must already exist)."""
        return self._modify(
            doc_id, document, merge=False, message=message, expected_rev=expected_rev
        )

    def update(
        self,
        doc_id: str,
        patch: Mapping[str, Any],
        *,
        message: Optional[str] = None,
        expected_rev: Optional[int] = None,
    ) -> Document:
        """Shallow-merge ``patch`` into an existing document.

        Pass ``expected_rev`` to make the write a compare-and-set: the document
        is only written when its ``_rev`` still matches.
        """
        return self._modify(doc_id, patch, merge=True, message=message, expected_rev=expected_rev)

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
        expected_rev: Optional[int] = None,
    ) -> Document:
        validate_id(doc_id)
        path = self.document_path(doc_id)
        # A compare-and-set must not be retried: the caller asked for this exact revision.
        attempts = 1 if expected_rev is not None else self.db.conflict_retries + 1
        last_error: Optional[ConflictError] = None
        for _ in range(attempts):
            existing, sha = self.db._read(path)
            if existing is None or sha is None:
                raise NotFoundError(f"document {self.name}/{doc_id} does not exist")
            _check_rev(self.name, doc_id, existing, expected_rev)
            base = dict(existing) if merge else {}
            base.update(changes)
            payload = with_metadata(doc_id, base, existing)
            commit_message = message or f"update {self.name}/{doc_id}"
            try:
                if self.config.derived:
                    self._commit_document(doc_id, payload, message=commit_message, expect_sha=sha)
                else:
                    self.db._write(path, payload, sha=sha, message=commit_message)
            except ConflictError as exc:
                last_error = exc
                self.db.invalidate(path)
                continue
            return payload
        raise last_error or ConflictError(f"could not write {self.name}/{doc_id}")

    def delete(
        self,
        doc_id: str,
        *,
        message: Optional[str] = None,
        expected_rev: Optional[int] = None,
    ) -> None:
        """Delete a document, raising :class:`NotFoundError` when it is missing."""
        validate_id(doc_id)
        path = self.document_path(doc_id)
        commit_message = message or f"delete {self.name}/{doc_id}"
        if expected_rev is not None or self.config.derived:
            existing, sha = self.db._read(path)
            if existing is None or sha is None:
                raise NotFoundError(f"document {self.name}/{doc_id} does not exist")
            _check_rev(self.name, doc_id, existing, expected_rev)
            if self.config.derived:
                self._commit_document(doc_id, None, message=commit_message, expect_sha=sha)
            else:
                self.db._remove(path, sha=sha, message=commit_message)
            return

        attempts = self.db.conflict_retries + 1
        last_error: Optional[ConflictError] = None
        for _ in range(attempts):
            sha = self.db._sha(path, refresh=last_error is not None)
            if sha is None:
                raise NotFoundError(f"document {self.name}/{doc_id} does not exist")
            try:
                self.db._remove(path, sha=sha, message=commit_message)
            except ConflictError as exc:
                last_error = exc
                self.db.invalidate(path)
                continue
            return
        raise last_error or ConflictError(f"could not delete {self.name}/{doc_id}")

    def _commit_document(
        self,
        doc_id: str,
        document: Optional[Document],
        *,
        message: str,
        expect_sha: Optional[str] = None,
        expect_absent: bool = False,
    ) -> None:
        """Write a document and its derived index/manifest files in one commit."""
        path = self.document_path(doc_id)
        changes: Dict[str, Optional[Mapping[str, Any]]] = {doc_id: document}
        puts: Dict[str, Mapping[str, Any]] = dict(self.db._derived_documents(self.name, changes))
        deletes: List[str] = []
        if document is None:
            deletes.append(path)
        else:
            puts[path] = document

        def verify() -> None:
            current = self.db._sha(path, refresh=True)
            if expect_absent and current is not None:
                raise ConflictError(f"document {self.name}/{doc_id} already exists")
            if expect_sha is not None and current != expect_sha:
                raise ConflictError(f"document {self.name}/{doc_id} changed concurrently")

        self.db._commit(puts, deletes, message=message, verify=verify)

    def restore(
        self,
        doc_id: str,
        commit_sha: str,
        *,
        message: Optional[str] = None,
    ) -> Optional[Document]:
        """Restore the document as it was at ``commit_sha`` with a new commit.

        Returns the restored document, or ``None`` when the document did not
        exist at that commit (in which case it is deleted).
        """
        validate_id(doc_id)
        path = self.document_path(doc_id)
        historical = self.db._read_at(path, commit_sha)
        commit_message = message or f"restore {self.name}/{doc_id} from {commit_sha[:7]}"
        if historical is None:
            try:
                self.delete(doc_id, message=commit_message)
            except NotFoundError:
                pass
            return None
        payload = dict(historical)
        payload.pop("_rev", None)
        payload.pop("_updated_at", None)
        return self.upsert(doc_id, payload, message=commit_message)

    # -------------------------------------------------------------- iteration
    def list(self, limit: Optional[int] = None, *, after: Optional[str] = None) -> List[str]:
        """Return the sorted ids stored in this collection.

        ``after`` is a cursor: pass the last id of the previous page to continue
        from there without listing everything again.
        """
        ids = self._ids()
        if after is not None:
            ids = [doc_id for doc_id in ids if doc_id > after]
        return ids[:limit] if limit is not None else ids

    def _ids(self) -> List[str]:
        if self.config.manifest:
            manifest, _ = self.db._read(self.db.manifest_path(self.name))
            if manifest is not None:
                return sorted(manifest_ids(manifest))
        return [
            doc_id
            for doc_id in (GitDb.id_from_path(path) for path in self.db._list_paths(self.path))
            if doc_id is not None
        ]

    def _entries_for(self, ids: Sequence[str]) -> List[TreeEntry]:
        entries: List[TreeEntry] = []
        for doc_id in ids:
            path = self.document_path(doc_id)
            cached = self.db.cache.get(path)
            entries.append(TreeEntry(path, cached.sha if cached is not None else None))
        return entries

    def _documents_for(self, ids: Sequence[str]) -> List[Document]:
        if not ids:
            return []
        entries = self._entries_for(ids)
        fetched = self.db._read_many(entries)
        return [fetched[entry.path] for entry in entries if entry.path in fetched]

    def all(
        self,
        limit: Optional[int] = None,
        *,
        after: Optional[str] = None,
    ) -> Iterator[Document]:
        """Yield every document in the collection, in id order."""
        yield from self._documents_for(self.list(limit=limit, after=after))

    def page(self, limit: int = 100, *, after: Optional[str] = None) -> Page:
        """Return one page of documents plus the cursor for the next page."""
        ids = self.list(limit=limit, after=after)
        documents = self._documents_for(ids)
        cursor = ids[-1] if len(ids) == limit else None
        return Page(documents, cursor)

    def pages(self, size: int = 100, *, after: Optional[str] = None) -> Iterator[List[Document]]:
        """Iterate the collection page by page, never listing it all at once."""
        cursor = after
        while True:
            page = self.page(size, after=cursor)
            if page.documents:
                yield page.documents
            if page.cursor is None:
                return
            cursor = page.cursor

    def find(
        self,
        predicate: Callable[[Document], bool],
        *,
        limit: Optional[int] = None,
        after: Optional[str] = None,
    ) -> List[Document]:
        """Client-side filter over every document in the collection."""
        matches: List[Document] = []
        for document in self.all(after=after):
            if predicate(document):
                matches.append(document)
                if limit is not None and len(matches) >= limit:
                    break
        return matches

    def find_by(self, field: str, value: Any, *, limit: Optional[int] = None) -> List[Document]:
        """Look documents up by field value.

        When ``field`` is one of the collection's configured indexes this costs
        one request for the index plus one per matching document. Otherwise it
        falls back to a full client-side scan.
        """
        if field not in self.config.indexes:
            return self.find(lambda document: document.get(field) == value, limit=limit)
        index, _ = self.db._read(self.db.index_path(self.name, field))
        if index is None:
            return []
        ids = lookup_index(index, value)
        if limit is not None:
            ids = ids[:limit]
        return self._documents_for(ids)

    def count(self) -> int:
        if self.config.manifest:
            manifest, _ = self.db._read(self.db.manifest_path(self.name))
            if manifest is not None:
                return len(manifest_ids(manifest))
        return len(self.list())

    def history(self, doc_id: str, *, limit: int = 30) -> List[Dict[str, Any]]:
        return self.db.history(self.name, doc_id, limit=limit)

    def reindex(self, *, message: Optional[str] = None) -> Optional[str]:
        """Rebuild this collection's indexes and manifest from the documents."""
        return self.db.reindex(self.name, message=message)

    def __iter__(self) -> Iterator[Document]:
        return self.all()

    def __len__(self) -> int:
        return self.count()

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"Collection(name={self.name!r}, path={self.path!r})"


def _check_rev(
    collection: str,
    doc_id: str,
    existing: Mapping[str, Any],
    expected_rev: Optional[int],
) -> None:
    if expected_rev is None:
        return
    current = existing.get("_rev")
    if current != expected_rev:
        raise ConflictError(
            f"document {collection}/{doc_id} is at revision {current}, expected {expected_rev}"
        )
