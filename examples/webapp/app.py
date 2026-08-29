"""Small FastAPI service backed by a GitDb collection.

Requires ``GITHUB_TOKEN`` and ``GITDB_REPO=owner/name``. Install
``pip install -r examples/webapp/requirements.txt`` and run
``uvicorn examples.webapp.app:app --reload``. GET requests use a pinned Git
snapshot; POST and DELETE requests write to the configured scratch repository.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from fastapi import FastAPI, HTTPException

from gitdb import GitDb, NotFoundError


def make_db() -> GitDb:
    repo, token = os.environ.get("GITDB_REPO"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise RuntimeError("set GITDB_REPO=owner/name and GITHUB_TOKEN before starting the service")
    return GitDb(repo=repo, token=token)


def create_app(db: Optional[GitDb] = None) -> FastAPI:
    app = FastAPI(title="GitDb notes")
    database = db or make_db()
    notes = database.collection("notes")

    @app.get("/notes")
    def list_notes() -> list[Mapping[str, Any]]:
        snapshot = database.snapshot()
        return list(snapshot.collection("notes").all())

    @app.get("/notes/{note_id}")
    def get_note(note_id: str) -> Mapping[str, Any]:
        snapshot = database.snapshot()
        document = snapshot.collection("notes").get(note_id)
        if document is None:
            raise HTTPException(status_code=404, detail="note not found")
        return document

    @app.post("/notes/{note_id}", status_code=201)
    def put_note(note_id: str, document: Mapping[str, Any]) -> Mapping[str, Any]:
        return notes.upsert(note_id, document)

    @app.delete("/notes/{note_id}", status_code=204)
    def delete_note(note_id: str) -> None:
        try:
            notes.delete(note_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="note not found") from exc

    return app


app = create_app() if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITDB_REPO") else FastAPI()
