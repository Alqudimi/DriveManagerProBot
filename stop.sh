#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for name in bot bot-api; do
  file="$ROOT/state/$name.pid"
  if [[ -f "$file" ]]; then
    pid="$(cat "$file")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then kill -TERM "$pid"; fi
    rm -f "$file"
  fi
done
echo "أوقفت عمليات المشروع المسجلة فقط."
