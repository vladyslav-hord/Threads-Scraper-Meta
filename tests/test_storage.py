import json
from pathlib import Path

import pytest

from threads_parser.storage import atomic_write_json


def test_atomic_write_json_replaces_output_without_leaving_temp_files(tmp_path: Path) -> None:
    destination = tmp_path / "posts.json"

    atomic_write_json(destination, [{"id": "synthetic-post"}])

    assert json.loads(destination.read_text(encoding="utf-8")) == [{"id": "synthetic-post"}]
    assert list(tmp_path.iterdir()) == [destination]
