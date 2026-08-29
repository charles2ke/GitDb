"""GitDb Server: a small web UI that logs in to a repository and queries it.

Install ``pip install -r examples/server/requirements.txt`` and run
``uvicorn examples.server.main:app --reload``, then open http://127.0.0.1:8000.
Sign in with ``owner/name`` plus a GitHub token; the UI then lists the
collections ("tables") of the repository and queries them.

Credentials stay in memory for the lifetime of the session cookie and are never
written to disk, so run this next to the browser that uses it.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TypeVar

from fastapi import Body, Cookie, FastAPI, HTTPException, Response

from gitdb import GitDb, GitDbError, ValidationError

#: Name of the cookie holding the opaque session identifier.
SESSION_COOKIE = "gitdb_session"

#: Upper bound for the number of documents one query may return.
MAX_LIMIT = 500

INDEX_HTML = Path(__file__).with_name("static") / "index.html"

T = TypeVar("T")


class Sessions:
    """In-memory map of session id to logged-in :class:`GitDb` client."""

    def __init__(self) -> None:
        self._clients: Dict[str, GitDb] = {}

    def login(self, db: GitDb) -> str:
        token = secrets.token_urlsafe(32)
        self._clients[token] = db
        return token

    def get(self, token: Optional[str]) -> GitDb:
        client = self._clients.get(token) if token else None
        if client is None:
            raise HTTPException(status_code=401, detail="sign in first")
        return client

    def logout(self, token: Optional[str]) -> None:
        client = self._clients.pop(token, None) if token else None
        if client is not None:
            client.close()


def _text(payload: Mapping[str, Any], field: str, *, required: bool = True) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        if required:
            raise HTTPException(status_code=422, detail=f"{field} is required")
        return ""
    return value.strip()


def _limit(payload: Mapping[str, Any]) -> int:
    try:
        limit = int(payload.get("limit", 50))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="limit must be a number") from exc
    return max(1, min(limit, MAX_LIMIT))


def _run(operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call a GitDb operation, turning client errors into HTTP responses."""
    try:
        return operation(*args, **kwargs)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GitDbError as exc:
        status = exc.status if exc.status and 400 <= exc.status < 500 else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc


def _columns(documents: Sequence[Mapping[str, Any]]) -> List[str]:
    """Return the union of the document keys, metadata columns first."""
    seen: List[str] = []
    for document in documents:
        for key in document:
            if key not in seen:
                seen.append(key)
    leading = [key for key in ("_id", "_rev", "_updated_at") if key in seen]
    return leading + [key for key in seen if key not in leading]


def create_app(sessions: Optional[Sessions] = None) -> FastAPI:
    store = sessions if sessions is not None else Sessions()
    app = FastAPI(title="GitDb Server")

    @app.get("/")
    def index() -> Response:
        return Response(content=INDEX_HTML.read_text(encoding="utf-8"), media_type="text/html")

    @app.post("/api/login")
    def login(response: Response, payload: Mapping[str, Any] = Body(...)) -> Mapping[str, Any]:
        repo = _text(payload, "repo")
        token = _text(payload, "token")
        branch = _text(payload, "branch", required=False) or "main"
        root = _text(payload, "root", required=False) or "data"
        try:
            db = GitDb(repo=repo, token=token, branch=branch, root=root)
        except GitDbError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        collections = _run(db.collections)
        response.set_cookie(
            SESSION_COOKIE,
            store.login(db),
            httponly=True,
            samesite="strict",
            path="/",
        )
        return {"repo": repo, "branch": branch, "root": root, "collections": collections}

    @app.post("/api/logout", status_code=204)
    def logout(response: Response, gitdb_session: Optional[str] = Cookie(default=None)) -> None:
        store.logout(gitdb_session)
        response.delete_cookie(SESSION_COOKIE, path="/")

    @app.get("/api/collections")
    def collections(gitdb_session: Optional[str] = Cookie(default=None)) -> Mapping[str, Any]:
        db = store.get(gitdb_session)
        return {"repo": db.repo, "branch": db.branch, "collections": _run(db.collections)}

    @app.post("/api/query")
    def query(
        payload: Mapping[str, Any] = Body(...),
        gitdb_session: Optional[str] = Cookie(default=None),
    ) -> Mapping[str, Any]:
        db = store.get(gitdb_session)
        collection = _run(db.collection, _text(payload, "collection"))
        field = _text(payload, "field", required=False)
        limit = _limit(payload)
        if field:
            documents = _run(collection.find_by, field, payload.get("value", ""), limit=limit)
        else:
            documents = list(_run(collection.all, limit))
        return {
            "collection": collection.name,
            "documents": documents,
            "columns": _columns(documents),
        }

    return app


app = create_app()
