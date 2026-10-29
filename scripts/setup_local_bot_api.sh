#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -x "$ROOT/.local/bin/telegram-bot-api" ]] || { echo "شغّل install-colab.sh لبناء خادم Bot API من المصدر الرسمي." >&2; exit 1; }
"$ROOT/.local/bin/telegram-bot-api" --help >/dev/null
echo "ملف Telegram Bot API التنفيذي صالح. شغّله بواسطة run.sh بعد إعداد الأسرار."
