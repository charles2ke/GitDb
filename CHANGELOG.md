# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned

- Async `Writer`/`Transaction` twins.
- Range and prefix indexes in addition to equality lookups.

## [0.2.0] - 2026-08-29

Addresses the limitations documented in the README. Everything is
backwards-compatible: new behaviour is either a pure improvement or opt-in.

### Added

- `AsyncGitDb`, `AsyncCollection` and `AsyncBatch` built on `httpx.AsyncClient`,
  mirroring the synchronous surface. Install with `pip install "gitdb-py[async]"`.
- Secondary indexes (`{root}/_index/{collection}/{field}.json`) written in the
  same commit as the document, with `Collection.find_by(field, value)` and
  `reindex()` for drift. Configure with `indexes={"users": ["email"]}`.
- Collection manifests (`{root}/_manifest/{collection}.json`) so `list()` and
  `count()` cost a single request. Configure with `manifests=[...]`.
- `concurrency` option: a bounded thread pool (`asyncio.gather` in the async
  client) fanning out the document reads behind `all()`, `find()` and `count()`.
- Optional GraphQL bulk reads (`use_graphql=True`, `graphql_batch_size`), which
  collapse N document reads into a handful of requests.
- Cursor pagination: `Collection.page()`, `Collection.pages()` and `after=` on
  `list()`/`all()`.
- Snapshots and pinned reads: `db.at(commit_sha)`, `db.snapshot()`,
  `db.on_branch(branch)`, `db.resolve_ref()` and the `ref`/`pin_ref` options.
  Read-only mode now resolves the branch to a commit sha and reads immutable
  `raw.githubusercontent.com/{repo}/{sha}/{path}` urls by default.
- `get(id, fresh=True)` to bypass caches and revalidate a read.
- Batch preconditions: `Batch.expect(collection, id, rev=..., sha=...)`, verified
  against the base tree before any blob is uploaded.
- Conflict-aware batch commits: a lost ref-update race rebuilds the tree on the
  new head and retries up to `batch_retries` times instead of failing outright.
- `db.transaction()` — stages several commits on a work branch and
  fast-forwards the target branch in a single ref update, deleting the work
  branch on rollback.
- `db.writer()` — coalesces single-document writes into commits every
  `max_operations` operations or `max_seconds` seconds.
- Compensating undo: `Collection.restore(id, commit_sha)` and
  `db.revert(commit_sha)` commit forward instead of rewriting history.
- `expected_rev=` compare-and-set on `update()`, `replace()` and `delete()`.
- `db.compact(confirm=True)` — squashes branch history into a single commit
  (destructive, opt-in).
- Conditional requests: ETags are cached next to blob shas and replayed as
  `If-None-Match`; GitHub does not charge 304 responses against the quota.
- Pluggable caching: `Cache`, `CacheEntry`, `MemoryCache` and `NullCache`, with
  `cache=` accepting any implementation so shas/ETags can be shared across
  processes.
- Proactive rate limiting: `pace_requests=True` spreads the remaining quota over
  the window, `db.rate_limit()` inspects the budget for free, and
  `RateLimitError` exposes `.remaining`, `.reset` and `.reset_at`.
- `InstallationTokenAuth` for GitHub App installation tokens with automatic
  refresh, for callers who need quotas above the personal-token limit.

### Changed

- Documents larger than `contents_max_bytes` (default ~900 KB) are written
  through the Git Data blob API instead of the Contents API, removing the
  effective ~1 MB write cap; reads fall back to fetching the blob by sha when
  the Contents response omits an oversized body.
- `Collection.all()` reads document bodies from the blob shas already returned
  by the tree listing, so listing plus reading no longer needs a Contents call
  per document.
- Collection listing requests the collection subtree (`{ref}:{prefix}`) rather
  than the whole repository tree, and descends per shard subtree when a tree is
  truncated, before falling back to the Contents crawl.
- Transport-independent logic (paths, ids, JSON handling, metadata, indexes,
  caching, rate limiting) moved into `paths`, `documents`, `derived`, `cache`
  and `ratelimit` modules shared by the sync and async clients.

### Fixed

- Cache entries are invalidated correctly when a blob sha changes, instead of
  merging a fresh sha with a stale document body.

## [0.1.0] - 2026-08-29

### Added

- `GitDb` client with `collection()`, `batch()`, `history()` and `invalidate()`.
- `Collection` CRUD: `insert`, `get`, `exists`, `update`, `replace`, `upsert`,
  `delete`, `list`, `all`, `find`, `count`, `history`.
- Optimistic concurrency via blob shas with bounded automatic retries on conflict.
- Batched commits through the Git Data API (blobs → tree → commit → ref update).
- Collection listing via the Trees API with `recursive=1`, including truncated
  tree handling and a Contents API fallback that walks shard directories.
- Rate-limit aware HTTP layer honouring `X-RateLimit-Remaining`,
  `X-RateLimit-Reset` and `Retry-After`, with exponential backoff plus jitter.
- Sortable ULID-style ids, strict id/collection name validation, optional
  id-prefix sharding and per-document metadata (`_id`, `_created_at`,
  `_updated_at`, `_rev`).
- In-memory blob sha cache, GitHub Enterprise support via `api_url`, injectable
  session/auth, and read-only mode over `raw.githubusercontent.com`.
- Exception hierarchy: `GitDbError`, `NotFoundError`, `ConflictError`,
  `RateLimitError`, `AuthError`, `ValidationError`.
- Type hints throughout plus `py.typed`, pytest suite with mocked HTTP, ruff and
  mypy configuration, GitHub Actions CI on Python 3.9–3.12, README and example.
