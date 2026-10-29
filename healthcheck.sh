#!/usr/bin/env bash
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$ROOT"
status=0
python3 --version || status=1
uv --version || status=1
[[ -x .venv/bin/python ]] && echo "بيئة uv: موجودة" || { echo "بيئة uv: مفقودة"; status=1; }
if [[ -f .env ]]; then
  for key in BOT_TOKEN TELEGRAM_API_ID TELEGRAM_API_HASH ADMIN_IDS DRIVE_ROOT TEMP_DIR TELEGRAM_LOCAL_API_URL; do
    printf -v "SAVED_$key" '%s' "${!key-}"
  done
  set -a; source .env; set +a
  for key in BOT_TOKEN TELEGRAM_API_ID TELEGRAM_API_HASH ADMIN_IDS DRIVE_ROOT TEMP_DIR TELEGRAM_LOCAL_API_URL; do
    saved="SAVED_$key"; [[ -n "${!saved}" ]] && printf -v "$key" '%s' "${!saved}"
  done
else
  echo ".env مفقود"; status=1
fi
root="${DRIVE_ROOT:-/content/drive/MyDrive}"
[[ -d "$root" ]] && echo "Drive متاح: $root" || { echo "Drive غير متاح: $root"; status=1; }
api="${TELEGRAM_LOCAL_API_URL:-http://127.0.0.1:8081}"
if [[ -n "${BOT_TOKEN:-}" ]] && curl -fsS "$api/bot${BOT_TOKEN}/getMe" >/dev/null; then echo "Local Bot API: جاهز"; else echo "Local Bot API: غير متاح"; status=1; fi
df -h "${TEMP_DIR:-/content}" 2>/dev/null || true
exit "$status"
