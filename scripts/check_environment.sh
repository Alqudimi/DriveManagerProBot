#!/usr/bin/env bash
set -Eeuo pipefail
command -v python3 >/dev/null || { echo "Python 3 غير موجود" >&2; exit 1; }
command -v uv >/dev/null || { echo "uv غير موجود" >&2; exit 1; }
python3 --version
uv --version
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$ROOT/pyproject.toml" && -f "$ROOT/uv.lock" ]] || { echo "ملفات المشروع أو القفل مفقودة" >&2; exit 1; }
if [[ -n "${DRIVE_ROOT:-}" && ! -d "$DRIVE_ROOT" ]]; then echo "تحذير: DRIVE_ROOT غير متاح بعد: $DRIVE_ROOT" >&2; fi
echo "بيئة المشروع الأساسية سليمة."
