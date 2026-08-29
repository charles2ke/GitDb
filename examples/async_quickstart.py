"""Runnable GitDb quickstart for the asyncio client.

Usage:

    pip install "gitdb-py[async]"
    export GITHUB_TOKEN=ghp_...            # needs contents:write on the repo
    export GITDB_REPO=owner/name
    python examples/async_quickstart.py

``AsyncGitDb`` mirrors the synchronous surface: same options, same method names,
same errors, with ``await`` in front. Reads behind ``all()``/``find()`` fan out
concurrently, bounded by the ``concurrency`` option.
"""

from __future__ import annotations

import asyncio
import os

from gitdb import AsyncGitDb, NotFoundError


async def main() -> None:
    repo = os.environ.get("GITDB_REPO")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise SystemExit("set GITDB_REPO and GITHUB_TOKEN first")

    async with AsyncGitDb(
        repo=repo,
        token=token,
        branch=os.environ.get("GITDB_BRANCH", "main"),
        root="data",
        concurrency=8,
        indexes={"users": ["email"]},
    ) as db:
        users = db.collection("users")

        user_id = await users.insert({"name": "Ada", "email": "ada@example.com"})
        print("inserted", user_id)
        print("fetched", await users.get(user_id))

        await users.update(user_id, {"email": "ada@lovelace.dev"})

        async with db.batch(message="seed more users") as batch:
            batch.put("users", "grace", {"name": "Grace Hopper"})
            batch.put("users", "alan", {"name": "Alan Turing"})

        print("collection now holds", await users.count(), "documents")
        print("by email:", [d["_id"] for d in await users.find_by("email", "ada@lovelace.dev")])

        async for document in users:
            print("document:", document["_id"], document.get("name"))

        for doc_id in ("grace", "alan", user_id):
            try:
                await users.delete(doc_id)
            except NotFoundError:
                pass
        print("cleaned up")


if __name__ == "__main__":
    asyncio.run(main())
