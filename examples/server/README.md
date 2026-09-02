# GitDb Server

A small web UI for a GitDb repository: sign in with a repository and token,
browse the collections ("tables") it contains, and query them.

A backend-free build of this UI is published to GitHub Pages at
<https://charles2ke.github.io/GitDb/> (source in [`site/`](../../site/)); use it
if you just want to browse a repository. Run the FastAPI version below when you
want the HTTP API or want the token to stay on a server you control.

```bash
pip install -r examples/server/requirements.txt
uvicorn examples.server.main:app --reload
```

Then open <http://127.0.0.1:8000> and sign in with:

![GitDb Server sign-in form](../../docs/images/server-sign-in.png)

| Field | Meaning |
| --- | --- |
| Repository | `owner/name` of the backing repository. |
| Token | GitHub token with **Contents: Read** permission for that repository. |
| Branch | Branch to read, `main` by default. |
| Data root | Directory holding the collections, `data` by default. |

The sidebar lists every collection under the data root (the derived `_index`
and `_manifest` directories are hidden). Selecting one runs a query; the form
also filters by field value — indexed fields use the index, everything else
falls back to a client-side scan — and caps the number of returned documents.

![GitDb Server listing the documents of a collection](../../docs/images/server-browse.png)

![GitDb Server filtering a collection by field value](../../docs/images/server-query.png)

The token is exchanged for an opaque, `HttpOnly` session cookie and is kept in
the server process memory only. Sessions are per process and are dropped on
sign-out or restart, so run the server locally next to the browser that uses it
rather than exposing it to a network.

## HTTP API

| Endpoint | Description |
| --- | --- |
| `POST /api/login` | `{repo, token, branch, root}`, sets the session cookie. |
| `GET /api/collections` | Collection names for the signed-in session. |
| `POST /api/query` | `{collection, field, value, limit}` → documents and columns. |
| `POST /api/logout` | Closes the session. |
