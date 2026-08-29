"""Async GitDb client with bounded concurrent reads and a batch write.

Requires ``GITHUB_TOKEN`` and ``GITDB_REPO=owner/name`` plus
``pip install 'gitdb-py[async]'``. Run ``python examples/async_client.py``.
All work targets a scratch repository; ``concurrency`` bounds in-flight reads.
"""

from __future__ import annotations

import asyncio
import os

from gitdb import AsyncGitDb


async def main() -> None:
    repo, token = os.environ.get("GITDB_REPO"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise SystemExit("set GITDB_REPO=owner/name and GITHUB_TOKEN before running this example")
    async with AsyncGitDb(repo=repo, token=token, concurrency=4) as db:
        tasks = db.collection("tasks")
        async with db.batch(message="seed async tasks") as batch:
            for number in range(10):
                batch.put("tasks", f"task-{number}", {"number": number, "done": False})
        documents = await tasks.all()
        print(f"concurrently read {len(documents)} documents (maximum four requests at once)")


if __name__ == "__main__":
    asyncio.run(main())
