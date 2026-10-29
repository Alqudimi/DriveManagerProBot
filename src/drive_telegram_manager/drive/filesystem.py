from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path


def safe_child(path: str | Path, roots: list[Path]) -> Path:
    raw = Path(path).expanduser()
    for root in roots:
        resolved_root = root.expanduser().resolve(strict=False)
        candidate = (resolved_root / raw if not raw.is_absolute() else raw).resolve(strict=False)
        try:
            candidate.relative_to(resolved_root)
            return candidate
        except ValueError:
            continue
    raise ValueError("المسار خارج مجلدات Drive المسموح بها")


def safe_name(name: str) -> str:
    cleaned = Path(name.replace("\\", "/")).name.strip().replace("\x00", "")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("اسم الملف غير صالح")
    return cleaned[:240]


def unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for number in range(1, 10000):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError("تعذر إيجاد اسم غير متكرر")


class DriveFilesystem:
    def __init__(self, roots: list[Path]):
        self.roots = [p.expanduser().resolve(strict=False) for p in roots]

    def resolve(self, path: str | Path) -> Path:
        return safe_child(path, self.roots)

    def list_dir(self, path: str | Path, page: int = 0, page_size: int = 20) -> list[Path]:
        folder = self.resolve(path)
        if not folder.is_dir():
            raise NotADirectoryError(folder)
        if page < 0 or page_size < 1 or page_size > 100:
            raise ValueError("قيم الصفحة غير صالحة")
        entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        start = page * page_size
        return entries[start : start + page_size]

    def search(self, query: str, start: str | Path, limit: int = 100) -> Iterator[Path]:
        folder = self.resolve(start)
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in dirs + files:
                if query.casefold() in name.casefold():
                    yield Path(root) / name
                    limit -= 1
                    if limit <= 0:
                        return

    def mkdir(self, parent: str | Path, name: str) -> Path:
        target = unique_destination(self.resolve(parent) / safe_name(name))
        target.mkdir()
        return target

    def rename(self, source: str | Path, new_name: str) -> Path:
        src = self.resolve(source)
        dest = src.with_name(safe_name(new_name))
        if dest.exists():
            raise FileExistsError("يوجد عنصر بالاسم الجديد")
        return src.rename(dest)

    def move(self, source: str | Path, destination: str | Path) -> Path:
        src, dst_dir = self.resolve(source), self.resolve(destination)
        if src.is_dir() and (dst_dir == src or src in dst_dir.parents):
            raise ValueError("لا يمكن نقل مجلد إلى نفسه أو أحد مجلداته الفرعية")
        if not dst_dir.is_dir():
            raise NotADirectoryError(dst_dir)
        return shutil.move(str(src), str(unique_destination(dst_dir / src.name)))

    def copy_streaming(
        self,
        source: Path,
        destination: Path,
        chunk_size: int = 8 * 1024 * 1024,
        on_progress: Callable[[int], None] | None = None,
    ) -> int:
        destination = unique_destination(destination)
        count = 0
        with source.open("rb") as src, destination.open("xb") as dst:
            while chunk := src.read(chunk_size):
                dst.write(chunk)
                count += len(chunk)
                if on_progress:
                    on_progress(count)
            dst.flush()
            os.fsync(dst.fileno())
        return count
