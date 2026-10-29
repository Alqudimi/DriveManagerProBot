#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
if [[ ! -d /content && "${FORCE_COLAB_INSTALL:-0}" != 1 ]]; then
  echo "هذا المثبت معدّ لـ Google Colab. للتجربة المحلية اضبط FORCE_COLAB_INSTALL=1." >&2
  exit 2
fi
command -v curl >/dev/null || { echo "المتطلب مفقود: curl" >&2; exit 3; }
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv lock
uv sync --all-extras
BIN="$ROOT/.local/bin/telegram-bot-api"
SOURCE="$ROOT/.build/telegram-bot-api-source"
# Python's zipfile extraction does not reliably restore Unix executable mode bits.
if [[ -f "$BIN" && ! -x "$BIN" ]]; then chmod 0755 "$BIN"; fi
is_usable_binary() {
  [[ -x "$BIN" ]] && "$BIN" --help >/dev/null 2>&1
}
if is_usable_binary; then
  echo "اكتُشف خادم Telegram Bot API محلي صالح؛ سأعيد استخدامه دون إعادة بناء."
else
  if [[ -e "$BIN" ]]; then
    echo "ملف الخادم المحلي موجود لكنه غير صالح أو لا يعمل؛ سيُبنى بديل رسمي بعد فحص المصدر." >&2
  else
    echo "لا يوجد خادم محلي صالح؛ سيُبنى خادم Telegram Bot API الرسمي من المصدر."
  fi
  for cmd in git cmake c++ make pkg-config; do
    command -v "$cmd" >/dev/null || { echo "المتطلب مفقود للبناء من المصدر: $cmd" >&2; exit 3; }
  done
  mkdir -p "$ROOT/.build" "$ROOT/.local/bin"
  if [[ ! -f "$SOURCE/CMakeLists.txt" || ! -d "$SOURCE/.git" ]]; then
    CLONE_TMP="$ROOT/.build/telegram-bot-api-source.tmp.$$"
    rm -rf "$CLONE_TMP"
    git clone --depth 1 --recurse-submodules https://github.com/tdlib/telegram-bot-api.git "$CLONE_TMP"
    [[ -f "$CLONE_TMP/CMakeLists.txt" ]] || { echo "المستودع الرسمي لا يحتوي CMakeLists.txt" >&2; exit 7; }
    mv "$CLONE_TMP" "$SOURCE"
  fi
  REMOTE="$(git -C "$SOURCE" remote get-url origin 2>/dev/null || true)"
  case "$REMOTE" in
    https://github.com/tdlib/telegram-bot-api.git|git@github.com:tdlib/telegram-bot-api.git) ;;
    *) echo "رُفض مصدر البناء غير الرسمي: ${REMOTE:-لا يوجد remote origin}" >&2; exit 11 ;;
  esac
  cmake -S "$SOURCE" -B "$SOURCE/build" -DCMAKE_BUILD_TYPE=Release
  cmake --build "$SOURCE/build" --target telegram-bot-api -j2
  BUILT="$SOURCE/build/telegram-bot-api"
  [[ -x "$BUILT" ]] || { echo "انتهى CMake دون إنشاء الملف التنفيذي المتوقع: $BUILT" >&2; exit 8; }
  STAGED="$ROOT/.local/bin/telegram-bot-api.tmp.$$"
  install -m 0755 "$BUILT" "$STAGED"
  if ! "$STAGED" --help >/dev/null 2>&1; then
    rm -f "$STAGED"
    echo "فشل فحص --help للملف التنفيذي المبني؛ لم أستبدل الخادم الحالي." >&2
    exit 9
  fi
  mv -f "$STAGED" "$BIN"
fi
is_usable_binary || { echo "خادم Telegram Bot API غير صالح بعد التثبيت." >&2; exit 10; }
mkdir -p "$ROOT/state" "${TEMP_DIR:-/content/dtm-tmp}"
[[ -f .env ]] || cp .env.example .env
printf 'اكتمل تجهيز Python والخادم المحلي. أضف أسرارك إلى .env أو Colab Secrets ثم شغّل run.sh.\n'
