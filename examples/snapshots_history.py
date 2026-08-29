"""Read GitDb history and undo an accidental write without rewriting history.

Requires ``GITHUB_TOKEN`` and ``GITDB_REPO=owner/name``. Run
``python examples/snapshots_history.py`` against a scratch repository. It pins a
snapshot with ``snapshot()``/``at()``, prints document history, then restores a
prior document version and demonstrates repository-level ``revert()``.
"""

from __future__ import annotations

import os

from gitdb import GitDb


def main() -> None:
    repo, token = os.environ.get("GITDB_REPO"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise SystemExit("set GITDB_REPO=owner/name and GITHUB_TOKEN before running this example")
    db = GitDb(repo=repo, token=token)
    posts = db.collection("posts")
    posts.upsert("welcome", {"title": "Welcome"})
    snapshot = db.snapshot()
    pinned = snapshot.resolve_ref()
    posts.update("welcome", {"title": "Accidental title"})
    bad_commit = db.resolve_ref(refresh=True)
    snapshot_document = snapshot.collection("posts").get("welcome")
    assert snapshot_document is not None
    print(f"snapshot {pinned[:7]} sees:", snapshot_document["title"])
    print("history:", [entry["message"] for entry in posts.history("welcome")])
    db.revert(bad_commit)
    reverted = posts.get("welcome")
    assert reverted is not None
    print("reverted bad write:", reverted["title"])
    posts.restore("welcome", pinned)
    restored = posts.get("welcome")
    assert restored is not None
    print("restored:", restored["title"])


if __name__ == "__main__":
    main()
