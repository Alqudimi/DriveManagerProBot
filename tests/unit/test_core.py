from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from drive_telegram_manager.config import Settings
from drive_telegram_manager.drive.filesystem import DriveFilesystem, safe_child, safe_name
from drive_telegram_manager.indexing.csv_index import FIELDS, CsvIndex
from drive_telegram_manager.transfer.chunking import join_parts, sha256_file, split_file


def test_path_traversal_rejected(tmp_path: Path):
    root = tmp_path / "drive"
    root.mkdir()
    with pytest.raises(ValueError):
        safe_child("../outside", [root])
    with pytest.raises(ValueError):
        safe_child(tmp_path / "elsewhere", [root])


def test_safe_name_and_conflict(tmp_path: Path):
    assert safe_name("../../hello.txt") == "hello.txt"
    assert safe_name("file.txt") == "file.txt"
    root = tmp_path / "drive"
    root.mkdir()
    (root / "x").write_text("a")
    fs = DriveFilesystem([root])
    created = fs.mkdir(root, "x")
    assert created.name == "x (1)"


def test_split_join_and_checksum(tmp_path: Path):
    source = tmp_path / "generated.bin"
    source.write_bytes(bytes(range(256)) * 1000)
    parts = tmp_path / "parts"
    manifest = split_file(source, parts, 17000)
    assert manifest["part_count"] > 1
    assert json.loads((parts / "manifest.json").read_text())["sha256"] == sha256_file(source)
    out = tmp_path / "joined.bin"
    join_parts(parts / "manifest.json", parts, out)
    assert out.read_bytes() == source.read_bytes()


def test_bad_part_fails_integrity(tmp_path: Path):
    src = tmp_path / "a"
    src.write_bytes(b"abcdef")
    parts = tmp_path / "parts"
    split_file(src, parts, 3)
    first = next(parts.glob("*.part0001*"))
    first.write_bytes(b"bad")
    with pytest.raises(ValueError):
        join_parts(parts / "manifest.json", parts, tmp_path / "out")


def test_csv_append_duplicate_and_batch_fields(tmp_path: Path):
    folder = tmp_path / "drive"
    folder.mkdir()
    f = folder / "report.csv"
    f.write_text("content")
    index = CsvIndex(folder / "_index.csv")
    a = index.append(f, group_id="g-1", description="shared", note="batch note")
    b = index.append(f, group_id="g-1", description="shared", note="batch note")
    rows = index.rows()
    assert len(rows) == 1
    assert a["record_id"] == b["record_id"]
    assert rows[0]["description"] == "shared"
    assert rows[0]["note"] == "batch note"
    with (folder / "_index.csv").open(encoding="utf-8", newline="") as stream:
        assert next(csv.reader(stream)) == FIELDS


def test_settings_requires_secret_and_localhost_only():
    with pytest.raises(Exception):
        Settings(_env_file=None)
    settings = Settings(bot_token="123:token", telegram_api_id=1, telegram_api_hash="hash", _env_file=None)
    assert settings.telegram_local_api_url == "http://127.0.0.1:8081"
    with pytest.raises(Exception):
        Settings(
            bot_token="x",
            telegram_api_id=1,
            telegram_api_hash="x",
            telegram_local_api_url="https://api.telegram.org",
            _env_file=None,
        )


def test_pagination_and_move_descendant_prohibited(tmp_path: Path):
    root = tmp_path / "drive"
    sub = root / "sub"
    sub.mkdir(parents=True)
    fs = DriveFilesystem([root])
    for i in range(35):
        (root / f"f{i:02}.txt").touch()
    assert len(fs.list_dir(root, 0, 20)) == 20
    assert len(fs.list_dir(root, 1, 20)) == 16
    with pytest.raises(ValueError):
        fs.move(sub, sub)
