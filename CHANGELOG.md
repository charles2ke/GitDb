# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned

- `AsyncGitDb` built on `httpx.AsyncClient`.
- Server-side-ish querying helpers (secondary index files).

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
