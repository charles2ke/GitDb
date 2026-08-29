# GitDb sample clients

All examples need Python 3.9+, an installed `gitdb-py`, and a scratch GitHub
repository. Create a fine-grained personal access token with **Contents:
Read and write** permission for that repository, then configure:

```bash
export GITHUB_TOKEN=github_pat_...
export GITDB_REPO=owner/scratch-repository
```

Do not use a production repository: most examples write sample documents.
Install optional asyncio dependencies with `pip install -r examples/requirements.txt`;
the web service has its own requirements file.

| Example | Description |
| --- | --- |
| [`quickstart.py`](quickstart.py) | Basic sync API tour. |
| [`async_quickstart.py`](async_quickstart.py) | Basic asyncio API tour. |
| [`crud_cli.py`](crud_cli.py) | CRUD command-line client with JSON file/stdin input. |
| [`bulk_import.py`](bulk_import.py) | Chunked CSV or JSON-lines importer with dry-run support. |
| [`indexed_queries.py`](indexed_queries.py) | Indexed lookup versus client-side scan. |
| [`concurrency_cas.py`](concurrency_cas.py) | Revision and batch compare-and-set handling. |
| [`snapshots_history.py`](snapshots_history.py) | Pinned reads, history, restore, and revert guidance. |
| [`async_client.py`](async_client.py) | Bounded concurrent reads and an async batch write. |
| [`webapp/`](webapp/) | FastAPI read/write notes REST service. |

Each script has its own usage header; run `python examples/<script>.py --help`
for the command-line examples.
