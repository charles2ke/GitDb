"""Runnable GitDb quickstart.

Usage:

    export GITHUB_TOKEN=ghp_...            # needs contents:write on the repo
    export GITDB_REPO=owner/name
    python examples/quickstart.py

The script creates, reads, updates, batch-writes and deletes documents in the
``data/users`` folder of the target repository, so point it at a scratch repo.
"""

from __future__ import annotations

import os

from gitdb import GitDb, NotFoundError


def main() -> None:
    repo = os.environ.get("GITDB_REPO")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise SystemExit("set GITDB_REPO and GITHUB_TOKEN first")

    db = GitDb(repo=repo, token=token, branch=os.environ.get("GITDB_BRANCH", "main"), root="data")
    users = db.collection("users")

    user_id = users.insert({"name": "Ada", "email": "ada@example.com"})
    print("inserted", user_id)

    print("fetched", users.get(user_id))

    users.update(user_id, {"email": "ada@lovelace.dev"})
    print("updated", users.get(user_id))

    with db.batch(message="seed more users") as batch:
        batch.put("users", "grace", {"name": "Grace Hopper"})
        batch.put("users", "alan", {"name": "Alan Turing"})
    print("collection now holds", users.count(), "documents")

    print("names starting with A:", [d["name"] for d in users.find(lambda d: d["name"][0] == "A")])

    for entry in users.history(user_id):
        print("history:", entry["date"], entry["message"])

    for doc_id in ("grace", "alan", user_id):
        try:
            users.delete(doc_id)
        except NotFoundError:
            pass
    print("cleaned up")


if __name__ == "__main__":
    main()
