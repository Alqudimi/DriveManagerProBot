#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_DIR="${TEMP_DIR:-/content/dtm-tmp}"
if [[ ! -d "$TEMP_DIR" ]]; then echo "لا يوجد مجلد مؤقت للتنظيف: $TEMP_DIR"; exit 0; fi
case "$(realpath -m "$TEMP_DIR")" in
  /content/dtm-tmp|"$ROOT"/tmp) ;;
  *) echo "رفض التنظيف لأن المسار ليس مجلدًا مؤقتًا معروفًا للمشروع: $TEMP_DIR" >&2; exit 2;;
esac
find "$TEMP_DIR" -mindepth 1 -maxdepth 1 -type f -name 'dtm-*' -print -delete
echo "تم تنظيف الملفات المؤقتة المعروفة فقط."
