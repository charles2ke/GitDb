"""Use revision compare-and-set to handle competing GitDb writers.

Requires ``GITHUB_TOKEN`` and ``GITDB_REPO=owner/name``. Run
``python examples/concurrency_cas.py`` against a scratch repository. It shows
``expected_rev``, ``ConflictError`` retries, and a whole-batch precondition with
``Batch.expect()``.
"""

from __future__ import annotations

import os

from gitdb import ConflictError, GitDb


def main() -> None:
    repo, token = os.environ.get("GITDB_REPO"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise SystemExit("set GITDB_REPO=owner/name and GITHUB_TOKEN before running this example")
    db = GitDb(repo=repo, token=token)
    notes = db.collection("cas_notes")
    notes.upsert("shared", {"value": 0})
    first_view, second_view = notes.get("shared"), notes.get("shared")
    assert first_view is not None and second_view is not None
    notes.update("shared", {"value": 1}, expected_rev=first_view["_rev"])
    try:
        notes.update("shared", {"value": 2}, expected_rev=second_view["_rev"])
    except ConflictError:
        latest = notes.get("shared", fresh=True)
        assert latest is not None
        notes.update("shared", {"value": 2}, expected_rev=latest["_rev"])
        print("second writer conflicted, refreshed, and retried")
    # Without expected_rev, GitDb also retries a blob-SHA race automatically.
    notes.update("shared", {"value": 3})
    print("unconditional writes use GitDb's automatic conflict retry")
    latest = notes.get("shared")
    assert latest is not None
    with db.batch(message="CAS-protected batch") as batch:
        batch.expect("cas_notes", "shared", rev=latest["_rev"])
        batch.put("cas_notes", "audit", {"message": "shared note changed"})
    print("batch precondition succeeded")


if __name__ == "__main__":
    main()
