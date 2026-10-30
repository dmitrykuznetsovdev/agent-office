#!/usr/bin/env python3
"""Agent Office: пиксельный офис, где видно, как работают агенты Claude Code.

Читает журналы всех сессий (~/.claude/projects) и отдаёт состояние офиса
выбранной сессии. В журналы ничего не пишет, хуки не нужны.

  python3 server.py [--port 8767] [--data ~/.claude/agent-office] [--watch-parent]

  /                         страница офиса
  /api/sessions             сессии за последние сутки; today — потрачено сегодня
  /api/stats                траты по дням, моделям и папкам за всё время, почасово — за 5 недель
  /api/state?s=<id>         офис одной сессии (руководитель, агенты, журнал)
  POST /api/office?s=<id>   сгенерировать офис для папки сессии
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, 'web')
CLAUDE_DIR = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude')
PROJECTS = os.path.join(CLAUDE_DIR, 'projects')
DATA = os.path.join(CLAUDE_DIR, 'agent-office')  # офисы папок; меняется флагом --data
RECENT_S = 24 * 3600     # сессии в списке
LIVE_S = 10 * 60         # тишина дольше — сессия считается остывшей
AGENT_IDLE_S = 10 * 60   # агент без finished и без записей дольше — ушёл
LOG_MAX = 80
# $ за 1M токенов: вход, выход, чтение кэша. Цены меняются — правьте здесь.
PRICES = {
    'claude-fable-5-1': (10.0, 50.0, 0.25),
    'claude-opus-5-5': (4.0, 20.0, 0.20),
    'claude-sonnet-5-5': (2.0, 10.0, 0.20),
    'claude-haiku-5-5': (0.10, 0.50, 0.01),
}
LOCK = threading.Lock()  # журналы дочитываются по одному


def ms(ts):
    """ISO-время журнала → миллисекунды."""
    if not ts:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() * 1000)
    except ValueError:
        return 0


TOOL_KINDS = {'Read': 'read', 'Write': 'write', 'Edit': 'edit', 'MultiEdit': 'edit', 'NotebookEdit': 'edit',
              'Grep': 'grep', 'Glob': 'grep', 'Bash': 'bash', 'WebSearch': 'web', 'WebFetch': 'web',
              'Agent': 'agent', 'Task': 'agent', 'Skill': 'skill'}


def tool_kind(tool):
    """Вид инструмента для значка в офисе: читает, правит, ищет, терминал…"""
    return TOOL_KINDS.get(tool, 'other')


def base(path):
    return os.path.basename(str(path or ''))


def describe(tool, inp):
    """Вызов инструмента — парой слов, как его видно через офис."""
    inp = inp if isinstance(inp, dict) else {}
    if tool == 'Read':
        return 'читает ' + base(inp.get('file_path'))
    if tool == 'Write':
        return 'пишет ' + base(inp.get('file_path'))
    if tool in ('Edit', 'NotebookEdit', 'MultiEdit'):
        return 'правит ' + base(inp.get('file_path'))
    if tool in ('Grep', 'Glob'):
        return 'ищет ' + str(inp.get('pattern', ''))
    if tool in ('WebSearch', 'WebFetch'):
        return 'гуглит'
    if tool == 'Skill':
        return 'скил ' + str(inp.get('skill', ''))
    if tool in ('Agent', 'Task'):
        return 'зовёт: ' + str(inp.get('subagent_type') or 'general-purpose')
    if tool == 'Bash':
        desc = str(inp.get('description') or '').strip()
        if desc:
            return desc
        cmd = re.sub(r'^(cd \S+\s*(&&|;)\s*)+', '', str(inp.get('command', '')).strip())
        words = cmd.split()
        if words and words[0] in ('until', 'sleep'):
            return 'ждёт'
        if words and words[0] in ('for', 'while') and words[0] != cmd:
            return 'перебирает в цикле'
        if words and words[0] in ('python3', 'python', 'node') and (len(words) == 1 or words[1].startswith('-')):
            return 'считает скриптом'
        if words and words[0] in ('python3', 'python', 'node', 'bash', 'sh') and len(words) > 1:
            return '$ ' + ' '.join([base(words[1])] + words[2:3])
        return '$ ' + ' '.join(words[:2])
    if tool == 'ToolSearch':
        return 'ищет инструмент'
    if tool == 'SendMessage':
        return 'пишет агенту'
    if tool == 'SubagentHandback':
        return 'докладывает руководителю'
    if tool.startswith('mcp__ccd_turn'):
        return 'отмечает план'
    if tool.startswith('mcp__ccd_'):
        return 'двигает окна'
    if tool.startswith('mcp__Claude_Browser'):
        return 'смотрит в браузер'
    if tool.startswith('mcp__claude-in-chrome'):
        return 'работает в Chrome'
    if 'notion' in tool:
        return 'листает Notion'
    if tool.startswith('mcp__life-transcriber'):
        return 'транскрибирует'
    if tool.startswith('mcp__epidemic'):
        return 'ищет музыку'
    if tool.startswith('mcp__'):
        return ' '.join(tool.split('__')[1:])
    return tool


def cache_writes(usage):
    """Записи в кэш: (на 5 минут, на час)."""
    cc = usage.get('cache_creation') or {}
    if not cc:
        return usage.get('cache_creation_input_tokens', 0), 0
    return cc.get('ephemeral_5m_input_tokens', 0), cc.get('ephemeral_1h_input_tokens', 0)


def cost_parts(model, usage):
    """Стоимость ответа по статьям: вход, выход, чтение кэша, запись в кэш."""
    if model not in PRICES:
        return [0.0, 0.0, 0.0, 0.0]
    p_in, p_out, p_read = PRICES[model]
    w5m, w1h = cache_writes(usage)
    k = (2 if usage.get('speed') == 'fast' else 1) / 1e6
    return [usage.get('input_tokens', 0) * p_in * k, usage.get('output_tokens', 0) * p_out * k,
            usage.get('cache_read_input_tokens', 0) * p_read * k, (w5m * 1.25 + w1h * 2) * p_in * k]


def message_cost(model, usage):
    """Стоимость одного ответа модели, формула reel.py cost."""
    return sum(cost_parts(model, usage))


def token_parts(usage):
    """Токены ответа по статьям, в том же порядке, что cost_parts."""
    return [usage.get('input_tokens', 0), usage.get('output_tokens', 0),
            usage.get('cache_read_input_tokens', 0), sum(cache_writes(usage))]


def tokens_of(usage):
    return sum(token_parts(usage))


def day_of(at_ms):
    """Локальная календарная дата записи журнала."""
    return time.strftime('%Y-%m-%d', time.localtime(at_ms / 1000))


def today_cost(t):
    return t.daily.get(day_of(time.time() * 1000), 0.0)


def parent_alive(ppid):
    return os.getppid() == ppid


def clip(text, n=60):
    text = ' '.join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + '…'


def home_of(cwd):
    """Папка проекта: рабочая копия worktree считается своим репозиторием."""
    if not cwd:
        return ''
    return re.split(r'/\.claude/worktrees/', cwd)[0]


class Tail:
    """Журнал, дочитываемый с места остановки: вызовы, последнее действие, хвост."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.rest = b''
        self.calls = 0
        self.first = 0
        self.last = 0          # время последней записи
        self.activity = ''
        self.activity_at = 0
        self.tool = ''         # вид последнего инструмента, см. tool_kind
        self.model = ''        # модель последнего ответа
        self.busy = False      # последний ход не закончен
        self.title = ''
        self.prompt = ''
        self.cwd = ''
        self.home = ''         # папка, где сессию открыли
        self.roster = {}       # агенты, доступные сессии: тип → описание
        self.events = []       # [(at, text)]
        self.cost = 0.0
        self.daily = {}        # локальная дата → $, последние два дня
        self.paid = set()      # ответ модели пишется в журнал кусками — платим за id один раз

    def feed(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return self
        if size < self.offset:
            self.__init__(self.path)
        if size == self.offset:
            return self
        with open(self.path, 'rb') as f:
            f.seek(self.offset)
            chunk = self.rest + f.read(size - self.offset)
        self.offset = size
        lines = chunk.split(b'\n')
        self.rest = lines.pop()
        for raw in lines:
            try:
                self.take(json.loads(raw))
            except (ValueError, AttributeError):
                continue  # битая строка
        return self

    def listing(self, a):
        """Список агентов приходит в журнал дельтами: добавлены, убраны."""
        if a.get('type') != 'agent_listing_delta':
            return
        for t in a.get('removedTypes') or []:
            self.roster.pop(t, None)
        descs = {}
        for line in a.get('addedLines') or []:
            m = re.match(r'- ([^:]+(?::[^:\s]+)?): (.*)', line)
            if m:
                descs[m.group(1)] = m.group(2)
        for t in a.get('addedTypes') or []:
            self.roster[t] = descs.get(t, '')

    def take(self, d):
        kind = d.get('type')
        if kind == 'custom-title':
            self.title = d.get('customTitle') or self.title
            return
        if kind == 'last-prompt':
            self.prompt = d.get('lastPrompt') or self.prompt
            return
        if kind == 'attachment':
            self.listing(d.get('attachment') or {})
            return
        if kind not in ('user', 'assistant'):
            return
        at = ms(d.get('timestamp'))
        if at:
            self.first = self.first or at
            self.last = max(self.last, at)
        self.cwd = d.get('cwd') or self.cwd
        self.home = self.home or home_of(self.cwd)
        msg = d.get('message') or {}
        content = msg.get('content') if isinstance(msg, dict) else None
        if kind == 'user':
            # новый запрос или результат инструмента — ход продолжается
            self.busy = True
            if isinstance(content, str) and not d.get('isMeta'):
                self.activity, self.activity_at = 'думает', at
            return
        mid = msg.get('id')
        self.model = msg.get('model') or self.model
        if msg.get('usage') and mid not in self.paid:
            self.paid.add(mid)
            usd = message_cost(msg.get('model'), msg['usage'])
            self.cost += usd
            day = day_of(at)
            self.daily[day] = self.daily.get(day, 0.0) + usd
            for old in sorted(self.daily)[:-2]:
                del self.daily[old]
        uses = [b for b in content or [] if isinstance(b, dict) and b.get('type') == 'tool_use']
        for b in uses:
            self.calls += 1
            what = describe(b.get('name', ''), b.get('input'))
            self.activity, self.activity_at = what, at
            self.tool = tool_kind(b.get('name', ''))
            self.events.append((at, what))
        if len(self.events) > LOG_MAX:
            self.events = self.events[-LOG_MAX:]
        self.busy = msg.get('stop_reason') != 'end_turn' or bool(uses)

# ── Офисы: раскладка агентов папки по столам ───────────────────────────────
# универсальные агенты сидят во фрилансе, своих столов у них нет
BUILTIN = {'claude', 'general-purpose', 'Explore', 'Plan', 'claude-code-guide', 'statusline-setup'}
PROPS = ['crown', 'headphones', 'camera', 'scissors', 'beret', 'beretRed', 'books', 'chart', 'mic',
         'binoculars', 'script', 'box', 'download', 'magnifier', 'layers', 'frame', 'glasses',
         'tablet', 'clipboard', 'paper', 'cap']
SHIRTS = ['#2f4f8f', '#c0583a', '#5a7a2e', '#8f2f3a', '#4b5a8a', '#3a7a4a', '#7a3fb0', '#1f6f8f',
          '#b08a2e', '#2f7a5a', '#c04a6a', '#8a6a3a', '#a0602a', '#2f6f6a', '#6b2d5c', '#5a5f6a']
HAIRS = ['#2b1d14', '#1a1a1a', '#6b4a2b', '#c9a227', '#9a9a9a', '#3a2416', '#8a5a2b', '#e05a8a']
GEN_TIMEOUT = 120  # с: большой офис собирается за 15–30 с; дольше — claude чего-то ждёт
JOBS = {}  # папка → последняя генерация: status running|done|failed|cancelled, started, ended, error, proc


def hash_of(text):
    h = 0
    for c in text:
        h = (h * 31 + ord(c)) & 0x7fffffff
    return h


def office_path(folder):
    return os.path.join(DATA, 'offices', re.sub(r'[^A-Za-z0-9]+', '-', folder).strip('-') + '.json')


def dress(desk):
    """Цвета, которых нет в описании стола, — по типу агента, стабильно."""
    h = hash_of(desk['type'])
    desk.setdefault('shirt', SHIRTS[h % len(SHIRTS)])
    desk.setdefault('hair', HAIRS[(h // 7) % len(HAIRS)])
    desk.setdefault('skin', (h // 13) % 5)
    if desk.get('prop') not in PROPS:
        desk['prop'] = PROPS[(h // 3) % len(PROPS)]
    return desk


def humanize(t):
    key = t.split(':')[-1].replace('-', ' ').replace('_', ' ')
    return clip(key[:1].upper() + key[1:], 13)


# Должности по-русски без модели: сначала словосочетание целиком, потом главное слово (последнее).
PHRASES = {
    'art-director': 'Арт-директор', 'motion-designer': 'Аниматор', 'sound-designer': 'Звукорежиссёр',
    'cover-maker': 'Обложечник', 'subs-editor': 'Субтитровщик', 'qa-inspector': 'Контролёр', 'qa': 'Контролёр',
    'style-critic': 'Стилист', 'selection-critic': 'Отборщик', 'code-reviewer': 'Ревьюер', 'statusline-setup': 'Наладчик',
}
WORDS = {
    'reviewer': 'Ревьюер', 'review': 'Ревьюер', 'builder': 'Сборщик', 'investigator': 'Сыщик', 'rescue': 'Спасатель',
    'analyst': 'Аналитик', 'director': 'Директор', 'camera': 'Оператор', 'copywriter': 'Копирайтер', 'maker': 'Мастер',
    'editor': 'Редактор', 'interviewer': 'Интервьюер', 'inspector': 'Инспектор', 'librarian': 'Библиотекарь',
    'loader': 'Загрузчик', 'designer': 'Дизайнер', 'packager': 'Упаковщик', 'scout': 'Скаут', 'scriptwriter': 'Сценарист',
    'critic': 'Критик', 'writer': 'Автор', 'planner': 'Планировщик', 'plan': 'Архитектор', 'architect': 'Архитектор',
    'tester': 'Тестировщик', 'researcher': 'Исследователь', 'explorer': 'Разведчик', 'explore': 'Разведчик',
    'coder': 'Программист', 'developer': 'Разработчик', 'auditor': 'Аудитор', 'fixer': 'Наладчик', 'helper': 'Помощник',
    'assistant': 'Помощник', 'manager': 'Менеджер', 'translator': 'Переводчик', 'summarizer': 'Конспектор',
    'debugger': 'Отладчик', 'documenter': 'Документатор', 'guide': 'Гид', 'deployer': 'Выкладчик', 'optimizer': 'Оптимизатор',
    'refactorer': 'Рефакторщик', 'migrator': 'Мигратор', 'monitor': 'Наблюдатель', 'operator': 'Оператор',
}
# Отделы по ходу работы: найти → придумать → сделать → проверить → выручить
KINDS = [
    ('РАЗВЕДКА', ['investigat', 'scout', 'explor', 'research', 'search', 'analyst', 'librarian', 'finder', 'locator', 'monitor']),
    ('ПЛАН', ['plan', 'architect', 'script', 'copywrit', 'writer', 'interview', 'strateg', 'product']),
    ('ПРОИЗВОДСТВО', ['build', 'editor', 'maker', 'design', 'director', 'camera', 'motion', 'sound', 'loader', 'packag',
                      'render', 'cover', 'subs', 'coder', 'develop', 'refactor', 'migrat', 'translat', 'document', 'operator']),
    ('КАЧЕСТВО', ['review', 'critic', 'inspector', 'qa', 'test', 'audit', 'verif', 'lint', 'check']),
    ('ПОДДЕРЖКА', ['rescue', 'debug', 'fix', 'deploy', 'setup', 'support', 'ops', 'help', 'guide']),
]
LATIN = re.compile('[A-Za-z]')


def ru_name(t):
    key = t.split(':')[-1].lower()
    if key in PHRASES:
        return PHRASES[key]
    head = re.split(r'[-_ ]+', key)[-1]
    return WORDS.get(head) or humanize(t)


def kind_of(t, desc=''):
    key = t.split(':')[-1].lower()
    for text in (key, desc.lower()):
        for zone, words in KINDS:
            if any(w in text for w in words):
                return zone
    return 'ПРОЧИЕ'


def auto_office(folder, roster):
    """Офис без модели: отделы по ходу работы, должности по-русски из названий агентов."""
    order = [k for k, _ in KINDS] + ['ПРОЧИЕ']
    zones = {}
    for t, desc in roster.items():
        if t in BUILTIN:
            continue
        zones.setdefault(kind_of(t, desc or ''), []).append(dress({'type': t, 'name': ru_name(t)}))
    boss = dress({'type': 'main', 'name': 'Главный', 'prop': 'crown', 'shirt': '#3b3f8f'})
    rows = [{'name': 'ГЛАВНЫЙ', 'desks': [boss]}] if not zones else []
    for i, name in enumerate(n for n in order if n in zones):
        rows.append({'name': name, 'desks': ([boss] if i == 0 else []) + zones[name]})
    return {'folder': folder, 'title': 'ОФИС · ' + os.path.basename(folder).upper(), 'zones': rows, 'guests': {}}


def load_office(folder):
    try:
        with open(office_path(folder)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


SCHEMA = {
    'type': 'object',
    'properties': {
        'title': {'type': 'string'},
        'boss': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'prop': {'type': 'string', 'enum': PROPS}},
                 'required': ['name', 'prop']},
        'zones': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'name': {'type': 'string'},
            'desks': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'type': {'type': 'string'}, 'name': {'type': 'string'}, 'prop': {'type': 'string', 'enum': PROPS}},
                'required': ['type', 'name', 'prop']}}},
            'required': ['name', 'desks']}},
        'guests': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'type': {'type': 'string'}, 'name': {'type': 'string'}, 'prop': {'type': 'string', 'enum': PROPS}},
            'required': ['type', 'name', 'prop']}},
    },
    'required': ['title', 'boss', 'zones', 'guests'],
}

PROMPT = """Сделай пиксельный офис для команды ИИ-агентов проекта «{name}» ({folder}).
У главной сессии (руководителя) свой кабинет, остальные агенты — сотрудники по этажам-отделам.

Агенты (тип — описание):
{agents}

Правила:
- Всё по-русски, без латиницы: вывеска, отделы, должности (вместо QA — «Контролёр»).
- title: вывеска офиса заглавными, 1–3 слова, по сути проекта.
- boss: должность руководителя («Продюсер», «Тимлид», «Главред»), предмет из списка.
- zones: 2–5 отделов по ходу работы — от поиска и подготовки к производству, проверке и выпуску; порядок отделов — этот же.
  Название отдела — одно-два слова заглавными («РАЗВЕДКА», «МОНТАЖ», «КАЧЕСТВО»). В отделе 1–8 столов; не дроби отделы ради числа.
- Каждый агент, кроме универсальных ({builtin}), — ровно за одним столом в zones; тип копируй дословно. В guests их не клади.
- guests — только универсальные агенты из списка выше, если они есть.
- name: должность по-русски, до 13 знаков, понятная без описания («Монтажёр», «Сценарист», «Ревьюер», «Сыщик»), не имя человека.
- prop: предмет на столе, подходящий к работе, из списка в схеме."""


def find_claude():
    for p in [shutil.which('claude'), os.path.expanduser('~/.local/bin/claude'), '/opt/homebrew/bin/claude', '/usr/local/bin/claude']:
        if p and os.access(p, os.X_OK):
            return p
    for p in sorted(glob.glob(os.path.expanduser('~/.nvm/versions/node/*/bin/claude')), reverse=True):
        return p
    return None


class GenError(Exception):
    pass


def ask_claude(folder, roster, job):
    """Раскладка по смыслу — одним вызовом claude -p. Процесс лежит в job: его можно убить."""
    exe = find_claude()
    if not exe:
        raise GenError('не нашёл claude: нужен установленный Claude Code')
    agents = '\n'.join('- %s — %s' % (t, clip(d, 220)) for t, d in sorted(roster.items()))
    prompt = PROMPT.format(name=os.path.basename(folder), folder=folder, agents=agents or '(только универсальные)',
                           builtin=', '.join(sorted(BUILTIN)))
    try:
        proc = subprocess.Popen(
            [exe, '-p', '--model', 'sonnet', '--tools', '', '--no-session-persistence', '--setting-sources', '',
             '--output-format', 'json', '--json-schema', json.dumps(SCHEMA),
             '--system-prompt', 'Ты раскладываешь агентов по пиксельному офису. Отвечай только JSON по схеме.'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=DATA if os.path.isdir(DATA) else HERE,
            env=dict(os.environ, PATH=os.path.dirname(exe) + ':' + os.environ.get('PATH', '/usr/bin:/bin')))
    except OSError as e:
        raise GenError('claude не запустился: %s' % e)
    job['proc'] = proc
    try:
        out, err = proc.communicate(prompt, timeout=GEN_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise GenError('claude не ответил за %d с. Обычно он ждёт входа: запустите claude в терминале и войдите '
                       '(/login), а если macOS спросит доступ к связке ключей — разрешите' % GEN_TIMEOUT)
    if job.get('cancel'):
        return None
    try:
        answer = json.loads(out)
    except ValueError:
        raise GenError('claude ответил не JSON: %s' % clip(err or out or 'пусто', 160))
    if answer.get('is_error'):
        raise GenError('claude: %s' % clip(answer.get('result') or 'ошибка без описания', 160))
    office = answer.get('structured_output')
    if not office or not office.get('zones'):
        raise GenError('claude вернул офис без отделов')
    return office


def backup_path(folder):
    return office_path(folder)[:-5] + '.prev.json'


def run_office(folder, roster, job):
    """Фоновая генерация: при успехе прежний офис уходит в .prev.json, при неудаче остаётся как был."""
    try:
        office = ask_claude(folder, roster, job)
        if office is not None:
            result = tidy(folder, roster, office)
            path = office_path(folder)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path):
                os.replace(path, backup_path(folder))
            with open(path, 'w') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        job['status'] = 'cancelled' if job.get('cancel') else 'done'
    except GenError as e:
        job['status'], job['error'] = 'failed', str(e)
    except Exception as e:  # генерация в потоке: ошибка не должна оставить «running» навсегда
        job['status'], job['error'] = 'failed', 'сбой: %s' % e
    finally:
        sys.stderr.write('генерация офиса %s: %s %s\n' % (folder, job['status'], job.get('error', '')))
        job['ended'] = time.time()


def start_office(folder, roster):
    """Запустить генерацию; None — уже идёт."""
    job = JOBS.get(folder)
    if job and job['status'] == 'running':
        return None
    job = JOBS[folder] = {'status': 'running', 'started': time.time(), 'ended': 0, 'error': ''}
    threading.Thread(target=run_office, args=(folder, roster, job), daemon=True).start()
    return job


def cancel_office(folder):
    job = JOBS.get(folder)
    if not job or job['status'] != 'running':
        return False
    job['cancel'] = True
    proc = job.get('proc')
    if proc and proc.poll() is None:
        proc.kill()
    return True


def undo_office(folder):
    """Вернуть офис, который был до последней генерации."""
    prev = backup_path(folder)
    if not os.path.exists(prev):
        return False
    os.replace(prev, office_path(folder))
    job = JOBS.get(folder)
    if job and job['status'] == 'done':
        job['status'] = 'undone'
    return True


def job_view(folder):
    job = JOBS.get(folder) or {}
    return {'status': job.get('status', ''), 'started': int(job.get('started', 0) * 1000),
            'ended': int(job.get('ended', 0) * 1000), 'error': job.get('error', ''),
            'canUndo': os.path.exists(backup_path(folder)), 'timeout': GEN_TIMEOUT}


def tidy(folder, roster, raw):
    """Ответ модели — в файл офиса: чужие типы выкинуть, забытых посадить, руководителя — первым.
    Должности и отделы — только по-русски: латиницу заменяем словарём."""
    def russian(name):
        name = ' '.join(str(name or '').split())
        return name if name and not LATIN.search(name) else ''

    # русское имя, которое модель дала агенту, где бы его ни посадила
    given = {}
    for d in [d for z in raw['zones'] for d in z.get('desks') or []] + list(raw.get('guests') or []):
        given.setdefault(d.get('type'), russian(d.get('name')))
    title = lambda t: clip(given.get(t) or ru_name(t), 13)
    seen, zones = set(), []
    for z in raw['zones']:
        desks = []
        for d in z.get('desks') or []:
            t = d.get('type')
            if t in roster and t not in BUILTIN and t not in seen:
                seen.add(t)
                desks.append(dress({'type': t, 'name': title(t), 'prop': d.get('prop')}))
        if desks:
            kinds = [kind_of(d['type'], roster.get(d['type']) or '') for d in desks]
            name = russian(z.get('name')) or max(set(kinds), key=kinds.count)
            zones.append({'name': clip(name, 24).upper(), 'desks': desks})
    rest = [dress({'type': t, 'name': title(t)}) for t in roster if t not in BUILTIN and t not in seen]
    if rest:
        zones.append({'name': 'ПРОЧИЕ', 'desks': rest})
    boss = dress({'type': 'main', 'name': clip(russian(raw['boss'].get('name')) or 'Главный', 13), 'prop': raw['boss'].get('prop'),
                  'shirt': '#3b3f8f'})
    if not zones:
        zones = [{'name': 'ГЛАВНЫЙ', 'desks': []}]
    first = zones[0]['desks']
    # руководитель — в середине полного ряда, иначе первым
    first.insert(2 if len(first) == 4 else 0, boss)
    guests = {g['type']: {'name': title(g['type']), 'prop': g.get('prop') if g.get('prop') in PROPS else 'cap'}
              for g in raw.get('guests') or [] if g.get('type') in roster and g.get('type') in BUILTIN}
    return {'folder': folder, 'title': clip(russian(raw.get('title')) or os.path.basename(folder), 28).upper(),
            'zones': zones, 'guests': guests, 'generated': 'claude'}


# ── Статистика: траты по часам из всех журналов ─────────────────────────────
HEAT_DAYS = 35  # сколько дней отдавать почасово, для карты «когда тратим»


def blank_day(day):
    return {'day': day, 'usd': 0.0, 'models': {}, 'projects': {}, 'parts': [0.0, 0.0, 0.0, 0.0], 'tokens': 0,
            'tokParts': [0, 0, 0, 0], 'tokModels': {}, 'tokProjects': {}, 'sessions': 0, 'started': 0}


class Ledger:
    """Сводка трат по часам, моделям и папкам. Лежит на диске: Claude Code
    чистит журналы через месяц, а история трат остаётся."""
    IDS = 64  # ответ модели пишется кусками подряд — помним последние id файла
    VERSION = 2  # 2: токены по статьям в ячейке

    def __init__(self, path):
        self.path = path
        self.files = {}  # журнал → {offset, ids, days, main}
        # 'ГГГГ-ММ-ДДTЧЧ' → {папка: {модель: [$ вход, выход, чтение кэша, запись в кэш, токены, те же статьи в токенах]}}
        self.hours = {}
        self.dirty = False
        self.saved_at = 0
        try:
            with open(path) as f:
                data = json.load(f)
            files, hours = data['files'], data['hours']
        except (OSError, ValueError, KeyError, TypeError):
            return
        # старая сводка без токенов по статьям: пока журналы на месте, читаем их заново,
        # иначе оставляем как есть — у той истории будет только общее число токенов
        if data.get('v') != self.VERSION and all(os.path.exists(f) for f in files):
            self.dirty = True
            return
        for projects in hours.values():
            for models in projects.values():
                for cell in models.values():
                    cell.extend([0] * (9 - len(cell)))
        self.files, self.hours = files, hours

    def scan(self, root=None):
        root = root or PROJECTS
        for proj in os.listdir(root):
            pdir = os.path.join(root, proj)
            if not os.path.isdir(pdir):
                continue
            for name in os.listdir(pdir):
                if name.endswith('.jsonl'):
                    self.feed(os.path.join(pdir, name), True)
                    continue
                sub = os.path.join(pdir, name, 'subagents')
                if os.path.isdir(sub):
                    for agent in os.listdir(sub):
                        if agent.endswith('.jsonl'):
                            self.feed(os.path.join(sub, agent), False)
        return self

    def feed(self, path, main):
        rec = self.files.setdefault(path, {'offset': 0, 'ids': [], 'days': [], 'main': main})
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < rec['offset']:
            rec['offset'] = 0  # журнал переписан заново
        if size == rec['offset']:
            return
        with open(path, 'rb') as f:
            f.seek(rec['offset'])
            data = f.read(size - rec['offset'])
        cut = data.rfind(b'\n')
        if cut < 0:
            return  # строка ещё дописывается
        rec['offset'] += cut + 1
        for raw in data[:cut].split(b'\n'):
            if b'"usage"' in raw:  # дешёвый отсев: json разбираем только у ответов модели
                self.take(rec, raw)
        self.dirty = True

    def take(self, rec, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        msg = d.get('message') if d.get('type') == 'assistant' else None
        usage = msg.get('usage') if isinstance(msg, dict) else None
        at = ms(d.get('timestamp'))
        if not usage or not at:
            return
        mid = msg.get('id')
        if mid:
            if mid in rec['ids']:
                return
            rec['ids'].append(mid)
            del rec['ids'][:-self.IDS]
        model = msg.get('model') or '?'
        parts, toks = cost_parts(model, usage), token_parts(usage)
        tokens = sum(toks)
        if not tokens:
            return
        hour = time.strftime('%Y-%m-%dT%H', time.localtime(at / 1000))
        project = os.path.basename(home_of(d.get('cwd'))) or '?'
        cell = self.hours.setdefault(hour, {}).setdefault(project, {}).setdefault(model, [0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0, 0])
        for i, n in enumerate(parts + [tokens] + toks):
            cell[i] += n
        if rec['main'] and hour[:10] not in rec['days']:
            rec['days'].append(hour[:10])

    def save(self, every=0):
        if not self.dirty or time.time() - self.saved_at < every:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'v': self.VERSION, 'files': self.files, 'hours': self.hours}, f, separators=(',', ':'))
        os.replace(tmp, self.path)
        self.dirty, self.saved_at = False, time.time()

    def report(self, now=None):
        """Дни подряд от первой записи до сегодня; почасово — последние HEAT_DAYS дней."""
        from datetime import date, timedelta
        now = now or time.time()
        since_24 = time.strftime('%Y-%m-%dT%H', time.localtime(now - 23 * 3600))
        heat_from = day_of((now - HEAT_DAYS * 86400) * 1000)
        days, hours, last24 = {}, [], 0.0
        for hour, projects in sorted(self.hours.items()):
            row = days.setdefault(hour[:10], blank_day(hour[:10]))
            spent = 0.0
            for project, models in projects.items():
                for model, cell in models.items():
                    usd = sum(cell[:4])
                    spent += usd
                    row['models'][model] = row['models'].get(model, 0.0) + usd
                    row['projects'][project] = row['projects'].get(project, 0.0) + usd
                    row['parts'] = [a + b for a, b in zip(row['parts'], cell[:4])]
                    row['tokens'] += cell[4]
                    row['tokParts'] = [a + b for a, b in zip(row['tokParts'], cell[5:9])]
                    row['tokModels'][model] = row['tokModels'].get(model, 0) + cell[4]
                    row['tokProjects'][project] = row['tokProjects'].get(project, 0) + cell[4]
            row['usd'] += spent
            if hour >= since_24:
                last24 += spent
            if hour[:10] >= heat_from and spent:
                hours.append([hour[:10], int(hour[11:]), round(spent, 4)])
        for rec in self.files.values():
            for day in rec['days'] if rec['main'] else []:
                days.setdefault(day, blank_day(day))['sessions'] += 1
            if rec['main'] and rec['days']:
                days[min(rec['days'])]['started'] += 1
        out = []
        if days:
            d, end = date.fromisoformat(min(days)), date.fromisoformat(day_of(now * 1000))
            while d <= end:
                out.append(days.get(d.isoformat()) or blank_day(d.isoformat()))
                d += timedelta(days=1)
        for row in out:
            row['usd'] = round(row['usd'], 4)
            row['parts'] = [round(x, 4) for x in row['parts']]
            for k in ('models', 'projects'):
                row[k] = {name: round(usd, 4) for name, usd in row[k].items()}
        return {'since': out[0]['day'] if out else '', 'last24': round(last24, 4), 'days': out, 'hours': hours}


LEDGER = None
STATS_LOCK = threading.Lock()  # статистика копится отдельно от офиса: первый проход долгий


def stats():
    global LEDGER
    with STATS_LOCK:
        if LEDGER is None:
            LEDGER = Ledger(os.path.join(DATA, 'stats.json'))
        LEDGER.scan()
        LEDGER.save(every=60)
        return LEDGER.report()


TAILS = {}


def tail(path):
    t = TAILS.get(path)
    if t is None:
        t = TAILS[path] = Tail(path)
    return t.feed()


def session_files():
    now = time.time()
    out = []
    for proj in os.listdir(PROJECTS):
        pdir = os.path.join(PROJECTS, proj)
        if not os.path.isdir(pdir):
            continue
        for name in os.listdir(pdir):
            if not name.endswith('.jsonl'):
                continue
            path = os.path.join(pdir, name)
            mtime = os.path.getmtime(path)
            sub = os.path.join(pdir, name[:-6], 'subagents')
            if os.path.isdir(sub):
                mtime = max([mtime] + [os.path.getmtime(os.path.join(sub, f)) for f in os.listdir(sub)])
            if now - mtime < RECENT_S:
                out.append((mtime, name[:-6], pdir, path))
    out.sort(reverse=True)
    return out


def project_name(pdir, cwd):
    if cwd:
        return os.path.basename(cwd.rstrip('/')) or cwd
    return os.path.basename(pdir).split('-')[-1]


def agents_of(pdir, sid, now_ms):
    sub = os.path.join(pdir, sid, 'subagents')
    if not os.path.isdir(sub):
        return []
    agents = []
    for name in os.listdir(sub):
        if not (name.startswith('agent-') and name.endswith('.jsonl')):
            continue
        aid = name[6:-6]
        path = os.path.join(sub, name)
        try:
            with open(path[:-6] + '.meta.json') as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        t = tail(path)
        idle = now_ms - (os.path.getmtime(path) * 1000)
        working = not meta.get('finished') and idle < AGENT_IDLE_S * 1000
        agents.append({
            'id': aid,
            'type': meta.get('agentType') or 'general-purpose',
            'desc': clip(meta.get('description', ''), 40),
            'isWorking': working,
            'activity': t.activity or clip(meta.get('description', ''), 40),
            'startedAt': t.first,
            'endedAt': 0 if working else t.last,
            'calls': t.calls,
            'cost': round(t.cost, 4),
            'today': round(today_cost(t), 4),
            'model': t.model,
            'tool': t.tool,
            'at': t.activity_at or t.last,
            'finished': bool(meta.get('finished')),
            'events': t.events,
        })
    agents.sort(key=lambda a: a['startedAt'])
    return agents


def sessions():
    now = time.time()
    out = []
    for mtime, sid, pdir, path in session_files():
        t = tail(path)
        live = now - mtime < LIVE_S
        agents = agents_of(pdir, sid, now * 1000)
        working = sum(a['isWorking'] for a in agents) + (1 if live and t.busy else 0)
        out.append({
            'id': sid,
            'title': t.title or clip(t.prompt, 50) or sid[:8],
            'project': project_name(pdir, t.cwd),
            'at': int(mtime * 1000),
            'isLive': live,
            'working': working,
            'agents': len(agents),
            'cost': round(t.cost + sum(a['cost'] for a in agents), 2),
            'today': round(today_cost(t) + sum(a['today'] for a in agents), 2),
            'folder': t.home,
            'hasOffice': bool(t.home) and os.path.exists(office_path(t.home)),
        })
    return out


def office_for(t):
    saved = load_office(t.home) if t.home else None
    office = saved or auto_office(t.home or '?', t.roster)
    office['saved'] = bool(saved)
    office['job'] = job_view(t.home) if t.home else None
    office['generating'] = bool(office['job']) and office['job']['status'] == 'running'
    return office


def find_tail(sid):
    for mtime, s, pdir, path in session_files():
        if s == sid:
            return tail(path)
    return None


def state(sid):
    now = time.time()
    for mtime, s, pdir, path in session_files():
        if s != sid:
            continue
        t = tail(path)
        busy = t.busy and now - mtime < LIVE_S
        agents = agents_of(pdir, sid, now * 1000)
        log = [(at, 'producer', text) for at, text in t.events]
        for a in agents:
            log.append((a['startedAt'], a['type'], 'взял задачу: ' + a['desc']))
            log += [(at, a['type'], text) for at, text in a['events']]
            if not a['isWorking']:
                log.append((a['endedAt'], a['type'], 'сдал работу'))
        log.sort(key=lambda x: x[0])
        for a in agents:
            del a['events']
        return {
            'agents': agents,
            'producer': {'isBusy': busy, 'activity': t.activity if busy else '', 'at': t.activity_at, 'calls': t.calls, 'cost': round(t.cost, 4), 'model': t.model, 'tool': t.tool},
            'cost': round(t.cost + sum(a['cost'] for a in agents), 4),
            'office': office_for(t),
            'log': [{'at': at, 'who': who, 'text': clip(text)} for at, who, text in log[-LOG_MAX:]],
            'since': t.first,
        }
    return None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, *a):
        pass

    def end_headers(self):
        # страница и office.js меняются при правках — кэш браузера не нужен
        if not self.path.startswith('/api/'):
            self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def send_json(self, body, code=200):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        # /api/office — собрать заново, /api/office/cancel — отменить, /api/office/undo — вернуть прежний
        url = urlparse(self.path)
        action = {'/api/office': 'start', '/api/office/cancel': 'cancel', '/api/office/undo': 'undo'}.get(url.path)
        if not action:
            return self.send_json({'error': 'нет такого адреса'}, 404)
        sid = parse_qs(url.query).get('s', [''])[0]
        with LOCK:
            t = find_tail(sid)
            if not t or not t.home:
                return self.send_json({'error': 'у сессии не нашлась папка'}, 404)
            folder, roster = t.home, dict(t.roster)
        if action == 'start' and not start_office(folder, roster):
            return self.send_json({'error': 'офис уже собирается'}, 409)
        if action == 'cancel' and not cancel_office(folder):
            return self.send_json({'error': 'отменять нечего'}, 409)
        if action == 'undo' and not undo_office(folder):
            return self.send_json({'error': 'прежнего офиса нет'}, 409)
        return self.send_json(job_view(folder), 202 if action == 'start' else 200)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == '/api/sessions':
                with LOCK:
                    body = sessions()
                return self.send_json(body)
            if url.path == '/api/stats':
                return self.send_json(stats())
            if url.path == '/api/state':
                with LOCK:
                    st = state(parse_qs(url.query).get('s', [''])[0])
                return self.send_json(st if st else {'error': 'нет такой сессии'}, 200 if st else 404)
        except Exception as e:  # офис только смотрит: ошибка не роняет сервер
            return self.send_json({'error': str(e)}, 500)
        return super().do_GET()


def main():
    global DATA
    import argparse
    ap = argparse.ArgumentParser(description='Agent Office: пиксельный офис агентов Claude Code')
    ap.add_argument('--port', type=int, default=8767)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--data', default=DATA, help='где хранить офисы папок')
    ap.add_argument('--watch-parent', action='store_true', help='выйти, когда умрёт родительский процесс')
    args = ap.parse_args()
    DATA = os.path.expanduser(args.data)
    if args.watch_parent:
        ppid = os.getppid()

        def watch():
            while parent_alive(ppid):
                time.sleep(2)
            os._exit(0)
        threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=stats, daemon=True).start()  # первый проход по журналам — заранее
    print('Agent Office: http://%s:%d' % (args.host, args.port), flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
