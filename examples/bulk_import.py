"""Import CSV or JSON-lines data into GitDb with commit-sized batches.

Requires ``GITHUB_TOKEN`` and ``GITDB_REPO=owner/name``. Run
``python examples/bulk_import.py users records.jsonl --chunk-size 100``.
CSV headers become document keys. Use ``--dry-run`` to validate input without
writing; batching makes one Git commit per chunk rather than per document.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from gitdb import GitDb


def records(path: Path) -> Iterator[Mapping[str, Any]]:
    with path.open(encoding="utf-8", newline="") as source:
        if path.suffix.lower() == ".csv":
            yield from csv.DictReader(source)
        else:
            for line_number, line in enumerate(source, 1):
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError(f"line {line_number} is not a JSON object")
                    yield value


def chunks(items: Iterable[Mapping[str, Any]], size: int) -> Iterator[list[Mapping[str, Any]]]:
    iterator = iter(items)
    while chunk := list(islice(iterator, size)):
        yield chunk


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Import CSV or JSON-lines into GitDb.")
    result.add_argument("collection")
    result.add_argument("file", type=Path)
    result.add_argument("--chunk-size", type=int, default=100)
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--branch", default=os.environ.get("GITDB_BRANCH", "main"))
    return result


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parser().parse_args(argv)
    if args.chunk_size < 1:
        raise SystemExit("--chunk-size must be positive")
    repo, token = os.environ.get("GITDB_REPO"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise SystemExit("set GITDB_REPO=owner/name and GITHUB_TOKEN before running this example")
    db = GitDb(repo=repo, token=token, branch=args.branch)
    imported = 0
    for number, chunk in enumerate(chunks(records(args.file), args.chunk_size), 1):
        if not args.dry_run:
            with db.batch(message=f"import {args.collection} chunk {number}") as batch:
                for document in chunk:
                    raw_id = document.get("_id")
                    doc_id = str(raw_id) if raw_id is not None and raw_id != "" else str(imported)
                    batch.put(args.collection, doc_id, document)
                    imported += 1
        else:
            imported += len(chunk)
        print(f"{'would import' if args.dry_run else 'imported'} {imported} records")


if __name__ == "__main__":
    main()
