"""The asyncio client: :class:`AsyncGitDb`, :class:`AsyncCollection`, :class:`AsyncBatch`.

The transport-independent logic (path building, id validation, JSON encoding,
metadata stamping, index/manifest maintenance, caching and rate-limit pacing) is
shared with the synchronous client; only the I/O is different. Concurrency here
comes from ``asyncio.gather`` rather than a thread pool, which makes the bulk
reads of :meth:`AsyncCollection.all` natural.

Requires the ``async`` extra::

    pip install "gitdb-py[async]"
"""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from types import TracebackType
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union
from urllib.parse import quote

from .async_http import AsyncGitHubClient
from .cache import Cache, CacheEntry, MemoryCache, NullCache
from .client import (
    CONTENTS_MAX_BYTES,
    GRAPHQL_BATCH_SIZE,
    SCAN_CHUNK,
    CommitResult,
    Page,
    TreeEntry,
    _as_config_map,
    _check_rev,
    _make_cache,
)
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
from .http import DEFAULT_API_URL, DEFAULT_RAW_URL
from .ids import new_id, validate_id, validate_name
from .paths import PathResolver
from .ratelimit import RateLimit

__all__ = ["AsyncGitDb", "AsyncCollection", "AsyncBatch"]


class AsyncGitDb:
    """The asyncio twin of :class:`~gitdb.client.GitDb`.

    Every option of the synchronous client is accepted with the same meaning,
    except ``session`` (pass an ``httpx.AsyncClient`` as ``client``) and
    ``concurrency``, which bounds the number of in-flight requests.
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
        client: Optional[Any] = None,
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
        concurrency: int = 8,
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
        if token is None and auth is None and client is None and not read_only:
            raise AuthError("a token, auth or client is required unless read_only=True")

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
        self.client = AsyncGitHubClient(
            token,
            api_url=api_url,
            client=client,
            auth=auth,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            timeout=timeout,
            pace_requests=pace_requests,
        )
        self.cache: Cache = _make_cache(cache)
        self._collections: Dict[str, AsyncCollection] = {}
        # Created lazily: on Python 3.9 a Semaphore built outside a running loop
        # binds itself to the wrong loop.
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._semaphore_loop: Optional[asyncio.AbstractEventLoop] = None
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

    def collection(self, name: str) -> AsyncCollection:
        """Return (and memoize) the :class:`AsyncCollection` called ``name``."""
        validate_name(name)
        if name not in self._collections:
            self._collections[name] = AsyncCollection(self, name)
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
    ) -> AsyncGitDb:
        clone = object.__new__(AsyncGitDb)
        clone.__dict__.update(self.__dict__)
        clone._collections = {}
        clone.cache = MemoryCache() if not isinstance(self.cache, NullCache) else NullCache()
        clone._semaphore = None
        clone._semaphore_loop = None
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

    def at(self, commit_sha: str) -> AsyncGitDb:
        """Return a read-only view pinned to ``commit_sha``."""
        if not commit_sha:
            raise ValidationError("commit sha must not be empty")
        return self._view(ref=commit_sha, read_only=True)

    async def snapshot(self) -> AsyncGitDb:
        """Resolve the branch head and return a read-only view pinned to it."""
        return self.at(await self.resolve_ref(refresh=True))

    def on_branch(self, branch: str) -> AsyncGitDb:
        """Return a view of the same repository on another branch."""
        return self._view(branch=branch)

    async def resolve_ref(self, *, refresh: bool = False) -> str:
        """Return the commit sha the branch currently points at."""
        if self.ref:
            return self.ref
        if self._resolved_ref is None or refresh:
            payload = await self.client.get_json(f"/repos/{self.repo}/git/ref/heads/{self.branch}")
            self._resolved_ref = str(payload["object"]["sha"])
        return self._resolved_ref

    async def _raw_ref(self) -> str:
        if self.ref:
            return self.ref
        if not self.pin_ref:
            return self.branch
        try:
            return await self.resolve_ref()
        except GitDbError:
            return self.branch

    async def rate_limit(self, resource: str = "core") -> RateLimit:
        """Return the current API quota (``GET /rate_limit`` costs no quota)."""
        return await self.client.rate_limit(resource)

    # ------------------------------------------------------------------ cache
    def invalidate(self, path: Optional[str] = None) -> None:
        if path is None:
            self.cache.clear()
        else:
            self.cache.delete(path)

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
    async def _read(
        self, path: str, *, fresh: bool = False
    ) -> Tuple[Optional[Document], Optional[str]]:
        if self.read_only and not fresh:
            return await self._read_raw(path)
        return await self._read_contents(path)

    async def _read_raw(self, path: str) -> Tuple[Optional[Document], Optional[str]]:
        entry = self.cache.get(path)
        url = f"{self.raw_url}/{self.repo}/{await self._raw_ref()}/{path}"
        headers: Dict[str, str] = {}
        if entry is not None and entry.etag and entry.document is not None:
            headers["If-None-Match"] = entry.etag
        response = await self.client.request("GET", url, headers=headers or None, allow_404=True)
        if response.status_code == 304 and entry is not None and entry.document is not None:
            return dict(entry.document), entry.sha
        if response.status_code == 404:
            self.cache.delete(path)
            return None, None
        document = loads_document(response.content, path)
        self._store(path, etag=response.headers.get("ETag"), document=document)
        return document, None

    async def _read_contents(self, path: str) -> Tuple[Optional[Document], Optional[str]]:
        entry = self.cache.get(path)
        headers: Dict[str, str] = {}
        if entry is not None and entry.etag and entry.document is not None:
            headers["If-None-Match"] = entry.etag
        response = await self.client.request(
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
        document = await self._document_from_contents(payload, path)
        self._store(path, sha=sha, etag=response.headers.get("ETag"), document=document)
        return document, sha

    async def _document_from_contents(self, payload: Mapping[str, Any], path: str) -> Document:
        content = payload.get("content")
        if payload.get("encoding") == "base64" and isinstance(content, str) and content.strip():
            return decode_document(content, path)
        sha = payload.get("sha")
        if isinstance(sha, str) and sha:
            return await self._read_blob(sha, path)
        raise ValidationError(f"stored document at {path} has no readable content")

    async def _read_blob(self, sha: str, path: Optional[str] = None) -> Document:
        """Fetch one blob by sha using the raw media type (no size limit)."""
        response = await self.client.request(
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
            return decode_document(document["content"], path)
        return document

    async def _sha(self, path: str, *, refresh: bool = False) -> Optional[str]:
        if not refresh:
            entry = self.cache.get(path)
            if entry is not None and entry.sha is not None:
                return entry.sha
        _, sha = await self._read_contents(path)
        return sha

    async def _read_many(self, entries: Sequence[TreeEntry]) -> Dict[str, Document]:
        """Fetch many documents concurrently, reusing cached bodies."""
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
            fetched = await self._graphql_documents([entry.path for entry in pending])
            results.update(fetched)
            pending = [entry for entry in pending if entry.path not in fetched]

        for path, document in await self._gather(self._fetch_one, pending):
            if document is not None:
                results[path] = document
        return results

    async def _fetch_one(self, entry: TreeEntry) -> Tuple[str, Optional[Document]]:
        if entry.sha and not self.read_only:
            blob = await self._read_blob(entry.sha, entry.path)
            self._store(entry.path, sha=entry.sha, document=blob)
            return entry.path, blob
        document, _ = await self._read(entry.path)
        return entry.path, document

    def _gate(self) -> asyncio.Semaphore:
        """Return a semaphore bound to the running loop, rebuilding it if needed."""
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._semaphore_loop is not loop:
            self._semaphore = asyncio.Semaphore(self.concurrency)
            self._semaphore_loop = loop
        return self._semaphore

    async def _gather(self, function: Callable[[Any], Any], items: Sequence[Any]) -> List[Any]:
        """Run ``function`` over ``items`` with at most ``concurrency`` in flight."""
        if not items:
            return []
        gate = self._gate()

        async def guarded(item: Any) -> Any:
            async with gate:
                return await function(item)

        return list(await asyncio.gather(*(guarded(item) for item in items)))

    async def _graphql_documents(self, paths: Sequence[str]) -> Dict[str, Document]:
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
            data = await self.client.graphql(query, {"owner": owner, "name": name})
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
            raise AuthError("this AsyncGitDb instance is read-only")

    def _commit_fields(self, message: str) -> Dict[str, Any]:
        body: Dict[str, Any] = {"message": message, "branch": self.branch}
        if self.committer:
            body["committer"] = self.committer
        if self.author:
            body["author"] = self.author
        return body

    async def _write(
        self,
        path: str,
        document: Mapping[str, Any],
        *,
        sha: Optional[str],
        message: str,
    ) -> str:
        self._assert_writable()
        payload = dump_document(document)
        if len(payload) > self.contents_max_bytes:
            result = await self._commit({path: document}, (), message=message)
            return result.blobs[path]
        body = self._commit_fields(message)
        body["content"] = encode_document(document)
        if sha:
            body["sha"] = sha
        response = await self.client.request(
            "PUT", f"/repos/{self.repo}/contents/{path}", json=body
        )
        new_sha = response.json().get("content", {}).get("sha")
        self._store(path, sha=new_sha, document=document)
        return str(new_sha)

    async def _remove(self, path: str, *, sha: str, message: str) -> None:
        self._assert_writable()
        body = self._commit_fields(message)
        body["sha"] = sha
        await self.client.request("DELETE", f"/repos/{self.repo}/contents/{path}", json=body)
        self.cache.delete(path)

    async def _create_blob(self, document: Mapping[str, Any]) -> str:
        response = await self.client.request(
            "POST",
            f"/repos/{self.repo}/git/blobs",
            json={"content": encode_document(document), "encoding": "base64"},
        )
        return str(response.json()["sha"])

    async def _commit(
        self,
        puts: Mapping[str, Mapping[str, Any]],
        deletes: Sequence[str] = (),
        *,
        message: str,
        verify: Optional[Callable[[], Any]] = None,
        retries: Optional[int] = None,
    ) -> CommitResult:
        """Write ``puts``/``deletes`` as one commit (blobs → tree → commit → ref)."""
        self._assert_writable()
        if verify is not None:
            await verify()
        blobs: Dict[str, str] = {}
        for path, document in puts.items():
            blobs[path] = await self._create_blob(document)
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
                await verify()
            base_commit = await self.resolve_ref(refresh=True)
            commit_payload = await self.client.get_json(
                f"/repos/{self.repo}/git/commits/{base_commit}"
            )
            tree_response = await self.client.request(
                "POST",
                f"/repos/{self.repo}/git/trees",
                json={"base_tree": commit_payload["tree"]["sha"], "tree": entries},
            )
            tree = tree_response.json()
            commit_body: Dict[str, Any] = {
                "message": message,
                "tree": tree["sha"],
                "parents": [base_commit],
            }
            if self.author:
                commit_body["author"] = self.author
            if self.committer:
                commit_body["committer"] = self.committer
            commit = (
                await self.client.request(
                    "POST", f"/repos/{self.repo}/git/commits", json=commit_body
                )
            ).json()
            try:
                await self.client.request(
                    "PATCH",
                    f"/repos/{self.repo}/git/refs/heads/{self.branch}",
                    json={"sha": commit["sha"], "force": False},
                )
            except ConflictError as exc:
                last_error = exc
                self._resolved_ref = None
                if attempt >= attempts - 1:
                    break
                await asyncio.sleep(self.client._sleep_for(attempt))
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
    async def _derived_documents(
        self,
        collection: str,
        changes: Mapping[str, Optional[Mapping[str, Any]]],
    ) -> Dict[str, Document]:
        config = self.config(collection)
        updates: Dict[str, Document] = {}
        for field in config.indexes:
            path = self.index_path(collection, field)
            current, _ = await self._read(path)
            updates[path] = apply_index(
                current or empty_index(collection, field), collection, field, changes
            )
        if config.manifest:
            path = self.manifest_path(collection)
            current, _ = await self._read(path)
            updates[path] = apply_manifest(
                current or empty_manifest(collection),
                collection,
                changes,
                config.manifest_fields,
            )
        return updates

    async def reindex(self, collection: str, *, message: Optional[str] = None) -> Optional[str]:
        """Rebuild every index and manifest of ``collection`` in one commit."""
        self._assert_writable()
        config = self.config(collection)
        if not config.derived:
            return None
        documents = {
            str(document.get("_id")): document
            for document in await self.collection(collection).all()
            if document.get("_id")
        }
        updates: Dict[str, Document] = {}
        for field in config.indexes:
            updates[self.index_path(collection, field)] = build_index(collection, field, documents)
        if config.manifest:
            updates[self.manifest_path(collection)] = build_manifest(
                collection, documents, config.manifest_fields
            )
        result = await self._commit(updates, message=message or f"reindex {collection}")
        return result.sha

    # ------------------------------------------------------------------ trees
    async def _tree(self, expression: str, *, recursive: bool = True) -> Dict[str, Any]:
        payload = await self.client.get_json(
            f"/repos/{self.repo}/git/trees/{quote(expression, safe='')}",
            params={"recursive": "1"} if recursive else None,
        )
        return payload if isinstance(payload, dict) else {}

    async def _list_entries(self, prefix: str) -> List[TreeEntry]:
        entries = await self._scoped_entries(prefix)
        if entries is None:
            entries = await self._repository_entries(prefix)
        if entries is None:
            entries = await self._contents_entries(prefix)
        for entry in entries:
            if entry.sha:
                self._store(entry.path, sha=entry.sha)
        return sorted(entries, key=lambda entry: entry.path)

    async def _scoped_entries(self, prefix: str) -> Optional[List[TreeEntry]]:
        """List the collection subtree only — far smaller than the whole repo tree."""
        try:
            payload = await self._tree(f"{self.read_ref}:{prefix}")
        except GitDbError:
            return None
        if payload.get("truncated"):
            return await self._descend(prefix)
        return [
            TreeEntry(f"{prefix}/{entry['path']}", entry.get("sha"))
            for entry in payload.get("tree", [])
            if entry.get("type") == "blob" and str(entry.get("path", "")).endswith(".json")
        ]

    async def _descend(self, prefix: str) -> List[TreeEntry]:
        """Walk a truncated tree one shard at a time instead of listing everything."""
        try:
            payload = await self._tree(f"{self.read_ref}:{prefix}", recursive=False)
        except GitDbError:
            return await self._contents_entries(prefix)
        if payload.get("truncated"):
            return await self._contents_entries(prefix)
        found: List[TreeEntry] = []
        for entry in payload.get("tree", []):
            name = str(entry.get("path", ""))
            if entry.get("type") == "blob" and name.endswith(".json"):
                found.append(TreeEntry(f"{prefix}/{name}", entry.get("sha")))
            elif entry.get("type") == "tree" and name:
                nested = await self._scoped_entries(f"{prefix}/{name}")
                if nested is None:
                    nested = await self._contents_entries(f"{prefix}/{name}")
                found.extend(nested)
        return found

    async def _repository_entries(self, prefix: str) -> Optional[List[TreeEntry]]:
        try:
            payload = await self._tree(self.read_ref)
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

    async def _contents_entries(self, prefix: str) -> List[TreeEntry]:
        found: List[TreeEntry] = []
        pending = [prefix]
        while pending:
            current = pending.pop()
            response = await self.client.request(
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

    async def _list_paths(self, prefix: str) -> List[str]:
        return [entry.path for entry in await self._list_entries(prefix)]

    # ---------------------------------------------------------------- history
    async def history(
        self, collection: str, doc_id: str, *, limit: int = 30
    ) -> List[Dict[str, Any]]:
        """Return the commit history of a single document, newest first."""
        path = self.document_path(collection, doc_id)
        commits = await self.client.get_json(
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

    async def _read_at(self, path: str, commit_sha: str) -> Optional[Document]:
        response = await self.client.request(
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
        return await self._document_from_contents(payload, path)

    async def revert(self, commit_sha: str, *, message: Optional[str] = None) -> Optional[str]:
        """Undo ``commit_sha`` by committing its inverse (history is never rewritten)."""
        self._assert_writable()
        commit = await self.client.get_json(f"/repos/{self.repo}/commits/{commit_sha}")
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
            previous = await self._read_at(path, parent)
            if previous is None:
                deletes.append(path)
            else:
                puts[path] = previous
        if not puts and not deletes:
            return None
        result = await self._commit(puts, deletes, message=message or f"revert {commit_sha[:7]}")
        return result.sha

    # ------------------------------------------------------- batching helpers
    def batch(self, message: str = "gitdb batch") -> AsyncBatch:
        """Return an :class:`AsyncBatch` writing every queued change in one commit."""
        self._assert_writable()
        return AsyncBatch(self, message)

    # ------------------------------------------------------------ maintenance
    async def compact(self, *, message: str = "gitdb compaction", confirm: bool = False) -> str:
        """Squash the whole branch history into a single commit (destructive)."""
        self._assert_writable()
        if not confirm:
            raise ValidationError(
                "compact() rewrites history and force-updates the branch; "
                "call compact(confirm=True) to proceed"
            )
        head = await self.resolve_ref(refresh=True)
        payload = await self.client.get_json(f"/repos/{self.repo}/git/commits/{head}")
        body: Dict[str, Any] = {
            "message": message,
            "tree": payload["tree"]["sha"],
            "parents": [],
        }
        if self.author:
            body["author"] = self.author
        if self.committer:
            body["committer"] = self.committer
        response = await self.client.request("POST", f"/repos/{self.repo}/git/commits", json=body)
        commit = response.json()
        await self.client.request(
            "PATCH",
            f"/repos/{self.repo}/git/refs/heads/{self.branch}",
            json={"sha": commit["sha"], "force": True},
        )
        self._resolved_ref = str(commit["sha"])
        self.invalidate()
        return self._resolved_ref

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> AsyncGitDb:
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        await self.aclose()

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"AsyncGitDb(repo={self.repo!r}, branch={self.branch!r}, root={self.root!r})"


class AsyncCollection:
    """The asyncio twin of :class:`~gitdb.client.Collection`."""

    def __init__(self, db: AsyncGitDb, name: str) -> None:
        self.db = db
        self.name = name

    @property
    def path(self) -> str:
        return self.db.collection_path(self.name)

    @property
    def config(self) -> CollectionConfig:
        return self.db.config(self.name)

    def document_path(self, doc_id: str) -> str:
        return self.db.document_path(self.name, doc_id)

    # ------------------------------------------------------------------- CRUD
    async def insert(
        self,
        document: Mapping[str, Any],
        *,
        id: Optional[str] = None,
        message: Optional[str] = None,
    ) -> str:
        """Create a new document and return its id."""
        doc_id = validate_id(id) if id is not None else new_id()
        path = self.document_path(doc_id)
        payload = with_metadata(doc_id, document)
        commit_message = message or f"insert {self.name}/{doc_id}"
        if self.config.derived:
            await self._commit_document(doc_id, payload, message=commit_message, expect_absent=True)
        else:
            await self.db._write(path, payload, sha=None, message=commit_message)
        return doc_id

    async def get(self, doc_id: str, *, fresh: bool = False) -> Optional[Document]:
        """Return the document, or ``None`` when it does not exist."""
        document, _ = await self.db._read(self.document_path(doc_id), fresh=fresh)
        return document

    async def exists(self, doc_id: str) -> bool:
        return await self.get(doc_id) is not None

    async def replace(
        self,
        doc_id: str,
        document: Mapping[str, Any],
        *,
        message: Optional[str] = None,
        expected_rev: Optional[int] = None,
    ) -> Document:
        """Overwrite a document wholesale (it must already exist)."""
        return await self._modify(
            doc_id, document, merge=False, message=message, expected_rev=expected_rev
        )

    async def update(
        self,
        doc_id: str,
        patch: Mapping[str, Any],
        *,
        message: Optional[str] = None,
        expected_rev: Optional[int] = None,
    ) -> Document:
        """Shallow-merge ``patch`` into an existing document."""
        return await self._modify(
            doc_id, patch, merge=True, message=message, expected_rev=expected_rev
        )

    async def upsert(
        self,
        doc_id: str,
        document: Mapping[str, Any],
        *,
        message: Optional[str] = None,
    ) -> Document:
        """Update the document when it exists, otherwise create it."""
        try:
            return await self._modify(doc_id, document, merge=True, message=message)
        except NotFoundError:
            await self.insert(document, id=doc_id, message=message)
            result = await self.get(doc_id)
            if result is None:  # pragma: no cover - defensive
                raise
            return result

    async def _modify(
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
        attempts = 1 if expected_rev is not None else self.db.conflict_retries + 1
        last_error: Optional[ConflictError] = None
        for _ in range(attempts):
            existing, sha = await self.db._read(path)
            if existing is None or sha is None:
                raise NotFoundError(f"document {self.name}/{doc_id} does not exist")
            _check_rev(self.name, doc_id, existing, expected_rev)
            base = dict(existing) if merge else {}
            base.update(changes)
            payload = with_metadata(doc_id, base, existing)
            commit_message = message or f"update {self.name}/{doc_id}"
            try:
                if self.config.derived:
                    await self._commit_document(
                        doc_id, payload, message=commit_message, expect_sha=sha
                    )
                else:
                    await self.db._write(path, payload, sha=sha, message=commit_message)
            except ConflictError as exc:
                last_error = exc
                self.db.invalidate(path)
                continue
            return payload
        raise last_error or ConflictError(f"could not write {self.name}/{doc_id}")

    async def delete(
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
            existing, sha = await self.db._read(path)
            if existing is None or sha is None:
                raise NotFoundError(f"document {self.name}/{doc_id} does not exist")
            _check_rev(self.name, doc_id, existing, expected_rev)
            if self.config.derived:
                await self._commit_document(doc_id, None, message=commit_message, expect_sha=sha)
            else:
                await self.db._remove(path, sha=sha, message=commit_message)
            return

        attempts = self.db.conflict_retries + 1
        last_error: Optional[ConflictError] = None
        for _ in range(attempts):
            sha = await self.db._sha(path, refresh=last_error is not None)
            if sha is None:
                raise NotFoundError(f"document {self.name}/{doc_id} does not exist")
            try:
                await self.db._remove(path, sha=sha, message=commit_message)
            except ConflictError as exc:
                last_error = exc
                self.db.invalidate(path)
                continue
            return
        raise last_error or ConflictError(f"could not delete {self.name}/{doc_id}")

    async def _commit_document(
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
        puts: Dict[str, Mapping[str, Any]] = dict(
            await self.db._derived_documents(self.name, changes)
        )
        deletes: List[str] = []
        if document is None:
            deletes.append(path)
        else:
            puts[path] = document

        async def verify() -> None:
            current = await self.db._sha(path, refresh=True)
            if expect_absent and current is not None:
                raise ConflictError(f"document {self.name}/{doc_id} already exists")
            if expect_sha is not None and current != expect_sha:
                raise ConflictError(f"document {self.name}/{doc_id} changed concurrently")

        await self.db._commit(puts, deletes, message=message, verify=verify)

    async def restore(
        self,
        doc_id: str,
        commit_sha: str,
        *,
        message: Optional[str] = None,
    ) -> Optional[Document]:
        """Restore the document as it was at ``commit_sha`` with a new commit."""
        validate_id(doc_id)
        path = self.document_path(doc_id)
        historical = await self.db._read_at(path, commit_sha)
        commit_message = message or f"restore {self.name}/{doc_id} from {commit_sha[:7]}"
        if historical is None:
            try:
                await self.delete(doc_id, message=commit_message)
            except NotFoundError:
                pass
            return None
        payload = dict(historical)
        payload.pop("_rev", None)
        payload.pop("_updated_at", None)
        return await self.upsert(doc_id, payload, message=commit_message)

    # -------------------------------------------------------------- iteration
    async def list(self, limit: Optional[int] = None, *, after: Optional[str] = None) -> List[str]:
        """Return the sorted ids stored in this collection."""
        ids = await self._ids()
        if after is not None:
            ids = ids[bisect_right(ids, after) :]
        return ids[:limit] if limit is not None else ids

    async def _ids(self) -> List[str]:
        if self.config.manifest:
            manifest, _ = await self.db._read(self.db.manifest_path(self.name))
            if manifest is not None:
                return sorted(manifest_ids(manifest))
        return sorted(
            doc_id
            for doc_id in (
                AsyncGitDb.id_from_path(path) for path in await self.db._list_paths(self.path)
            )
            if doc_id is not None
        )

    def _entries_for(self, ids: Sequence[str]) -> List[TreeEntry]:
        entries: List[TreeEntry] = []
        for doc_id in ids:
            path = self.document_path(doc_id)
            cached = self.db.cache.get(path)
            entries.append(TreeEntry(path, cached.sha if cached is not None else None))
        return entries

    async def _documents_for(self, ids: Sequence[str]) -> List[Document]:
        entries = self._entries_for(ids)
        found = await self.db._read_many(entries)
        return [found[entry.path] for entry in entries if entry.path in found]

    async def all(
        self, *, limit: Optional[int] = None, after: Optional[str] = None
    ) -> List[Document]:
        """Return every document in the collection, sorted by id."""
        return await self._documents_for(await self.list(limit=limit, after=after))

    async def page(self, limit: int = 100, *, after: Optional[str] = None) -> Page:
        """Return one page of documents plus the cursor for the next page."""
        ids = await self.list(limit=limit, after=after)
        documents = await self._documents_for(ids)
        cursor = ids[-1] if len(ids) == limit else None
        return Page(documents, cursor)

    async def pages(
        self, size: int = 100, *, after: Optional[str] = None
    ) -> AsyncIterator[List[Document]]:
        """Yield the collection page by page without ever listing it all at once."""
        cursor = after
        while True:
            current = await self.page(size, after=cursor)
            if current.documents:
                yield current.documents
            if current.cursor is None:
                return
            cursor = current.cursor

    async def find(
        self,
        predicate: Callable[[Document], bool],
        *,
        limit: Optional[int] = None,
        after: Optional[str] = None,
    ) -> List[Document]:
        """Client-side filter over every document in the collection.

        Documents are fetched in chunks, so a bounded search stops as soon as
        enough matches are found instead of downloading the whole collection.
        """
        matches: List[Document] = []
        ids = await self.list(after=after)
        for start in range(0, len(ids), SCAN_CHUNK):
            for document in await self._documents_for(ids[start : start + SCAN_CHUNK]):
                if predicate(document):
                    matches.append(document)
                    if limit is not None and len(matches) >= limit:
                        return matches
        return matches

    async def find_by(
        self, field: str, value: Any, *, limit: Optional[int] = None
    ) -> List[Document]:
        """Look documents up by field value, using an index when one exists."""
        if field not in self.config.indexes:
            return await self.find(lambda document: document.get(field) == value, limit=limit)
        index, _ = await self.db._read(self.db.index_path(self.name, field))
        if index is None:
            return []
        ids = lookup_index(index, value)
        if limit is not None:
            ids = ids[:limit]
        return await self._documents_for(ids)

    async def count(self) -> int:
        if self.config.manifest:
            manifest, _ = await self.db._read(self.db.manifest_path(self.name))
            if manifest is not None:
                return len(manifest_ids(manifest))
        return len(await self.list())

    async def history(self, doc_id: str, *, limit: int = 30) -> List[Dict[str, Any]]:
        return await self.db.history(self.name, doc_id, limit=limit)

    async def reindex(self, *, message: Optional[str] = None) -> Optional[str]:
        """Rebuild this collection's indexes and manifest from the documents."""
        return await self.db.reindex(self.name, message=message)

    async def __aiter__(self) -> AsyncIterator[Document]:
        for document in await self.all():
            yield document

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"AsyncCollection(name={self.name!r}, path={self.path!r})"


class AsyncBatch:
    """The asyncio twin of :class:`~gitdb.batch.Batch`."""

    def __init__(self, db: AsyncGitDb, message: str = "gitdb batch") -> None:
        self.db = db
        self.message = message
        self._puts: Dict[str, Document] = {}
        self._deletes: List[str] = []
        self._collections: Dict[str, Dict[str, Optional[Document]]] = {}
        self._expectations: Dict[str, Tuple[Optional[str], Optional[int], bool]] = {}

    def put(
        self,
        collection: str,
        doc_id: str,
        document: Mapping[str, Any],
        *,
        expected_sha: Optional[str] = None,
        expected_rev: Optional[int] = None,
        absent: bool = False,
    ) -> AsyncBatch:
        """Queue a document write, optionally guarded by a precondition."""
        validate_name(collection)
        validate_id(doc_id)
        path = self.db.document_path(collection, doc_id)
        payload = with_metadata(doc_id, document, document)
        self._puts[path] = payload
        self._collections.setdefault(collection, {})[doc_id] = payload
        if path in self._deletes:
            self._deletes.remove(path)
        if expected_sha is not None or expected_rev is not None or absent:
            self._expectations[path] = (expected_sha, expected_rev, absent)
        return self

    def insert(self, collection: str, document: Mapping[str, Any]) -> str:
        """Queue a new document with a generated id and return that id."""
        doc_id = new_id()
        self.put(collection, doc_id, document, absent=False)
        return doc_id

    def delete(
        self,
        collection: str,
        doc_id: str,
        *,
        expected_sha: Optional[str] = None,
        expected_rev: Optional[int] = None,
    ) -> AsyncBatch:
        """Queue the removal of ``collection/doc_id``."""
        validate_name(collection)
        validate_id(doc_id)
        path = self.db.document_path(collection, doc_id)
        self._puts.pop(path, None)
        self._collections.setdefault(collection, {})[doc_id] = None
        if path not in self._deletes:
            self._deletes.append(path)
        if expected_sha is not None or expected_rev is not None:
            self._expectations[path] = (expected_sha, expected_rev, False)
        return self

    def discard(self) -> None:
        """Throw away every queued operation."""
        self._puts.clear()
        self._deletes.clear()
        self._collections.clear()
        self._expectations.clear()

    @property
    def operations(self) -> int:
        return len(self._puts) + len(self._deletes)

    @property
    def expectations(self) -> int:
        return len(self._expectations)

    async def _verify(self) -> None:
        for path, (expected_sha, expected_rev, absent) in self._expectations.items():
            document, sha = await self.db._read(path, fresh=True)
            if absent and sha is not None:
                raise ConflictError(f"{path} already exists")
            if expected_sha is not None and sha != expected_sha:
                raise ConflictError(
                    f"{path} is at sha {sha!r}, expected {expected_sha!r}",
                )
            if expected_rev is not None:
                current = document.get("_rev") if document else None
                if current != expected_rev:
                    raise ConflictError(
                        f"{path} is at revision {current}, expected {expected_rev}",
                    )

    async def commit(self) -> Optional[str]:
        """Write every queued operation in one commit; return the commit sha."""
        if not self._puts and not self._deletes:
            return None
        self.db._assert_writable()
        puts: Dict[str, Mapping[str, Any]] = dict(self._puts)
        for collection, changes in self._collections.items():
            puts.update(await self.db._derived_documents(collection, changes))
        verify = self._verify if self._expectations else None
        result = await self.db._commit(
            puts, list(self._deletes), message=self.message, verify=verify
        )
        self.discard()
        return result.sha

    async def __aenter__(self) -> AsyncBatch:
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        if exc_type is None:
            await self.commit()
        else:
            self.discard()

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"AsyncBatch(operations={self.operations}, message={self.message!r})"
