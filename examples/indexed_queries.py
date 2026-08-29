"""Compare indexed GitDb equality lookups with a client-side scan.

Requires ``GITHUB_TOKEN`` and ``GITDB_REPO=owner/name``. Run
``python examples/indexed_queries.py`` against a scratch repository. The script
configures an email index, seeds documents, calls ``reindex()``, then reports
timings for ``find_by()`` and a regular ``find()``.
"""

from __future__ import annotations

import os
import time

from gitdb import GitDb


def main() -> None:
    repo, token = os.environ.get("GITDB_REPO"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise SystemExit("set GITDB_REPO=owner/name and GITHUB_TOKEN before running this example")
    db = GitDb(repo=repo, token=token, indexes={"customers": ["email"]})
    customers = db.collection("customers")
    with db.batch(message="seed indexed query example") as batch:
        batch.put("customers", "ada", {"name": "Ada", "email": "ada@example.com"})
        batch.put("customers", "grace", {"name": "Grace", "email": "grace@example.com"})
    db.reindex("customers")

    started = time.perf_counter()
    indexed = customers.find_by("email", "ada@example.com")
    indexed_time = time.perf_counter() - started
    started = time.perf_counter()
    scanned = customers.find(lambda document: document.get("email") == "ada@example.com")
    scan_time = time.perf_counter() - started
    print(f"find_by: {[document['_id'] for document in indexed]} ({indexed_time:.4f}s)")
    print(f"find scan: {[document['_id'] for document in scanned]} ({scan_time:.4f}s)")


if __name__ == "__main__":
    main()
