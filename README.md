# GitDb

Use a GitHub repository as a lightweight document database.

GitDb stores JSON documents as files in a Git repository and talks to the GitHub
REST API (Contents API for single documents, Git Data API for batched commits).
Every write is a commit, so you get a full audit trail, diffs, pull-request
review and rollbacks for free — with no server to run.

It is a good fit for configuration, seed data, small catalogues, feature flags,
CMS-like content and demos. It is **not** a replacement for a real database; see
[Limitations](#limitations).

## Install

```bash
pip install gitdb-py
```

From source:

```bash
pip install -e ".[dev]"
```

Requires Python 3.9+ and `requests`.

## Quickstart

```python
import os
from gitdb import GitDb

db = GitDb(repo="owner/name", token=os.environ["GITHUB_TOKEN"], branch="main", root="data")
users = db.collection("users")

user_id = users.insert({"name": "Ada", "email": "ada@example.com"})  # -> generated id
users.get(user_id)  # -> dict | None
users.update(user_id, {"email": "new@example.com"})  # partial merge
users.upsert("grace", {"name": "Grace Hopper"})
users.list(limit=100)  # -> list of ids
list(users.all())  # -> documents
users.find(lambda d: d["name"].startswith("A"))  # client-side filter
users.count()
users.delete(user_id)
```

Write many documents in a single commit:

```python
with db.batch(message="seed users") as b:
    b.put("users", "1", {"name": "Ada"})
    b.put("users", "2", {"name": "Grace"})
    b.delete("users", "3")
```

A runnable script lives in [`examples/quickstart.py`](examples/quickstart.py).

## Storage layout

Documents are JSON files:

```
{root}/{collection}/{id}.json        # data/users/01HZY6C1MRK1P2Q0F4X8ZC5V9T.json
```

With sharding enabled (`shard_depth=2, shard_width=2`) the leading characters of
the id become directories, which keeps individual directories small:

```
data/users/01/HZ/01HZY6C1MRK1P2Q0F4X8ZC5V9T.json
```

Every document carries metadata written by GitDb:

| Field | Meaning |
| --- | --- |
| `_id` | Document id (also encoded in the file name) |
| `_created_at` | RFC 3339 UTC timestamp of the first write |
| `_updated_at` | RFC 3339 UTC timestamp of the latest write |
| `_rev` | Monotonic revision counter, starting at `1` |

Ids are generated as 26-character, ULID-style values: 10 characters of
millisecond timestamp plus 16 characters of randomness, so ids sort by creation
time. User-supplied ids are validated against `[A-Za-z0-9._-]` (must start
alphanumeric, max 128 characters) and path traversal (`..`, `/`, `\`) is
rejected.

## API reference

### `GitDb(repo, token=None, **options)`

| Option | Default | Description |
| --- | --- | --- |
| `repo` | – | `"owner/name"` of the backing repository |
| `token` | `None` | Personal access token with `contents:write` (optional in read-only mode) |
| `branch` | `"main"` | Branch used for reads and writes |
| `root` | `"data"` | Directory holding the collections (`""` for repository root) |
| `api_url` | `https://api.github.com` | Set to `https://ghe.example.com/api/v3` for GitHub Enterprise |
| `raw_url` | `https://raw.githubusercontent.com` | Base url used in read-only mode |
| `session` | `None` | Injectable `requests.Session` (custom transport, proxies, auth) |
| `auth` | `None` | Any `requests` auth object, e.g. for GitHub App flows |
| `max_retries` | `3` | Retries for rate limits and 5xx responses |
| `backoff_factor` | `0.5` | Base seconds for exponential backoff with jitter |
| `timeout` | `30.0` | Per-request timeout in seconds |
| `shard_depth` | `0` | Number of id-prefix directory levels (`0` disables sharding) |
| `shard_width` | `2` | Characters per shard level |
| `conflict_retries` | `2` | Automatic retries after a sha conflict |
| `cache` | `True` | Cache blob shas in memory |
| `read_only` | `False` | Fetch documents from `raw.githubusercontent.com` without a token |
| `committer` / `author` | `None` | `{"name": ..., "email": ...}` used for the commits |

Methods:

- `db.collection(name)` → `Collection`
- `db.batch(message=...)` → `Batch` (context manager)
- `db.history(collection, id, limit=30)` → commit history for one document
- `db.invalidate(path=None)` → drop cached blob shas (all, or a single path)
- `db.close()` / `with GitDb(...) as db:` → close the underlying session

### `Collection`

| Method | Description |
| --- | --- |
| `insert(document, id=None, message=None)` | Create a document, return its id. Raises `ConflictError` if the id exists |
| `get(id)` | Return the document or `None` |
| `exists(id)` | `True` when the document exists |
| `update(id, patch, message=None)` | Shallow-merge `patch`, return the new document |
| `replace(id, document, message=None)` | Overwrite the document wholesale |
| `upsert(id, document, message=None)` | Update when present, otherwise insert |
| `delete(id, message=None)` | Delete, raising `NotFoundError` when missing |
| `list(limit=None)` | Sorted ids in the collection |
| `all(limit=None)` | Iterator over documents (one API call per document) |
| `find(predicate, limit=None)` | Client-side filter over `all()` |
| `count()` / `len(collection)` | Number of documents |
| `history(id, limit=30)` | Commit history for one document |

### `Batch`

| Method | Description |
| --- | --- |
| `put(collection, id, document)` | Queue a create-or-replace |
| `insert(collection, document)` | Queue a document with a generated id, returns the id |
| `delete(collection, id)` | Queue a deletion |
| `commit()` | Flush as one commit, returns the commit sha (`None` when empty) |
| `operations` | Number of queued operations |

The context manager commits on a clean exit and discards the queue if the block
raises.

### Errors

```
GitDbError
├── NotFoundError    document/path missing (404)
├── ConflictError    blob sha mismatch or duplicate insert (409/422)
├── RateLimitError   rate limit exhausted after retries (403/429)
├── AuthError        bad credentials, missing scope, or write in read-only mode (401/403)
└── ValidationError  invalid id, collection name, repo or request body (422)
```

## Concurrency semantics

Single-document writes use optimistic concurrency: GitDb reads the blob `sha`
and sends it back on `PUT`/`DELETE`. If someone else committed in the meantime
GitHub rejects the write and GitDb raises `ConflictError`. `update`, `replace`,
`upsert` and `delete` automatically refetch the sha and retry up to
`conflict_retries` times (default `2`); set `conflict_retries=0` to surface
conflicts immediately.

Batches are a single commit created through the Git Data API (blobs → tree →
commit → ref update), so all documents in a batch land atomically in one commit.
The ref update is not forced, so a concurrent push to the same branch makes the
batch fail rather than silently overwrite history.

## Rate limits

Authenticated requests get 5,000 requests/hour on github.com. GitDb inspects
`X-RateLimit-Remaining`, `X-RateLimit-Reset` and `Retry-After`, sleeps for the
indicated duration (or exponential backoff with full jitter), and retries up to
`max_retries` times before raising `RateLimitError`. Transient 5xx responses are
retried with the same policy.

To keep request counts low:

- Prefer `db.batch(...)` over many single writes — one commit instead of one per document.
- Keep the sha cache enabled so `delete`/`update` can reuse a known sha.
- `list()` uses the Trees API with `recursive=1`, which returns the whole
  collection in one request; it transparently falls back to Contents listing
  when GitHub reports a truncated tree.

## Limitations

- **No server-side queries.** `find()` downloads documents and filters them in
  Python. There are no indexes, joins, sorting or aggregation on the server.
- **No ACID across commits.** A batch is atomic because it is one commit, but
  there are no multi-commit transactions, no isolation levels and no rollback of
  already-pushed commits.
- **API rate limits.** 5,000 requests/hour (authenticated) caps throughput, and
  the Contents API rejects files larger than ~1 MB for writes.
- **Repository size.** Every write adds a commit; repositories grow quickly and
  GitHub recommends staying below 1 GB. The Trees API also truncates very large
  trees.
- **Not for high write throughput.** Concurrent writers to the same branch will
  conflict; GitDb is designed for low-frequency writes and frequent reads.
- **Eventual consistency on raw reads.** Read-only mode uses
  `raw.githubusercontent.com`, which is cached and may lag behind a fresh commit.
- **Async client.** `AsyncGitDb` is not implemented yet — it is planned as
  `httpx.AsyncClient`-based future work, see [CHANGELOG.md](CHANGELOG.md).

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
pytest -q
```

Tests mock every HTTP call with `responses`; the suite never touches the
network.

## License

Apache-2.0. See [LICENSE](LICENSE).
