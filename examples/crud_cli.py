"""Command-line CRUD client for a GitDb collection.

Requires ``GITHUB_TOKEN`` and ``GITDB_REPO=owner/name``. Run, for example:
``python examples/crud_cli.py users insert --json user.json`` or pipe JSON to
``python examples/crud_cli.py users insert --json -``. Use ``--help`` for all
subcommands. Writes should target a scratch repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping, Optional, Sequence

from gitdb import GitDb


def document_argument(value: str) -> Mapping[str, Any]:
    """Read a JSON object from a file, or stdin when value is ``-``."""
    try:
        if value == "-":
            contents = sys.stdin.read()
        else:
            with open(value, encoding="utf-8") as file:
                contents = file.read()
        document = json.loads(contents)
    except (OSError, json.JSONDecodeError) as exc:
        raise argparse.ArgumentTypeError(f"cannot read JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise argparse.ArgumentTypeError("JSON input must be an object")
    return document


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Operate on a GitDb collection.")
    result.add_argument("collection")
    result.add_argument("--branch", default=os.environ.get("GITDB_BRANCH", "main"))
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("insert", "update"):
        command = commands.add_parser(name)
        if name == "update":
            command.add_argument("id")
        command.add_argument("--json", required=True, type=document_argument)
    for name in ("get", "delete"):
        commands.add_parser(name).add_argument("id")
    commands.add_parser("list").add_argument("--limit", type=int, default=100)
    commands.add_parser("count")
    find = commands.add_parser("find")
    find.add_argument("field")
    find.add_argument("value")
    return result


def make_db(branch: str) -> GitDb:
    repo, token = os.environ.get("GITDB_REPO"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise SystemExit("set GITDB_REPO=owner/name and GITHUB_TOKEN before running this example")
    return GitDb(repo=repo, token=token, branch=branch)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parser().parse_args(argv)
    collection = make_db(args.branch).collection(args.collection)
    if args.command == "insert":
        print(collection.insert(args.json))
    elif args.command == "get":
        print(json.dumps(collection.get(args.id), indent=2, sort_keys=True))
    elif args.command == "update":
        print(json.dumps(collection.update(args.id, args.json), indent=2, sort_keys=True))
    elif args.command == "delete":
        collection.delete(args.id)
        print(f"deleted {args.id}")
    elif args.command == "list":
        print(json.dumps(collection.list(limit=args.limit), indent=2))
    elif args.command == "count":
        print(collection.count())
    else:
        documents = collection.find(lambda document: str(document.get(args.field)) == args.value)
        print(json.dumps(documents, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
