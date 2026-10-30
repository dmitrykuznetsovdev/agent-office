---
name: git-workflow
description: Ветки, сообщения коммитов и PR в Agent Office. Использовать перед созданием ветки, коммитом, пушем и открытием PR.
license: MIT
---

# Git в Agent Office

В `main` напрямую не коммитим. Любое изменение идёт через ветку и PR.

## Старт

```bash
git fetch --prune origin
git status --porcelain
git checkout -b <type>/<kebab-case-description> origin/main
```

Ветвиться от `origin/main`, а не от локального `main`: так ветка не вырастет из устаревшего кода.

## Имена веток

| Тип | Когда | Пример |
|---|---|---|
| `feat/` | Новая фича для пользователя | `feat/office-screenshot` |
| `fix/` | Исправление бага | `fix/office-generation-proxy` |
| `refactor/` | Внутренние изменения без смены поведения | `refactor/split-ledger` |
| `docs/` | Только документация | `docs/readme-and-skills` |
| `test/` | Только тесты | `test/codex-limits` |
| `chore/` | Зависимости, конфиги, CI | `chore/bump-typescript` |
| `perf/` | Оптимизация | `perf/journal-tail` |

Имя описывает суть изменения. Автоматические имена вроде `claude/<id>` не годятся: такую ветку перед пушем переименовать.

## Коммиты: Conventional Commits

Формат: `<type>(<scope>): <описание в повелительном наклонении>`.

- `scope` необязателен: `server`, `floors`, `menubar`, `stats`.
- Описание до 70 символов, без точки в конце.
- Тело через пустую строку объясняет, почему, а не что.

```
feat(floors): add a lift ride for the lounge
fix(server): skip truncated journal lines
docs: describe office manifest fields
```

## Перед PR

1. `npm test` зелёный, `npm run check` чистый.
2. Если менялись `src/*.ts`: `npm run build`, собранные `web/*.js` в коммите.
3. README совпадает с кодом, см. скил `readme-sync`.
4. В диффе нет личных данных, см. скил `public-repo-hygiene`.

## PR и мерж

Заголовок PR тоже в формате Conventional Commits: он станет текстом коммита в `main`. В описании указать ветку. Мерж только через squash:

```bash
gh pr merge --squash --delete-branch
```
