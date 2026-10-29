from __future__ import annotations

import csv
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

FIELDS = [
    "record_id",
    "group_id",
    "file_id",
    "file_name",
    "mime_type",
    "size_bytes",
    "drive_path",
    "parent_path",
    "parent_id",
    "web_view_link",
    "description",
    "note",
    "uploaded_at_utc",
    "sha256",
    "status",
]
_LOCK = threading.RLock()


class CsvIndex:
    def __init__(self, path: Path):
        self.path = path

    def rows(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open(newline="", encoding="utf-8-sig") as stream:
            return list(csv.DictReader(stream))

    def append(
        self,
        file_path: Path,
        *,
        group_id: str,
        description: str = "",
        note: str = "",
        sha256: str = "",
        file_id: str = "",
    ) -> dict[str, str]:
        row = {
            "record_id": str(uuid4()),
            "group_id": group_id,
            "file_id": file_id,
            "file_name": file_path.name,
            "mime_type": "application/octet-stream",
            "size_bytes": str(file_path.stat().st_size),
            "drive_path": str(file_path),
            "parent_path": str(file_path.parent),
            "parent_id": "",
            "web_view_link": "",
            "description": description,
            "note": note,
            "uploaded_at_utc": datetime.now(timezone.utc).isoformat(),
            "sha256": sha256,
            "status": "complete",
        }
        with _LOCK:
            rows = self.rows()
            key = (row["drive_path"], row["size_bytes"])
            previous = next(
                (
                    r
                    for r in rows
                    if (r.get("drive_path"), r.get("size_bytes")) == key and r.get("status") == "complete"
                ),
                None,
            )
            if previous:
                return previous
            self._write(rows + [row])
        return row

    def update_path(self, old_path: Path, new_path: Path, *, status: str = "complete") -> int:
        """Update matching descriptive paths; this does not claim to update Drive API metadata."""
        with _LOCK:
            rows = self.rows()
            changed = 0
            for row in rows:
                if row.get("drive_path") == str(old_path) and row.get("status") == "complete":
                    row["drive_path"] = str(new_path)
                    row["file_name"] = new_path.name
                    row["parent_path"] = str(new_path.parent)
                    row["size_bytes"] = (
                        str(new_path.stat().st_size)
                        if new_path.exists() and new_path.is_file()
                        else row.get("size_bytes", "")
                    )
                    row["status"] = status
                    changed += 1
            if changed:
                self._write(rows)
            return changed

    def mark_deleted(self, target: Path) -> int:
        with _LOCK:
            rows = self.rows()
            changed = 0
            for row in rows:
                if row.get("drive_path") == str(target) and row.get("status") == "complete":
                    row["status"] = "deleted"
                    changed += 1
            if changed:
                self._write(rows)
            return changed

    def _write(self, rows: list[dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.exists():
                backup = self.path.with_suffix(self.path.suffix + ".bak")
                if not backup.exists():
                    backup.write_bytes(self.path.read_bytes())
            os.replace(tmp_name, self.path)
        finally:
            Path(tmp_name).unlink(missing_ok=True)
