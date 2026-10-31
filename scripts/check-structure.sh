#!/bin/sh
# Правила качества: не больше 500 строк в файле и правила слоёв.
# Usage: scripts/check-structure.sh [корень репозитория]
# Старые файлы больше лимита записаны в scripts/lines-allowlist.txt («путь строк»): расти им нельзя,
# а запись убирается, как только файл ушёл под лимит или удалён.
set -eu

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT"
LIMIT=500
ALLOW=scripts/lines-allowlist.txt
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

# строки как у редактора: последняя без перевода строки тоже считается
count() { awk 'END { print NR }' "$1"; }
allowed() { [ -f "$ALLOW" ] && awk -v f="$1" '$1 == f { print $2 }' "$ALLOW"; }

git -c core.quotePath=false ls-files -- '*.rs' '*.ts' '*.tsx' '*.css' '*.swift' '*.sh' '*.mjs' '*.py' '*.html' |
  grep -vE '(^|/)(fixtures|golden)/' |
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    n=$(count "$f")
    max=$(allowed "$f")
    if [ -n "$max" ]; then
      [ "$n" -le "$max" ] || echo "$f: $n строк, в $ALLOW записано $max — старому файлу расти нельзя"
    elif [ "$n" -gt "$LIMIT" ]; then
      echo "$f: $n строк, лимит $LIMIT"
    fi
  done >> "$OUT"

if [ -f "$ALLOW" ]; then
  while read -r f max; do
    [ -n "$f" ] || continue
    if [ ! -f "$f" ]; then
      echo "$ALLOW: $f удалён — убери запись"
    elif [ "$(count "$f")" -le "$LIMIT" ]; then
      echo "$ALLOW: $f ушёл под лимит — убери запись"
    fi
  done < "$ALLOW" >> "$OUT"
fi

# слои сервера: domain без IO, процессов, рантайма и часов; app без HTTP и файлов
forbid() {
  [ -d "$1" ] || return 0
  # комментарии не в счёт; std::{fs, io} ловится так же, как std::fs
  grep -rnE "$2" "$1" | grep -vE '^[^:]+:[0-9]+:[[:space:]]*//' | sed "s|\$| — $3|" >> "$OUT" || true
}
forbid server/src/domain 'std::(fs|process|net)\b|std::\{[^}]*\b(fs|process|net)\b|tokio|axum|SystemTime::now|Instant::now|Local::now|Utc::now' 'domain без IO и часов'
forbid server/src/app 'axum|std::(fs|process)\b|std::\{[^}]*\b(fs|process)\b' 'app работает через порты'

if [ -s "$OUT" ]; then
  cat "$OUT" >&2
  exit 1
fi
