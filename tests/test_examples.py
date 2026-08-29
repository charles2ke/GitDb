"""Smoke tests for runnable examples; all GitHub access is replaced locally."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import responses

EXAMPLES = Path(__file__).parents[1] / "examples"


def load_example(name: str) -> ModuleType:
    path = EXAMPLES / name
    module_name = f"example_{name.replace('/', '_').replace('.py', '')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "name",
    [
        "quickstart.py",
        "async_quickstart.py",
        "crud_cli.py",
        "bulk_import.py",
        "indexed_queries.py",
        "concurrency_cas.py",
        "snapshots_history.py",
        "async_client.py",
        "webapp/app.py",
        "server/main.py",
    ],
)
def test_examples_import_without_network(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITDB_REPO", raising=False)
    load_example(name)


@responses.activate
def test_crud_cli_insert_uses_public_collection_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_example("crud_cli.py")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITDB_REPO", "owner/repo")

    def created_document(request: Any) -> tuple[int, dict[str, str], str]:
        payload = json.loads(request.body)
        assert json.loads(base64.b64decode(payload["content"]))["name"] == "Ada"
        return 201, {"Content-Type": "application/json"}, '{"content": {"sha": "blob"}}'

    responses.add_callback(
        responses.PUT,
        re.compile(r"https://api\.github\.com/repos/owner/repo/contents/data/users/.+\.json"),
        callback=created_document,
    )
    monkeypatch.setattr(module, "document_argument", lambda value: {"name": "Ada"})
    module.main(["users", "insert", "--json", "-"])
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]+", capsys.readouterr().out.strip())


def test_bulk_import_dry_run_validates_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_example("bulk_import.py")
    source = tmp_path / "records.jsonl"
    source.write_text('{"name": "Ada"}\n{"name": "Grace"}\n', encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITDB_REPO", "owner/repo")
    monkeypatch.setattr(module, "GitDb", lambda **kwargs: object())
    module.main(["users", str(source), "--dry-run", "--chunk-size", "1"])
    assert "would import 2 records" in capsys.readouterr().out
