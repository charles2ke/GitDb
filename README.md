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

With the asyncio client:

```bash
pip install "gitdb-py[async]"
```

From source:

```bash
pip install -e ".[dev]"
```

Requires Python 3.9+ and `requests`; the async client additionally needs `httpx`.

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

Or asynchronously:

```python
from gitdb import AsyncGitDb

async with AsyncGitDb(repo="owner/name", token=token, concurrency=8) as db:
    users = db.collection("users")
    await users.upsert("ada", {"name": "Ada"})
    async for user in users:
        print(user["name"])
```

A runnable script lives in [`examples/quickstart.py`](examples/quickstart.py).
For complete client applications (CLI, bulk import, indexed queries, snapshots,
async, and a small web service), see the [examples guide](examples/README.md).

## GitDb Server

**GitDb Server** puts a browser UI in front of a GitDb repository: sign in with
a repository and token, browse the collections ("tables") it contains, and
query them. It is published to GitHub Pages at
**<https://charles2ke.github.io/GitDb/>** and there is nothing to install: the
page is static and talks to the GitHub REST API from your browser. The token is
kept in memory for the tab only — it is never stored and never sent anywhere but
`api.github.com` — and a token is only needed for private repositories. The
source lives in [`site/`](site/) and is deployed by
[`.github/workflows/pages.yml`](.github/workflows/pages.yml) (enable Pages with
the *GitHub Actions* source once, under **Settings → Pages**).

[`examples/server/`](examples/server/) ships the same UI as a small FastAPI
application, for running it locally against a repository:

```bash
pip install -r examples/server/requirements.txt
uvicorn examples.server.main:app --reload
```

Then open <http://127.0.0.1:8000> and sign in with the repository (`owner/name`),
a GitHub token with **Contents: Read** permission, the branch to read (`main` by
default) and the data root holding the collections (`data` by default).

![GitDb Server sign-in form](https://raw.githubusercontent.com/charles2ke/GitDb/main/docs/images/server-sign-in.png)

The sidebar lists every collection under the data root; the derived `_index` and
`_manifest` directories are hidden. Selecting one runs a query and renders the
documents as a table, with `_id`, `_rev` and `_updated_at` first.

![GitDb Server listing the documents of a collection](https://raw.githubusercontent.com/charles2ke/GitDb/main/docs/images/server-browse.png)

The query form filters by field value and caps how many documents come back
(500 at most). Indexed fields are served from the index, any other field falls
back to a client-side scan.

![GitDb Server filtering a collection by field value](https://raw.githubusercontent.com/charles2ke/GitDb/main/docs/images/server-query.png)

The token is exchanged for an opaque, `HttpOnly` session cookie and only ever
lives in the server process memory: sessions are per process and are dropped on
sign-out or restart. Run it locally next to the browser that uses it rather than
exposing it to a network. Besides the UI it exposes `POST /api/login`,
`GET /api/collections`, `POST /api/query` and `POST /api/logout`; see the
[server README](examples/server/README.md) for the payloads. The hosted build has
no backend and therefore no HTTP API: it performs the same reads directly against
the GitHub REST API.

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

Optional [indexes and manifests](#indexes-and-manifests) live alongside the
collections and are written in the same commit as the documents they describe:

```
{root}/_index/{collection}/{field}.json    # {"values": {"ada@example.com": ["ada"]}, ...}
{root}/_manifest/{collection}.json         # {"count": N, "ids": [...], "documents": {...}}
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
| `batch_retries` | `2` | Retries after a batch ref update loses a race |
| `cache` | `True` | `True`/`False`, or any [`Cache`](#caching) implementation |
| `concurrency` | `1` | Bounded thread pool used by `all()`, `find()` and `count()` |
| `indexes` | `None` | `{"users": ["email"]}` — [secondary indexes](#indexes-and-manifests) |
| `manifests` | `None` | `["users"]` or `{"users": ["name"]}` — per-collection manifests |
| `use_graphql` | `False` | Fetch documents in bulk through the GraphQL API |
| `graphql_batch_size` | `50` | Aliased lookups per GraphQL request |
| `contents_max_bytes` | `900_000` | Above this, writes/reads route through the Git Data blob API |
| `pace_requests` | `True` | Self-throttle from `X-RateLimit-Remaining`/`Reset` |
| `read_only` | `False` | Fetch documents from `raw.githubusercontent.com` without a token |
| `ref` | `None` | Read a fixed commit sha or tag (implies `read_only=True`) |
| `pin_ref` | `True` | Resolve the branch to a commit sha for immutable raw reads |
| `committer` / `author` | `None` | `{"name": ..., "email": ...}` used for the commits |

Methods:

- `db.collection(name)` → `Collection`
- `db.collections()` → sorted collection names stored under `root`
- `db.batch(message=...)` → `Batch` (context manager)
- `db.writer(max_operations=100, max_seconds=5.0)` → `Writer` coalescing writes
- `db.transaction(message=...)` → `Transaction` over a temporary work branch
- `db.at(commit_sha)` / `db.snapshot()` → read-only view pinned to a commit
- `db.on_branch(branch)` → view of another branch
- `db.resolve_ref(refresh=False)` → commit sha the client currently reads from
- `db.history(collection, id, limit=30)` → commit history for one document
- `db.revert(commit_sha, message=None)` → forward commit undoing another commit
- `db.reindex(collection)` → rebuild index and manifest files
- `db.compact(confirm=True)` → squash history into a single commit (**destructive**)
- `db.rate_limit(resource="core")` → current `RateLimit` without spending quota
- `db.invalidate(path=None)` → drop cached shas/ETags (all, or a single path)
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
| `list(limit=None, after=None)` | Sorted ids in the collection |
| `all(limit=None, after=None)` | Documents, fetched in bulk and in parallel |
| `find(predicate, limit=None)` | Client-side filter over `all()` |
| `find_by(field, value, limit=None)` | Index-backed lookup (1–2 requests when indexed) |
| `page(limit=100, after=None)` | One `Page(documents, cursor)`; `cursor is None` at the end |
| `pages(size=100)` | Iterator over pages |
| `count()` / `len(collection)` | Number of documents |
| `history(id, limit=30)` | Commit history for one document |
| `restore(id, commit_sha)` | Restore an earlier version as a new commit |
| `reindex()` | Rebuild this collection's index/manifest files |

`insert`, `get`, `update`, `replace`, `upsert` and `delete` accept extra
keyword arguments: `expected_rev=` for explicit compare-and-set on
`update`/`replace`/`delete`, and `fresh=True` on `get` to bypass caches.

### `Batch`

| Method | Description |
| --- | --- |
| `put(collection, id, document)` | Queue a create-or-replace |
| `insert(collection, document)` | Queue a document with a generated id, returns the id |
| `delete(collection, id)` | Queue a deletion |
| `expect(collection, id, rev=..., sha=...)` | Require a document version before committing |
| `commit()` | Flush as one commit, returns the commit sha (`None` when empty) |
| `operations` / `expectations` | Queued operation and precondition counts |

The context manager commits on a clean exit and discards the queue if the block
raises. Preconditions are checked against the base tree *before* any blob is
uploaded, and a lost ref-update race is retried up to `batch_retries` times
against the new head.

### `Writer` and `Transaction`

```python
with db.writer(max_operations=50, max_seconds=5.0) as w:  # coalesce into commits
    for record in records:
        w.put("users", record["id"], record)

with db.transaction(message="migrate") as tx:  # work branch + fast-forward
    tx.collection("users").upsert("ada", {"name": "Ada"})
    with tx.batch() as b:
        b.put("users", "grace", {"name": "Grace"})
```

`Writer` flushes every `max_operations` writes or `max_seconds` seconds (and
on exit).
`Transaction` applies several commits to a temporary branch and then advances
the target branch in a single ref update, rolling back by deleting the work
branch if the block raises.

### `AsyncGitDb`

```bash
pip install "gitdb-py[async]"
```

`AsyncGitDb`, `AsyncCollection` and `AsyncBatch` mirror the synchronous surface
on top of `httpx.AsyncClient` — same options, same method names, same errors,
with `await` in front and `async with` for the context managers:

```python
from gitdb import AsyncGitDb

async with AsyncGitDb(repo="owner/name", token=token, concurrency=8) as db:
    users = db.collection("users")
    await users.insert({"name": "Ada"})
    documents = await users.all()
    async for page in users.pages(size=100):
        ...
    async with db.batch(message="seed") as b:
        b.put("users", "grace", {"name": "Grace"})
```

`all()`, `find()` and `count()` fan out with `asyncio.gather`, bounded by
`concurrency`. `Writer` and `Transaction` are synchronous-only for now.

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

`update`, `replace` and `delete` also take `expected_rev=`, which fails with
`ConflictError` unless the stored document is at that `_rev` — use it when a
blind overwrite would be wrong.

Batches are a single commit created through the Git Data API (blobs → tree →
commit → ref update), so all documents in a batch land atomically in one commit.
The ref update is not forced, so a concurrent push to the same branch cannot
silently overwrite history: GitDb rebuilds the tree on the new head and retries
up to `batch_retries` times, then raises `ConflictError`.

The precise guarantees are: **read-committed at branch head, atomic per commit,
no isolation levels.** Reads see whatever the branch (or pinned commit) contains
at the time of the request. Anything wider than one commit is not a transaction
— `db.transaction()` gets close by staging commits on a work branch and
fast-forwarding once, but the fast-forward itself is the only atomic step.

Undo is always a forward commit: `collection.restore(id, commit_sha)` brings an
old version back and `db.revert(commit_sha)` undoes a whole commit. Neither
rewrites history.

## Snapshots and pinned reads

```python
snap = db.snapshot()  # pin to the current branch head
old = db.at("abc123...")  # pin to a specific commit
```

Views are read-only, share the HTTP session, and keep their own cache. In
read-only mode GitDb resolves the branch to a commit sha once (`pin_ref=True`)
and reads `raw.githubusercontent.com/{repo}/{sha}/{path}`, which is immutable
and therefore safe to cache; pass `pin_ref=False` for the older
branch-name behaviour, or `get(id, fresh=True)` to force a revalidated read.

## Indexes and manifests

```python
db = GitDb(
    repo="owner/name", token=token, indexes={"users": ["email"]}, manifests={"users": ["name"]}
)
db.collection("users").find_by("email", "ada@example.com")
```

Index files (`{root}/_index/{collection}/{field}.json`) map field values to ids
and are written **in the same commit** as the document, so they never disagree
with the data. Manifests (`{root}/_manifest/{collection}.json`) hold the id list
plus projected fields, making `list()` and `count()` a single request.

Both are opt-in per collection because they cause write amplification: every
write to an indexed collection also rewrites the index. Use `reindex()` to
rebuild them after direct pushes or configuration changes.

## Rate limits

Authenticated requests get 5,000 requests/hour on github.com. GitDb inspects
`X-RateLimit-Remaining`, `X-RateLimit-Reset` and `Retry-After`, sleeps for the
indicated duration (or exponential backoff with full jitter), and retries up to
`max_retries` times before raising `RateLimitError`. Transient 5xx responses are
retried with the same policy.

With `pace_requests=True` (the default) the client also throttles *before*
sending, spreading the remaining quota over the time left until reset, and
`db.rate_limit()` reports the current budget without spending any of it.
`RateLimitError` carries `.remaining`, `.reset` and `.reset_at`.

To keep request counts low:

- Prefer `db.batch(...)` over many single writes — one commit instead of one per
  document — or `db.writer(...)` to coalesce a stream of writes automatically.
- Keep the cache enabled: GitDb stores ETags next to blob shas and revalidates
  with `If-None-Match`; GitHub does not charge 304 responses against the quota.
  Pass any `Cache` implementation (disk, Redis, …) to share it across processes.
- `list()` asks for the collection subtree only, and `all()` reads the document
  bodies straight from the Git blob shas returned by that listing. Raise
  `concurrency` to fan those reads out, or set `use_graphql=True` to collapse
  them into a handful of GraphQL requests.
- Index a collection and use `find_by()` instead of scanning with `find()`.
- For higher quotas, authenticate as a GitHub App installation:

  ```python
  from gitdb import GitDb, InstallationTokenAuth

  db = GitDb(repo="owner/name", auth=InstallationTokenAuth(fetch_token))
  ```

  `fetch_token` returns a token or a `(token, expires_at)` pair and is called
  again shortly before expiry. Minting is left to you, so GitDb needs no JWT
  dependency.

## Maintenance operations

- `db.reindex(collection)` rebuilds index and manifest files after direct pushes.
- `db.compact(confirm=True)` replaces the branch with a single commit carrying
  the current tree. It **discards history** and force-updates the ref; use it
  only when history growth, rather than content size, is the problem.

## Limitations

These are properties of Git and GitHub, not of the client, so they can be
mitigated but not removed:

- **No true multi-commit ACID.** One commit is atomic and reads are
  read-committed at the branch head, but there are no isolation levels.
  `db.transaction()` stages work on a branch and fast-forwards once, which is
  the closest achievable analogue; `restore()`/`revert()` undo by committing
  forward rather than rewriting history.
- **API rate limits.** 5,000 requests/hour for a personal token caps throughput.
  ETag revalidation, caching, bulk blob reads, batching and proactive pacing all
  push against it, and GitHub App installation tokens raise the ceiling, but the
  ceiling exists.
- **Repository size.** Every write adds a commit, so a write-heavy repository
  grows steadily; GitHub recommends staying below 1 GB. Keep data on a dedicated
  branch or repository separate from code, enable `shard_depth` for large
  collections, split very large datasets across repositories, and use
  `db.compact()` when history is the problem.
- **Single-branch write serialization.** A branch ref is inherently serialized,
  so concurrent writers to the same branch contend. `db.writer()` coalescing and
  `batch_retries` absorb bursts; genuine write scaling means sharding across
  branches or repositories and merging.
- **Queries run client-side.** GitDb is not a query engine: `find()` still
  evaluates a Python predicate over the documents. Secondary indexes make
  equality lookups cheap (`find_by`) and manifests make listing cheap, but there
  are no joins, aggregations or server-side sorting beyond id order.

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
pytest -q
```

Tests mock every HTTP call with `responses` (and `respx` for the async client);
the suite never touches the network.

## License

Apache-2.0. See [LICENSE](LICENSE).
