# GitDb notes service

Install the sample's dependencies and point it at a scratch GitHub repository:

```bash
export GITHUB_TOKEN=github_pat_...
export GITDB_REPO=owner/scratch-repository
pip install -r examples/webapp/requirements.txt
uvicorn examples.webapp.app:app --reload
```

The token needs repository contents read/write permission. `GET /notes` and
`GET /notes/{id}` create a GitDb snapshot first, so both reads come from one
immutable commit. `POST /notes/{id}` accepts a JSON object and writes it; delete
with `DELETE /notes/{id}`.
