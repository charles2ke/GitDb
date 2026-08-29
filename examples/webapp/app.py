"""Small FastAPI service backed by a GitDb collection.

Requires ``GITHUB_TOKEN`` and ``GITDB_REPO=owner/name``. Install
``pip install -r examples/webapp/requirements.txt`` and run
``uvicorn examples.webapp.app:app --reload``. GET requests use a pinned Git
snapshot; POST and DELETE requests write to the configured scratch repository.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping, Optional

from fastapi import FastAPI, HTTPException

from gitdb import GitDb, NotFoundError


def make_db() -> GitDb:
    repo, token = os.environ.get("GITDB_REPO"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise RuntimeError("set GITDB_REPO=owner/name and GITHUB_TOKEN before starting the service")
    return GitDb(repo=repo, token=token)


def create_app(db: Optional[GitDb] = None) -> FastAPI:
    database = db

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal database
        if database is None:
            database = make_db()
        yield

    app = FastAPI(title="GitDb notes", lifespan=lifespan)

    def configured_db() -> GitDb:
        if database is None:
            raise RuntimeError("application has not started")
        return database

    @app.get("/notes")
    def list_notes() -> list[Mapping[str, Any]]:
        snapshot = configured_db().snapshot()
        return list(snapshot.collection("notes").all())

    @app.get("/notes/{note_id}")
    def get_note(note_id: str) -> Mapping[str, Any]:
        snapshot = configured_db().snapshot()
        document = snapshot.collection("notes").get(note_id)
        if document is None:
            raise HTTPException(status_code=404, detail="note not found")
        return document

    @app.post("/notes/{note_id}", status_code=201)
    def put_note(note_id: str, document: Mapping[str, Any]) -> Mapping[str, Any]:
        return configured_db().collection("notes").upsert(note_id, document)

    @app.delete("/notes/{note_id}", status_code=204)
    def delete_note(note_id: str) -> None:
        try:
            configured_db().collection("notes").delete(note_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="note not found") from exc

    return app


app = create_app()
