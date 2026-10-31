# Agent Office: правила разработки

Пиксельный офис, в котором видно работу агентов Claude Code и Codex. Сервер на Python читает журналы и отдаёт состояние страницам на TypeScript. Подробности в [README.md](README.md).

Этот файл читают и Codex, и Claude Code: `CLAUDE.md` подключает его целиком.

## Команды

```bash
npm test            # тесты сервера, зависимости не нужны
npm run check       # типы TypeScript
npm run build       # src/*.ts → web/*.js
python3 server.py --port 8768
scripts/demo/shoot.sh   # картинки README на вымышленных данных
```

Собранные `web/*.js` лежат в репозитории: пользователь запускает проект без `npm`. Поменял `src/*.ts`, собери и положи в коммит.

У сервера нет зависимостей, кроме стандартной библиотеки Python 3.9. Новые не добавлять без явного запроса.

## Обязательные правила

1. **Сначала тест.** Для фичи или багфикса сначала падающий тест, потом код. Тесты не подгонять под сломанный код. Скил `tdd`.
2. **В `main` не коммитить.** Ветка `<type>/<kebab-case-description>`, Conventional Commits, PR в наш репозиторий (см. «Репозиторий»). Каждый `feat` и `fix` дописывает строку в `[Unreleased]` файла `CHANGELOG.md`. Скил `git-workflow`.
3. **Репозиторий публичный.** Никаких настоящих доменов, путей, токенов и реальных сессий на скриншотах. Скил `public-repo-hygiene`.
4. **README совпадает с кодом.** Скил `readme-sync`.
5. **«Готово» только после проверки.** Скил `verify-done`.
6. **Без лишнего.** Минимум кода, правки только по задаче, допущения вслух. Скил `karpathy-guidelines`.

Сервер только смотрит: в журналы Claude Code и Codex он ничего не пишет.

## Репозиторий

Проект развивается самостоятельно в `dmitrykuznetsovdev/agent-office` (`origin`). Он вырос из форка `miulyano/agent-office` (`upstream`), но тот теперь чужой проект.

- Ветка от `origin/main`, PR только в `dmitrykuznetsovdev/agent-office` → `main`. Это репозиторий-форк, и `gh` без `--repo` открывает PR в upstream, поэтому `--repo` указывать всегда.
- В `upstream` не открывать PR, не пушить, не комментировать.
- Изменения из `upstream` забирать только по явной просьбе, своей веткой и своим PR.

## Скилы

Скилы лежат в двух местах: `.claude/skills` для Claude Code, `.agents/skills` для Codex. Папки одинаковые. Правишь один, копируй во второй:

```bash
cp -R .claude/skills/. .agents/skills/
diff -r .claude/skills .agents/skills
```
