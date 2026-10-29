#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$ROOT"
[[ -f .env ]] || { echo "أنشئ .env من .env.example وأدخل بيانات Telegram بأمان." >&2; exit 2; }
# Keep Colab-provided secrets/environment overrides instead of replacing them with blank .env values.
for key in BOT_TOKEN TELEGRAM_API_ID TELEGRAM_API_HASH ADMIN_IDS; do
  printf -v "SAVED_$key" '%s' "${!key-}"
done
set -a; source .env; set +a
for key in BOT_TOKEN TELEGRAM_API_ID TELEGRAM_API_HASH ADMIN_IDS; do
  saved="SAVED_$key"; [[ -n "${!saved}" ]] && printf -v "$key" '%s' "${!saved}"
done
: "${BOT_TOKEN:?BOT_TOKEN مطلوب}"; : "${TELEGRAM_API_ID:?TELEGRAM_API_ID مطلوب}"; : "${TELEGRAM_API_HASH:?TELEGRAM_API_HASH مطلوب}"
[[ -d "${DRIVE_ROOT:-/content/drive/MyDrive}" ]] || { echo "جذر Drive غير متاح؛ اربط Google Drive في Colab أولاً." >&2; exit 3; }
mkdir -p state
API="${TELEGRAM_LOCAL_API_URL:-http://127.0.0.1:8081}"
if ! curl --fail --silent --show-error "$API/bot${BOT_TOKEN}/getMe" >/dev/null 2>&1; then
  BIN="$ROOT/.local/bin/telegram-bot-api"
  [[ -x "$BIN" ]] || { echo "خادم Bot API المحلي غير مثبت. شغّل install-colab.sh." >&2; exit 4; }
  mkdir -p "$ROOT/state/bot-api-data"
  nohup "$BIN" --api-id "$TELEGRAM_API_ID" --api-hash "$TELEGRAM_API_HASH" --local --http-ip-address 127.0.0.1 --http-port "${API##*:}" --dir "$ROOT/state/bot-api-data" >>"$ROOT/state/bot-api.log" 2>&1 &
  echo $! > state/bot-api.pid
fi
ready=0
for _ in $(seq 1 30); do
  if curl --fail --silent "$API/bot${BOT_TOKEN}/getMe" | grep -q '"ok":true'; then ready=1; break; fi
  sleep 1
done
[[ $ready == 1 ]] || { echo "فشل فحص API المحلي. راجع state/bot-api.log؛ لا يوجد تحويل تلقائي إلى السحابي." >&2; exit 5; }
if [[ -f state/bot.pid ]] && kill -0 "$(cat state/bot.pid)" 2>/dev/null; then echo "البوت يعمل بالفعل (PID $(cat state/bot.pid))."; exit 0; fi
uv run python main.py >> state/bot.log 2>&1 & echo $! > state/bot.pid
sleep 2
kill -0 "$(cat state/bot.pid)" || { echo "فشل بدء البوت." >&2; exit 6; }
echo "بدأ البوت وخادم API المحلي. السجلات: state/bot.log وstate/bot-api.log"
