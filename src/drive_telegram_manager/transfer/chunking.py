from __future__ import annotations

import hashlib
import json
from pathlib import Path


def sha256_file(path: Path, block_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def split_file(source: Path, output_dir: Path, chunk_size: int) -> dict:
    if chunk_size <= 0:
        raise ValueError("حجم الجزء يجب أن يكون موجبًا")
    output_dir.mkdir(parents=True, exist_ok=True)
    size = source.stat().st_size
    count = max(1, (size + chunk_size - 1) // chunk_size)
    parts: list[dict] = []
    with source.open("rb") as src:
        for number in range(1, count + 1):
            part = output_dir / f"{source.name}.part{number:04d}-of-{count:04d}"
            remaining = chunk_size
            with part.open("xb") as dst:
                while remaining:
                    data = src.read(min(8 * 1024 * 1024, remaining))
                    if not data:
                        break
                    dst.write(data)
                    remaining -= len(data)
            parts.append({"name": part.name, "size_bytes": part.stat().st_size, "sha256": sha256_file(part)})
    manifest = {
        "original_name": source.name,
        "size_bytes": size,
        "sha256": sha256_file(source),
        "part_count": count,
        "parts": parts,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def join_parts(manifest_path: Path, parts_dir: Path, output: Path) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if output.exists():
        raise FileExistsError(f"لن أستبدل ملفًا موجودًا: {output}")
    temp = output.with_name(output.name + ".partial")
    try:
        with temp.open("wb") as dst:
            for part_info in manifest["parts"]:
                name = part_info["name"]
                if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
                    raise ValueError("اسم الجزء في manifest غير آمن")
                part = parts_dir / name
                if sha256_file(part) != part_info["sha256"]:
                    raise ValueError(f"فشل التحقق من الجزء: {part.name}")
                with part.open("rb") as src:
                    while block := src.read(8 * 1024 * 1024):
                        dst.write(block)
        if temp.stat().st_size != manifest["size_bytes"] or sha256_file(temp) != manifest["sha256"]:
            raise ValueError("فشل التحقق من الملف بعد التجميع")
        temp.replace(output)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return output
