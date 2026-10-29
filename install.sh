#!/usr/bin/env bash
set -Eeuo pipefail
# Bootstrap only. Inspect this source before piping it to bash.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$ROOT/pyproject.toml" ]]; then
  echo "لم يُعثر على pyproject.toml. شغّل هذا المثبت من مجلد المشروع؛ لا يوجد مستودع منشور معروف بعد." >&2
  exit 2
fi
exec bash "$ROOT/install-colab.sh"
