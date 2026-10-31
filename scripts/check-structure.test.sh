#!/bin/sh
# Самопроверка scripts/check-structure.sh на временных репозиториях.
set -eu

CHECK="$(cd "$(dirname "$0")" && pwd)/check-structure.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
n=0

lines() { i=0; while [ "$i" -lt "$1" ]; do echo x; i=$((i + 1)); done; }

# case <имя> <ожидание: ok|fail> — репозиторий собирает функция setup_<имя>
case_() {
  n=$((n + 1))
  repo="$TMP/$n"
  mkdir -p "$repo/scripts"
  : > "$repo/scripts/lines-allowlist.txt"
  (cd "$repo" && git init -q && "setup_$1" && git add -A)
  if sh "$CHECK" "$repo" >/dev/null 2>&1; then got=ok; else got=fail; fi
  [ "$got" = "$2" ] || { echo "FAIL: $1 → $got, ждали $2"; exit 1; }
  echo "ok: $1"
}

setup_at_limit() { lines 500 > a.ts; }
setup_over_limit() { lines 501 > a.ts; }
setup_md_is_free() { lines 900 > README.md; }
setup_fixture_is_free() { mkdir -p t/fixtures; lines 900 > t/fixtures/x.py; }
setup_allowlisted() { lines 600 > old.py; printf 'old.py 600\n' > scripts/lines-allowlist.txt; }
setup_allowlisted_grew() { lines 601 > old.py; printf 'old.py 600\n' > scripts/lines-allowlist.txt; }
setup_allowlisted_shrank_under() { lines 400 > old.py; printf 'old.py 600\n' > scripts/lines-allowlist.txt; }
setup_allowlisted_gone() { printf 'old.py 600\n' > scripts/lines-allowlist.txt; }
setup_domain_pure() { mkdir -p server/src/domain; echo 'pub fn f(now: i64) -> i64 { now }' > server/src/domain/a.rs; }
setup_domain_fs() { mkdir -p server/src/domain; echo 'use std::fs;' > server/src/domain/a.rs; }
setup_domain_clock() { mkdir -p server/src/domain; echo 'let t = SystemTime::now();' > server/src/domain/a.rs; }
setup_cyrillic_name() { lines 501 > пример.rs; }
setup_no_final_newline() { lines 500 > a.ts; printf y >> a.ts; }
setup_domain_grouped() { mkdir -p server/src/domain; echo 'use std::{fs, io};' > server/src/domain/a.rs; }
setup_domain_comment() { mkdir -p server/src/domain; echo '// tokio здесь нельзя' > server/src/domain/a.rs; }
setup_app_axum() { mkdir -p server/src/app; echo 'use axum::Router;' > server/src/app/a.rs; }

case_ at_limit ok
case_ over_limit fail
case_ md_is_free ok
case_ fixture_is_free ok
case_ allowlisted ok
case_ allowlisted_grew fail
case_ allowlisted_shrank_under fail
case_ allowlisted_gone fail
case_ domain_pure ok
case_ domain_fs fail
case_ domain_clock fail
case_ app_axum fail
case_ cyrillic_name fail
case_ no_final_newline fail
case_ domain_grouped fail
case_ domain_comment ok
