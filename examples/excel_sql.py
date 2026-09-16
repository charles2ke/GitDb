"""Import from another database and export a collection to Excel.

Requires ``GITHUB_TOKEN`` and ``GITDB_REPO=owner/name``. Examples:

```bash
# relational: any PEP 249 connection (sqlite3 here, psycopg/MySQL work the same)
python examples/excel_sql.py import users --sqlite app.db --query "SELECT * FROM users"

# non-relational: a MongoDB-style collection (needs pymongo installed)
python examples/excel_sql.py import users --mongo mongodb://localhost:27017/app.users

# export the collection to a workbook openable in Excel
python examples/excel_sql.py export users --out users.xlsx
```

``--dry-run`` validates an import without writing; imports commit one chunk at
a time rather than one commit per document.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from gitdb import GitDb, from_mongo, from_sql


def sql_records(database: str, query: str, chunk_size: int) -> Iterable[Mapping[str, Any]]:
    connection = sqlite3.connect(database)
    return from_sql(connection, query, fetch_size=chunk_size)


def mongo_records(uri: str) -> Iterable[Mapping[str, Any]]:
    """Read from ``mongodb://host/database.collection`` using pymongo."""
    try:
        from pymongo import MongoClient
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit("pip install pymongo to import from MongoDB") from exc
    base, _, target = uri.rpartition("/")
    database, _, collection = target.partition(".")
    if not database or not collection:
        raise SystemExit("use mongodb://host/database.collection")
    return from_mongo(MongoClient(f"{base}/{database}")[database][collection])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Import from a database, export to Excel.")
    commands = result.add_subparsers(dest="command", required=True)

    importer = commands.add_parser("import", help="import rows from another database")
    importer.add_argument("collection")
    importer.add_argument("--sqlite", help="path to a SQLite database file")
    importer.add_argument("--query", default="SELECT * FROM users")
    importer.add_argument("--mongo", help="mongodb://host/database.collection")
    importer.add_argument("--id-field", default="_id")
    importer.add_argument("--chunk-size", type=int, default=100)
    importer.add_argument("--dry-run", action="store_true")

    exporter = commands.add_parser("export", help="export a collection to an .xlsx workbook")
    exporter.add_argument("collection", nargs="?")
    exporter.add_argument("--out", type=Path, default=Path("gitdb-export.xlsx"))
    exporter.add_argument("--limit", type=int)

    result.add_argument("--branch", default=os.environ.get("GITDB_BRANCH", "main"))
    return result


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parser().parse_args(argv)
    repo, token = os.environ.get("GITDB_REPO"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise SystemExit("set GITDB_REPO=owner/name and GITHUB_TOKEN before running this example")
    db = GitDb(repo=repo, token=token, branch=args.branch)

    if args.command == "import":
        if bool(args.sqlite) == bool(args.mongo):
            raise SystemExit("pass exactly one of --sqlite or --mongo")
        records = (
            sql_records(args.sqlite, args.query, args.chunk_size)
            if args.sqlite
            else mongo_records(args.mongo)
        )
        count = db.collection(args.collection).import_records(
            records,
            id_field=args.id_field,
            chunk_size=args.chunk_size,
            dry_run=args.dry_run,
        )
        print(f"{'would import' if args.dry_run else 'imported'} {count} records")
        return

    with args.out.open("wb") as handle:
        if args.collection:
            columns = db.collection(args.collection).export_excel(handle, limit=args.limit)
            print(f"wrote {args.out} ({len(columns)} columns)")
        else:
            sheets = db.export_excel(handle, limit=args.limit)
            print(f"wrote {args.out} ({len(sheets)} sheets)")


if __name__ == "__main__":
    main()
