#!/usr/bin/env python3
"""Agent Office: пиксельный офис, где видно, как работают агенты Claude Code.

Читает журналы всех сессий (~/.claude/projects) и отдаёт состояние офиса
выбранной сессии. В журналы ничего не пишет, хуки не нужны.

  python3 server.py [--port 8767] [--data ~/.claude/agent-office] [--watch-parent]

  /                         страница офиса
  /api/sessions             сессии за последние сутки; today — потрачено сегодня
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


def message_cost(model, usage):
    """Стоимость одного ответа модели, формула reel.py cost."""
    if model not in PRICES:
        return 0.0
    p_in, p_out, p_read = PRICES[model]
    cc = usage.get('cache_creation') or {}
    w1h = cc.get('ephemeral_1h_input_tokens', 0)
    w5m = cc.get('ephemeral_5m_input_tokens', 0) if cc else usage.get('cache_creation_input_tokens', 0)
    usd = (usage.get('input_tokens', 0) * p_in + usage.get('output_tokens', 0) * p_out
           + usage.get('cache_read_input_tokens', 0) * p_read + w5m * p_in * 1.25 + w1h * p_in * 2) / 1e6
    return usd * (2 if usage.get('speed') == 'fast' else 1)


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
GENERATING = {}  # папка → время старта генерации


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


def auto_office(folder, roster):
    """Офис без сохранённой раскладки: отделы по плагинам, имена из названий."""
    zones = {}
    for t in roster:
        if t in BUILTIN:
            continue
        ns = t.split(':')[0].upper() if ':' in t else 'СВОИ АГЕНТЫ'
        zones.setdefault(ns, []).append(dress({'type': t, 'name': humanize(t)}))
    boss = dress({'type': 'main', 'name': 'Главный', 'prop': 'crown', 'shirt': '#3b3f8f'})
    rows = [{'name': 'ГЛАВНЫЙ', 'desks': [boss]}] if not zones else []
    for i, (name, desks) in enumerate(sorted(zones.items())):
        rows.append({'name': name, 'desks': ([boss] if i == 0 else []) + desks})
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
У главной сессии (руководителя) свой стол, остальные агенты — сотрудники.

Агенты (тип — описание):
{agents}

Правила:
- title: вывеска офиса заглавными, 1–3 слова, по сути проекта.
- boss: должность руководителя по-русски (например «Продюсер», «Тимлид»), предмет из списка.
- zones: 1–4 отдела по смыслу работы, у каждого короткое название заглавными; в отделе до 5 столов, лучше 3–5.
- Каждый агент, кроме универсальных ({builtin}), — ровно за одним столом; тип копируй дословно.
- Универсальные агенты — в guests.
- name: должность по-русски, до 12 знаков, не имя человека («Ревьюер», «Сыщик», «Монтажёр»).
- prop: предмет на столе, подходящий к работе, из списка в схеме."""


def find_claude():
    for p in [shutil.which('claude'), os.path.expanduser('~/.local/bin/claude'), '/opt/homebrew/bin/claude', '/usr/local/bin/claude']:
        if p and os.access(p, os.X_OK):
            return p
    for p in sorted(glob.glob(os.path.expanduser('~/.nvm/versions/node/*/bin/claude')), reverse=True):
        return p
    return None


def generate_office(folder, roster):
    """Раскладка по смыслу — одним вызовом claude -p; не вышло — автораскладка."""
    office = None
    exe = find_claude()
    agents = '\n'.join('- %s — %s' % (t, clip(d, 220)) for t, d in sorted(roster.items()))
    prompt = PROMPT.format(name=os.path.basename(folder), folder=folder, agents=agents or '(только универсальные)',
                           builtin=', '.join(sorted(BUILTIN)))
    if exe:
        try:
            out = subprocess.run(
                [exe, '-p', '--model', 'sonnet', '--tools', '', '--no-session-persistence', '--setting-sources', '',
                 '--output-format', 'json', '--json-schema', json.dumps(SCHEMA),
                 '--system-prompt', 'Ты раскладываешь агентов по пиксельному офису. Отвечай только JSON по схеме.'],
                input=prompt, capture_output=True, text=True, timeout=180, cwd=DATA if os.path.isdir(DATA) else HERE,
                env=dict(os.environ, PATH=os.path.dirname(exe) + ':' + os.environ.get('PATH', '/usr/bin:/bin')))
            office = json.loads(out.stdout).get('structured_output')
        except (subprocess.SubprocessError, ValueError, OSError) as e:
            sys.stderr.write('генерация офиса: %s\n' % e)
    if not office or not office.get('zones'):
        result = auto_office(folder, roster)
        result['generated'] = 'auto'
    else:
        result = tidy(folder, roster, office)
    os.makedirs(os.path.dirname(office_path(folder)), exist_ok=True)
    with open(office_path(folder), 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def tidy(folder, roster, raw):
    """Ответ модели — в файл офиса: чужие типы выкинуть, забытых посадить, руководителя — первым."""
    seen, zones = set(), []
    for z in raw['zones']:
        desks = []
        for d in z.get('desks') or []:
            t = d.get('type')
            if t in roster and t not in BUILTIN and t not in seen:
                seen.add(t)
                desks.append(dress({'type': t, 'name': clip(d.get('name') or humanize(t), 13), 'prop': d.get('prop')}))
        if desks:
            zones.append({'name': clip(z.get('name') or 'ОТДЕЛ', 24).upper(), 'desks': desks})
    rest = [dress({'type': t, 'name': humanize(t)}) for t in roster if t not in BUILTIN and t not in seen]
    if rest:
        zones.append({'name': 'ПРОЧИЕ', 'desks': rest})
    boss = dress({'type': 'main', 'name': clip(raw['boss'].get('name') or 'Главный', 13), 'prop': raw['boss'].get('prop'),
                  'shirt': '#3b3f8f'})
    if not zones:
        zones = [{'name': 'ГЛАВНЫЙ', 'desks': []}]
    first = zones[0]['desks']
    # руководитель — в середине полного ряда, иначе первым
    first.insert(2 if len(first) == 4 else 0, boss)
    guests = {g['type']: {'name': clip(g.get('name') or humanize(g['type']), 13), 'prop': g.get('prop') if g.get('prop') in PROPS else 'cap'}
              for g in raw.get('guests') or [] if g.get('type') in roster}
    return {'folder': folder, 'title': clip(raw.get('title') or os.path.basename(folder), 28).upper(),
            'zones': zones, 'guests': guests, 'generated': 'claude'}



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
    office['generating'] = t.home in GENERATING
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
            'producer': {'isBusy': busy, 'activity': t.activity if busy else '', 'at': t.activity_at, 'calls': t.calls, 'cost': round(t.cost, 4)},
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
        url = urlparse(self.path)
        if url.path != '/api/office':
            return self.send_json({'error': 'нет такого адреса'}, 404)
        sid = parse_qs(url.query).get('s', [''])[0]
        with LOCK:
            t = find_tail(sid)
            if not t or not t.home:
                return self.send_json({'error': 'у сессии не нашлась папка'}, 404)
            if t.home in GENERATING:
                return self.send_json({'error': 'офис уже генерируется'}, 409)
            folder, roster = t.home, dict(t.roster)
            GENERATING[folder] = time.time()
        try:
            office = generate_office(folder, roster)  # десятки секунд: без замка
        except Exception as e:
            return self.send_json({'error': str(e)}, 500)
        finally:
            GENERATING.pop(folder, None)
        return self.send_json(office)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == '/api/sessions':
                with LOCK:
                    body = sessions()
                return self.send_json(body)
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
    print('Agent Office: http://%s:%d' % (args.host, args.port), flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
