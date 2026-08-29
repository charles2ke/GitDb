"""Runnable GitDb quickstart.

Usage:

    export GITHUB_TOKEN=ghp_...            # needs contents:write on the repo
    export GITDB_REPO=owner/name
    python examples/quickstart.py

The script creates, reads, updates, batch-writes and deletes documents in the
``data/users`` folder of the target repository, so point it at a scratch repo.
It also demonstrates the 0.2 additions: indexed lookups, pagination, snapshots,
coalesced writes and rate-limit inspection.

``examples/async_quickstart.py`` shows the same flow with ``AsyncGitDb``.
"""

from __future__ import annotations

import os

from gitdb import GitDb, NotFoundError


def main() -> None:
    repo = os.environ.get("GITDB_REPO")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise SystemExit("set GITDB_REPO and GITHUB_TOKEN first")

    db = GitDb(
        repo=repo,
        token=token,
        branch=os.environ.get("GITDB_BRANCH", "main"),
        root="data",
        concurrency=4,
        indexes={"users": ["email"]},
    )
    users = db.collection("users")

    limit = db.rate_limit()
    print(f"rate limit: {limit.remaining}/{limit.limit} remaining")

    user_id = users.insert({"name": "Ada", "email": "ada@example.com"})
    print("inserted", user_id)

    print("fetched", users.get(user_id))

    users.update(user_id, {"email": "ada@lovelace.dev"})
    print("updated", users.get(user_id))

    with db.batch(message="seed more users") as batch:
        batch.put("users", "grace", {"name": "Grace Hopper"})
        batch.put("users", "alan", {"name": "Alan Turing"})
    with db.writer(max_operations=2) as writer:
        writer.put("users", "edsger", {"name": "Edsger Dijkstra"})
        writer.put("users", "barbara", {"name": "Barbara Liskov"})
    print("writer flushed", len(writer.commits), "commit(s)")

    print("collection now holds", users.count(), "documents")

    # Indexed equality lookup: one request instead of scanning the collection.
    print("by email:", [d["_id"] for d in users.find_by("email", "ada@lovelace.dev")])

    print("names starting with A:", [d["name"] for d in users.find(lambda d: d["name"][0] == "A")])

    for page in users.pages(size=2):
        print("page:", [d["_id"] for d in page])

    snapshot = db.snapshot()
    print("snapshot pinned at", snapshot.resolve_ref()[:7])

    for entry in users.history(user_id):
        print("history:", entry["date"], entry["message"])

    for doc_id in ("grace", "alan", "edsger", "barbara", user_id):
        try:
            users.delete(doc_id)
        except NotFoundError:
            pass
    print("cleaned up")


if __name__ == "__main__":
    main()
